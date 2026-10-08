"""Normalize common Stage 2 AI JSON variants before schema validation."""
from __future__ import annotations

import copy
import logging
from typing import Any

from pa_agent.ai.trace_normalize import normalize_stage2_traces
from pa_agent.util.price_tick import (
    normalize_breakout_basis_extreme,
    normalize_breakout_entry_price,
    parse_k_seq,
)

logger = logging.getLogger(__name__)

_TRADE_ORDER_TYPES = frozenset({"限价单", "突破单", "市价单"})
_NO_ORDER_PRICE_FIELDS = (
    "order_direction",
    "entry_price",
    "take_profit_price",
    "stop_loss_price",
    "entry_basis_bar",
    "entry_basis_extreme",
    "entry_rule",
)

# Valid enum values for features_used in next_bar_prediction / next_cycle_prediction.
# Must stay in sync with schemas.py _NEXT_BAR_PREDICTION / _NEXT_CYCLE_PREDICTION.
_VALID_FEATURES_USED = frozenset({
    "stage1_diagnosis",
    "kline_features",
    "analysis_history",
    "experience_library",
    "stage2_decision",
    "previous_prediction_summary",
})


def _trace_node_answer(trace: Any, node_id: str) -> str | None:
    if not isinstance(trace, list):
        return None
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() == node_id:
            return str(item.get("answer", "") or "").strip()
    return None


def _section14_violated(trace: Any) -> bool:
    """Return True only when §14 answer is 是 AND the reason text confirms a violation.

    Background: §14 question is "是否触犯禁止行为清单？"
      answer=是  → violated (程序强制 order_type=不下单)
      answer=否  → not violated (can proceed)

    Some models incorrectly write answer=是 to mean "I completed the scan (no violations)".
    To guard against this common mistake we cross-check the reason text: if it contains
    explicit denial phrases (未触犯 / 未违反 / 无触犯 / 通过) we do NOT treat it as a
    violation.  This is a safety hatch — the prompt now clearly specifies answer=否 for
    the no-violation case, so future outputs should be correct.
    """
    _DENIAL_PHRASES = ("未触犯", "未违反", "无触犯", "无违规", "通过扫描", "扫描通过", "无禁止", "未触发")
    if not isinstance(trace, list):
        return False
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        if not nid.startswith("14"):
            continue
        if str(item.get("answer", "")).strip() != "是":
            continue
        # answer=是: check reason for denial phrases before treating as violation
        reason = str(item.get("reason", "") or "")
        if any(phrase in reason for phrase in _DENIAL_PHRASES):
            # AI wrote answer=是 but reason says no violation — ignore (AI used wrong answer)
            logger.debug(
                "_section14_violated: node %s answer=是 but reason contains denial phrase; "
                "treating as NOT violated (AI should use answer=否 for no-violation)",
                nid,
            )
            continue
        return True
    return False


def _clear_decision_to_no_order(decision: dict[str, Any]) -> None:
    decision["order_type"] = "不下单"
    for field in _NO_ORDER_PRICE_FIELDS:
        decision[field] = None
    decision["estimated_win_rate"] = None


def _set_trace_node_answer(
    trace: Any,
    node_id: str,
    answer: str,
    *,
    reason_suffix: str = "",
) -> None:
    if not isinstance(trace, list):
        return
    for item in trace:
        if not isinstance(item, dict):
            continue
        if str(item.get("node_id", "")).strip() != node_id:
            continue
        item["answer"] = answer
        if reason_suffix:
            base = str(item.get("reason", "") or "").strip()
            item["reason"] = f"{base}{reason_suffix}".strip()
        return


def _coerce_decision_no_order(out: dict[str, Any]) -> bool:
    """When trace/terminal reject a trade, clear decision prices (common model slip)."""
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return False
    if decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False

    trace = out.get("decision_trace")
    terminal = out.get("terminal")
    outcome = (
        str(terminal.get("outcome", "")).strip()
        if isinstance(terminal, dict)
        else ""
    )

    triggers: list[str] = []
    if _trace_node_answer(trace, "10.3") == "否":
        triggers.append("10.3=否")
    if outcome in ("wait", "reject"):
        triggers.append(f"terminal.outcome={outcome}")
    if _section14_violated(trace):
        triggers.append("§14触犯")

    if not triggers:
        return False

    _clear_decision_to_no_order(decision)
    logger.debug("Coerced decision to 不下单 (%s)", ", ".join(triggers))
    return True


def _normalize_signal_entry_bar_chain(bar_analysis: dict[str, Any], decision: dict[str, Any]) -> bool:
    """Signal K must be strictly older than entry K (larger seq); pending entry exempt."""
    if decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False
    signal_bar = bar_analysis.get("signal_bar")
    entry_bar = bar_analysis.get("entry_bar")
    if not isinstance(signal_bar, dict) or not isinstance(entry_bar, dict):
        return False

    strength = str(entry_bar.get("strength", "") or "").strip().lower()
    freshness = str(entry_bar.get("freshness", "") or "").strip().lower()
    pending = (
        strength == "not_triggered"
        or not entry_bar.get("bar")
        or freshness in ("pending", "stale", "invalid")
    )
    if pending:
        entry_bar["bar"] = None
        entry_bar["strength"] = "not_triggered"
        entry_bar.setdefault("freshness", "pending")
        if entry_bar.get("follow_through") in (None, "", False):
            entry_bar["follow_through"] = "pending"
        return False

    signal_seq = parse_k_seq(signal_bar.get("bar"))
    entry_seq = parse_k_seq(entry_bar.get("bar"))
    if signal_seq is None or entry_seq is None:
        return False
    if signal_seq > entry_seq:
        return False

    signal_bar["bar"] = f"K{entry_seq + 1}"
    logger.debug(
        "signal_bar K%s -> K%s (must be older than entry K%s)",
        signal_seq,
        entry_seq + 1,
        entry_seq,
    )
    return True


def _coerce_decision_when_trade_metrics_fail(
    out: dict[str, Any],
    *,
    decision_stance: str | None = None,
) -> bool:
    """After breakout entry snap, reject orders that still fail RR / trader equation."""
    decision = out.get("decision")
    if not isinstance(decision, dict) or decision.get("order_type") not in _TRADE_ORDER_TYPES:
        return False
    if decision.get("entry_price") is None:
        return False

    from pa_agent.util.trade_metrics import validate_order_trade_metrics

    metric_errors = validate_order_trade_metrics(
        decision, decision_stance=decision_stance
    )
    if not metric_errors:
        return False

    summary = metric_errors[0]
    _clear_decision_to_no_order(decision)
    _set_trace_node_answer(
        out.get("decision_trace"),
        "10.3",
        "否",
        reason_suffix=f"（程序按 decision 三价校验未通过：{summary}，已改为不下单。）",
    )
    terminal = out.get("terminal")
    if isinstance(terminal, dict):
        terminal["outcome"] = "reject"
        terminal["node_id"] = "10.3"
        terminal.setdefault(
            "label",
            "交易者方程/盈亏比未达标，不下单",
        )
    logger.debug("Coerced decision to 不下单 (trade metrics: %s)", summary)
    return True


# Reused by _coerce_terminal_when_no_entry_plan to detect a *genuine* §14 violation.
# Mirrors the same denial-phrase guard in decision_tree.validate_stage2_trace_consistency
# so the auto-fix and the validator agree on what counts as a real §14 hit.
_SECTION14_DENIAL_PHRASES = (
    "未触犯", "未违反", "无触犯", "无违规", "通过扫描", "扫描通过", "无禁止", "未触发",
)


def _section14_actually_violated(trace: Any) -> bool:
    """True iff a §14 node has answer=是 AND its reason does not contain a denial phrase.

    Many models write ``answer=是`` to mean "I completed the scan" — those are
    false positives.  The genuine violation case has answer=是 AND a reason that
    describes the actual prohibited behavior (e.g. "触犯：宽通道中追突破").
    """
    if not isinstance(trace, list):
        return False
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        if not nid.startswith("14"):
            continue
        if str(item.get("answer", "")).strip() != "是":
            continue
        reason = str(item.get("reason", "") or "")
        if any(p in reason for p in _SECTION14_DENIAL_PHRASES):
            continue
        return True
    return False


def _coerce_terminal_when_no_entry_plan(out: dict[str, Any]) -> bool:
    """Auto-fix the "no entry plan yet terminal=reject" anti-pattern.

    The model frequently writes ``terminal.outcome=reject / node_id=10.3`` for
    ANY no-trade case, but the validator's rule (decision_tree.py
    ``validate_stage2_trace_consistency``) only allows ``reject`` when a real
    trade plan existed for the trader equation to evaluate.  When
    §9.0=否/等待 (no signal bar) or §10.1=否 (no stop-loss anchor), there is
    nothing to reject — the correct semantic is ``outcome=wait``.

    Auto-fix behaviour:
      * Detect: ``order_type=不下单`` AND §9.0=否/等待 OR §10.1=否
                AND ``terminal.outcome=reject``
                AND §14 is NOT a genuine violation.
      * Action: ``terminal.outcome`` → ``"wait"``;
                ``terminal.node_id`` → ``"9.0"`` if §9.0=否, else ``"10.1"``;
                ``terminal.label`` → standardized "§X=否,无入场方案,等待"
                (only if model label is empty or still contains the old
                 "reject"/"方程不通过" wording).

    Exception: when §14 is genuinely violated, ``outcome=reject`` is the
    correct semantic (a prohibition explicitly terminated the analysis),
    so this function leaves that case untouched.

    Returns True iff a change was applied.
    """
    decision = out.get("decision")
    terminal = out.get("terminal")
    if not isinstance(decision, dict) or not isinstance(terminal, dict):
        return False
    if decision.get("order_type") != "不下单":
        return False

    outcome = str(terminal.get("outcome", "") or "").strip()
    if outcome != "reject":
        return False  # already "wait"/"trade"/"proceed" — no fix needed

    trace = out.get("decision_trace")
    if not isinstance(trace, list):
        return False

    # §14 genuine violation takes precedence — leave outcome=reject alone.
    if _section14_actually_violated(trace):
        return False

    # Find the earliest entry-plan negative node.
    target_node: str | None = None
    for item in trace:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("node_id", "")).strip()
        ans = str(item.get("answer", "") or "").strip()
        if nid == "9.0" and ans in ("否", "等待"):
            target_node = "9.0"
            break  # §9.0 is the earliest — stop searching
        if nid == "10.1" and ans == "否" and target_node is None:
            target_node = "10.1"
            # keep looking in case §9.0 appears later (shouldn't, but defensive)

    if target_node is None:
        return False  # not the anti-pattern we handle

    old_outcome = terminal.get("outcome")
    old_nid = terminal.get("node_id")
    terminal["outcome"] = "wait"
    terminal["node_id"] = target_node

    # Refresh label only if the model left it as the old reject wording,
    # so we don't clobber a more informative user-written label.
    old_label = str(terminal.get("label", "") or "").strip()
    if (
        not old_label
        or "方程" in old_label
        or "reject" in old_label.lower()
        or "拒绝" in old_label
    ):
        terminal["label"] = f"§{target_node}=否,无入场方案,等待"

    logger.debug(
        "Coerced terminal no_entry_plan: outcome %s->%s, node_id %s->%s",
        old_outcome, terminal["outcome"], old_nid, terminal["node_id"],
    )
    return True


def _normalize_next_cycle_prediction(prediction: dict[str, Any]) -> None:
    """In-place normalize next_cycle_prediction common model quirks. Idempotent."""
    from pa_agent.ai.cycle_enums import CYCLE_ORDER

    if not isinstance(prediction, dict):
        return

    # 1. unpredictable fallback
    unpredictable = bool(prediction.get("unpredictable", False))
    prediction["unpredictable"] = unpredictable

    # 2. features_used: ensure list, dedup, minimum set, filter invalid values
    feats = prediction.get("features_used")
    if not isinstance(feats, list):
        feats = []
    feats = [f for f in feats if isinstance(f, str)]
    # Filter out values not in the schema enum (e.g. "detected_patterns")
    invalid_feats = [f for f in feats if f not in _VALID_FEATURES_USED]
    if invalid_feats:
        logger.debug(
            "next_cycle_prediction.features_used dropped invalid values: %s",
            invalid_feats,
        )
    feats = [f for f in feats if f in _VALID_FEATURES_USED]
    if "stage1_diagnosis" not in feats:
        feats.insert(0, "stage1_diagnosis")
    seen: set[str] = set()
    deduped: list[str] = []
    for f in feats:
        if f not in seen:
            deduped.append(f)
            seen.add(f)
    prediction["features_used"] = deduped

    # 3. reasoning truncation
    reasoning = prediction.get("reasoning")
    if isinstance(reasoning, str) and len(reasoning) > 1500:
        prediction["reasoning"] = reasoning[:1499] + "…"
    elif not isinstance(reasoning, str):
        prediction["reasoning"] = ""

    if unpredictable:
        # unpredictable → force cycle / direction / probabilities = null
        prediction["cycle"] = None
        prediction["direction"] = None
        prediction["probabilities"] = None
        return

    # 4. probabilities integer rounding, clamping, and sum normalization
    probs = prediction.get("probabilities")
    if isinstance(probs, dict):
        normalized: dict[str, int] = {}
        for key in CYCLE_ORDER:
            raw = probs.get(key)
            try:
                value = int(round(float(raw))) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            normalized[key] = max(0, min(100, value))

        # Auto-rescale if sum is outside [99, 101] (model arithmetic error)
        total = sum(normalized[k] for k in CYCLE_ORDER)
        if total > 0 and not (99 <= total <= 101):
            scale = 100.0 / total
            rescaled = {k: int(round(normalized[k] * scale)) for k in CYCLE_ORDER}
            # Fix rounding residual so sum == 100
            diff = 100 - sum(rescaled[k] for k in CYCLE_ORDER)
            if diff != 0:
                # Add/subtract from the largest bucket
                biggest = max(CYCLE_ORDER, key=lambda k: rescaled[k])
                rescaled[biggest] = max(0, rescaled[biggest] + diff)
            normalized = rescaled
            logger.debug(
                "next_cycle_prediction probabilities rescaled (sum was %d -> 100)", total
            )

        prediction["probabilities"] = normalized

        # 5. cycle = argmax, tie-break by CYCLE_ORDER literal order
        max_value = max(normalized[k] for k in CYCLE_ORDER)
        # First winner in CYCLE_ORDER order
        argmax_cycle = next(k for k in CYCLE_ORDER if normalized[k] == max_value)

        model_cycle = str(prediction.get("cycle") or "").strip().lower()
        if model_cycle != argmax_cycle:
            logger.debug(
                "next_cycle_prediction cycle %r -> %r (argmax of %s)",
                model_cycle, argmax_cycle, normalized,
            )
            prediction["cycle"] = argmax_cycle

    # direction: keep model value; only type-coerce non-string to None
    direction = prediction.get("direction")
    if direction is not None and not isinstance(direction, str):
        prediction["direction"] = None


def _normalize_next_bar_prediction(prediction: dict[str, Any]) -> None:
    """In-place normalize next_bar_prediction common model quirks. Idempotent."""
    if not isinstance(prediction, dict):
        return

    # 1. unpredictable fallback
    unpredictable = bool(prediction.get("unpredictable", False))
    prediction["unpredictable"] = unpredictable

    # 2. features_used: ensure list, dedup, minimum set, filter invalid values
    feats = prediction.get("features_used")
    if not isinstance(feats, list):
        feats = []
    feats = [f for f in feats if isinstance(f, str)]
    # Filter out values not in the schema enum (e.g. "detected_patterns")
    invalid_feats = [f for f in feats if f not in _VALID_FEATURES_USED]
    if invalid_feats:
        logger.debug(
            "next_bar_prediction.features_used dropped invalid values: %s",
            invalid_feats,
        )
    feats = [f for f in feats if f in _VALID_FEATURES_USED]
    if "stage1_diagnosis" not in feats:
        feats.insert(0, "stage1_diagnosis")
    seen: set[str] = set()
    deduped: list[str] = []
    for f in feats:
        if f not in seen:
            deduped.append(f)
            seen.add(f)
    prediction["features_used"] = deduped

    # 3. reasoning truncation (R7.6)
    reasoning = prediction.get("reasoning")
    if isinstance(reasoning, str) and len(reasoning) > 1500:
        prediction["reasoning"] = reasoning[:1499] + "…"
    elif not isinstance(reasoning, str):
        prediction["reasoning"] = ""

    if unpredictable:
        # unpredictable → force direction / probabilities = null
        prediction["direction"] = None
        prediction["probabilities"] = None
        return

    # 4. probabilities integer rounding (R3.1)
    probs = prediction.get("probabilities")
    if isinstance(probs, dict):
        normalized: dict[str, int] = {}
        bar_order = ("bullish", "bearish", "neutral")
        for key in bar_order:
            raw = probs.get(key)
            try:
                value = int(round(float(raw))) if raw is not None else 0
            except (TypeError, ValueError):
                value = 0
            normalized[key] = max(0, min(100, value))

        # Auto-rescale if sum is outside [99, 101] (model arithmetic error)
        total = sum(normalized[k] for k in bar_order)
        if total > 0 and not (99 <= total <= 101):
            scale = 100.0 / total
            rescaled = {k: int(round(normalized[k] * scale)) for k in bar_order}
            diff = 100 - sum(rescaled[k] for k in bar_order)
            if diff != 0:
                biggest = max(bar_order, key=lambda k: rescaled[k])
                rescaled[biggest] = max(0, rescaled[biggest] + diff)
            normalized = rescaled
            logger.debug(
                "next_bar_prediction probabilities rescaled (sum was %d -> 100)", total
            )

        prediction["probabilities"] = normalized

        # 5. direction = argmax (R3.3) — respect model choice on ties
        order = ("bullish", "bearish", "neutral")
        max_value = max(normalized[k] for k in order)
        tied_winners = [k for k in order if normalized[k] == max_value]
        model_direction = str(prediction.get("direction") or "").strip().lower()

        if len(tied_winners) > 1:
            # Tie: preserve model's choice if it's one of the winners
            if model_direction in tied_winners:
                pass  # keep model's semantic choice
            else:
                # Model direction not in tied set — override with first winner
                logger.warning(
                    "next_bar_prediction direction=%r not in tied winners %s "
                    "(probs=%s); overriding to %r",
                    model_direction, tied_winners, normalized, tied_winners[0],
                )
                prediction["direction"] = tied_winners[0]
        else:
            # Clear winner
            expected = tied_winners[0]
            if model_direction != expected:
                logger.debug(
                    "next_bar_prediction direction %r -> %r (argmax of %s)",
                    model_direction, expected, normalized,
                )
                prediction["direction"] = expected
            # else: model direction matches argmax, no change needed
    # else: unparseable probabilities with unpredictable=False — leave for validator


def _max_bar_seq_from_frame(kline_frame: Any) -> int | None:
    bars = getattr(kline_frame, "bars", None) if kline_frame is not None else None
    if not bars:
        return None
    seqs = [int(getattr(b, "seq", 0)) for b in bars if getattr(b, "seq", None)]
    return max(seqs) if seqs else None


def normalize_stage2(
    obj: dict[str, Any],
    *,
    normalization_mode: str = "strict",
    kline_frame: Any = None,
    decision_stance: str | None = None,
    stage1_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of *obj* with decision_trace quirks corrected."""
    out = copy.deepcopy(obj)
    frame_max = _max_bar_seq_from_frame(kline_frame)
    _coerce_decision_no_order(out)
    decision = out.get("decision")
    if isinstance(decision, dict) and normalize_breakout_basis_extreme(decision):
        logger.debug(
            "breakout entry_basis_extreme aligned to %s for %s",
            decision.get("entry_basis_extreme"),
            decision.get("order_direction"),
        )
    if isinstance(decision, dict) and normalize_breakout_entry_price(
        decision, kline_frame=kline_frame
    ):
        logger.debug(
            "breakout entry_price adjusted to basis extreme ± 1 tick (basis=%s)",
            decision.get("entry_basis_bar"),
        )
    _coerce_decision_when_trade_metrics_fail(out, decision_stance=decision_stance)
    _coerce_terminal_when_no_entry_plan(out)

    # ── DecisionNodeEngine: fill §9.1/§9.2/§9.3/§9.5/§11 ─────────────────────
    if kline_frame is not None:
        try:
            from pa_agent.ai.decision_nodes import DecisionNodeEngine
            DecisionNodeEngine.apply_stage2(out, kline_frame, stage1_json)
        except Exception as exc:  # noqa: BLE001
            logger.warning("DecisionNodeEngine.apply_stage2 failed: %s", exc)

    normalize_stage2_traces(
        out,
        normalization_mode=normalization_mode,
        default_max_seq=frame_max,
    )
    decision = out.get("decision")
    if isinstance(decision, dict) and decision.get("order_type") == "不下单":
        # A no-order decision must satisfy the schema "then" branch:
        # all price fields + direction must be null.
        for field in _NO_ORDER_PRICE_FIELDS:
            decision[field] = None
        decision["estimated_win_rate"] = None

    bar_analysis = out.get("bar_analysis")
    decision = out.get("decision")
    if isinstance(bar_analysis, dict) and isinstance(decision, dict):
        if _normalize_signal_entry_bar_chain(bar_analysis, decision):
            pass
    if isinstance(bar_analysis, dict):
        signal_bar = bar_analysis.get("signal_bar")
        if isinstance(signal_bar, dict) and not signal_bar.get("bar"):
            signal_bar["bar"] = None
            signal_bar.setdefault("quality", "invalid")
            signal_bar.setdefault("pattern", "none")

        entry_bar = bar_analysis.get("entry_bar")
        if isinstance(entry_bar, dict):
            strength = str(entry_bar.get("strength", "") or "").strip().lower()
            has_bar = bool(entry_bar.get("bar"))
            if strength == "not_triggered" or not has_bar:
                # Pending limit/breakout orders do not have an actual entry bar
                # yet. Normalize common model variants before schema checks.
                entry_bar["strength"] = "not_triggered"
                entry_bar.setdefault("bar", None)
                entry_bar.setdefault("freshness", "pending")
                if entry_bar.get("follow_through") in (None, "", "pending"):
                    entry_bar["follow_through"] = "pending"

    # Next bar prediction normalization (R8.6: only when field exists)
    pred = out.get("next_bar_prediction")
    if isinstance(pred, dict):
        _normalize_next_bar_prediction(pred)

    # Next cycle prediction normalization (only when field exists)
    pred_c = out.get("next_cycle_prediction")
    if isinstance(pred_c, dict):
        _normalize_next_cycle_prediction(pred_c)

    return out

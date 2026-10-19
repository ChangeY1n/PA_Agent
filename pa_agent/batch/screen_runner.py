"""批量自选筛选共享核心：CLI（batch_watchlist_screen.py）与 GUI 批量窗口共用。

职责边界：
- analyze_one：单只股票的完整处理（取 K 线 → 停板跳过 → 初筛分流 → 两阶段深析
  → 分类决策），无状态、可在线程池并发执行；
- thread_ctx：每工作线程独立 AppContext（客户端/编排器不跨线程共享）；
- should_deep_analyze：初筛分流的纯函数（pass / 方向 / conviction 门槛）；
- write_screen_summary：结果落盘 batch_analysis_results/screen_*/summary.json。

进度回调、并发编排、取消策略由调用方（CLI 的 main / GUI 的 QThread worker）
自行实现——本模块只提供可复用的原子操作。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor  # noqa: F401  (供调用方复用)
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import logging

logger = logging.getLogger("batch_screen")

# 进程级锁：csv_logger 无内建锁，多线程写同一 CSV 需串行化
_CSV_LOCK = threading.Lock()
_CTX_LOCAL = threading.local()

NO_ORDER = "不下单"

# 状态 → 显示标记（CLI 行输出与 GUI 结果表共用）
STATUS_MARKS = {
    "opportunity": "★机会",
    "no_order": "—观望",
    "screened_out": "·淘汰",
    "light_pass": "+入围",
    "limit_skip": "■停板",
    "failed": "✗失败",
}


def local_no_trade_gate(stage1_json: dict) -> bool:
    """阶段一本地短路：True = 跳过阶段二（省 ~60% token）。

    仅当 router 一个策略文件都不会加载时触发（direction=neutral 且
    cycle_position ∈ {spike, extreme_tr}）：阶段二无任何策略可依，
    历史记录 9/9 全部判「不下单」，跳过等价于省掉走过场。

    刻意不扩大到 neutral+区间/宽通道：600333（neutral+broad_channel，
    2026-10-08）检测到 breakout_test 后阶段二给出过限价单，扩大会误杀
    真实机会；也不看 detected_patterns —— 形态集合在出单/不出单两组
    几乎相同，无法区分。
    """
    from pa_agent.ai.router import route_strategy_files

    try:
        return not route_strategy_files(stage1_json)
    except Exception:  # noqa: BLE001
        return False


def _limit_state(symbol: str, bars: list) -> str:
    """判断当前价是否触及涨停/跌停；返回 "" / "涨停" / "跌停"。

    - 用最新一根（K1）的 close 对比前一根收盘推算的停板价；盘中 K1 未收盘时
      close 即最新价，同样成立（「当前」停板）。
    - 涨跌幅按代码前缀：主板 10%，创业板 30/科创 68 为 20%，北交 43/83/87/92
      为 30%；停板价按交易所规则四舍五入到分。
    - 局限：ST（5%）无法从K线识别，会漏判为可分析——影响极小，接受。
    """
    if len(bars) < 2:
        return ""
    last, prev = bars[0], bars[1]
    prev_close = getattr(prev, "close", None)
    if not prev_close:
        return ""
    if symbol.startswith(("30", "68")):
        ratio = Decimal("0.20")
    elif symbol.startswith(("43", "83", "87", "92")):
        ratio = Decimal("0.30")
    else:
        ratio = Decimal("0.10")
    q = Decimal("0.01")
    base = Decimal(str(prev_close))
    up = (base * (Decimal(1) + ratio)).quantize(q, ROUND_HALF_UP)
    down = (base * (Decimal(1) - ratio)).quantize(q, ROUND_HALF_UP)
    c = Decimal(str(last.close)).quantize(q)
    # 停板价是交易所硬边界：真实停板收盘价只能恰好等于；大幅越过只可能是
    # 除权除息缺口或脏数据，不算停板。
    if c == up:
        return "涨停"
    if c == down:
        return "跌停"
    return ""


def thread_ctx():
    """每个工作线程一个独立 AppContext（客户端/编排器不跨线程共享）。

    注意：AppContext.bootstrap() 在线程首个任务时快照 config/settings.json
    的 provider 配置；运行中在 GUI 改网关不影响已引导的线程。
    """
    ctx = getattr(_CTX_LOCAL, "ctx", None)
    if ctx is not None:
        return ctx

    from pa_agent.app_context import AppContext

    ctx = AppContext.bootstrap()

    # csv_logger 并发写保护（excel_logger 内部已有锁）
    csv_logger = getattr(ctx, "csv_logger", None)
    if csv_logger is not None:
        orig = csv_logger.log_decision

        def _locked(rec, _orig=orig):
            with _CSV_LOCK:
                _orig(rec)

        csv_logger.log_decision = _locked

    _CTX_LOCAL.ctx = ctx
    return ctx


def should_deep_analyze(
    light: dict | None,
    *,
    long_only: bool = True,
    min_conviction: int = 0,
) -> tuple[bool, str]:
    """初筛分流纯函数：返回 (是否进入深析, 淘汰原因)。

    淘汰优先级：初筛不通过 > 只做多模式下的空头方向 > conviction 低于门槛。
    light 为 fail-open 放行（pass 缺省 True）时视为通过。
    """
    if not light:
        return True, ""
    if not light.get("pass", True):
        return False, str(light.get("reason") or "")
    if long_only and str(light.get("direction") or "") == "bearish":
        # A股无便利做空；历史全部出单均为做多，空头方向不深析。
        return False, "（只做多模式：初筛方向为空头）"
    if min_conviction > 0:
        try:
            conviction = int(light.get("conviction") or 0)
        except (TypeError, ValueError):
            conviction = 0
        if conviction < min_conviction:
            return False, f"（conviction {conviction} < 门槛 {min_conviction}）"
    return True, ""


def analyze_one(
    symbol: str,
    timeframe: str,
    bar_count: int,
    use_local_gate: bool = True,
    *,
    mode: str = "light",
    light_only: bool = False,
    light_bars: int = 80,
    long_only: bool = True,
    min_conviction: int = 0,
    cancel_token=None,
    system_prompt: str = "",
    user_template: str = "",
    dual_screen: bool = True,
) -> dict:
    """分析单只股票：取 K 线 →（可选）轻量初筛 → 两阶段分析 → 分类决策。

    cancel_token：调用方共享的取消令牌（GUI 停止按钮）；None 时内部新建、
    永不置位（CLI 行为不变）。system_prompt / user_template 非空时覆盖
    light_screen 内置初筛提示词（GUI 可编辑）。dual_screen=True 时初筛跑
    双 prompt 共识（Brooks 主筛 + 复核官，双通过才进深析，token ×2）。
    """
    from pa_agent.ai.light_screen import (
        LIGHT_SCREEN_DEFAULT_BARS,
        run_light_screen,
        run_light_screen_dual,
    )
    from pa_agent.data.factory import create_data_source
    from pa_agent.data.snapshot import (
        INDICATOR_WARMUP_BARS,
        build_display_frame,
    )
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken

    token = cancel_token if cancel_token is not None else CancelToken()

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "failed",  # opportunity | no_order | screened_out | light_pass | limit_skip | failed
        "order_type": "",
        "order_direction": "",
        "entry_price": None,
        "stop_loss_price": None,
        "take_profit_price": None,
        "trade_confidence": None,
        "reasoning": "",
        "error": None,
        "light": None,  # 初筛判定 {pass, direction, setup, conviction, reason}
        "tokens_light": 0,
        "tokens_full": 0,
    }

    tdx = None
    t0 = time.monotonic()
    try:
        ctx = thread_ctx()

        tdx = create_data_source("tdx")
        tdx.connect()
        tdx.subscribe(symbol, timeframe)
        bars = tdx.latest_snapshot(bar_count + INDICATOR_WARMUP_BARS + 5)
        if not bars:
            raise ValueError("未获取到 K 线数据")

        limit = _limit_state(symbol, bars)
        if limit:
            result["status"] = "limit_skip"
            result["reasoning"] = f"（{limit}，跳过分析）"
            return _finish(result, t0)

        if mode == "light":
            n_light = max(20, min(light_bars or LIGHT_SCREEN_DEFAULT_BARS, bar_count))
            light_frame = build_display_frame(bars, n_light, symbol, timeframe)
            if light_frame is None:
                raise ValueError("K 线不足以构建初筛帧")

            # 本地预筛（免 token）：停盘/铁丝网/无趋势漂移直接淘汰，不调模型
            from pa_agent.ai.pre_gate import pre_gate_check

            ok, gate_reason = pre_gate_check(light_frame)
            if not ok:
                result["status"] = "screened_out"
                result["reasoning"] = f"（程序预筛：{gate_reason}）"
                return _finish(result, t0)

            screen = run_light_screen_dual if dual_screen else run_light_screen
            light = screen(
                ctx.client,
                light_frame,
                cancel_token=token,
                system_prompt=system_prompt,
                user_template=user_template,
            )
            result["light"] = {
                k: light.get(k)
                for k in ("pass", "direction", "setup", "conviction", "reason")
            }
            if dual_screen and light.get("dual"):
                result["light"]["dual"] = light["dual"]
            result["tokens_light"] = (light.get("usage_total") or {}).get("total_tokens") or 0
            deep, why_not = should_deep_analyze(
                result["light"], long_only=long_only, min_conviction=min_conviction
            )
            if not deep:
                result["status"] = "screened_out"
                result["reasoning"] = why_not
                return _finish(result, t0)
            if light_only:
                result["status"] = "light_pass"
                return _finish(result, t0)

        frame = build_display_frame(bars, bar_count, symbol, timeframe)
        if frame is None:
            raise ValueError("K 线不足以构建分析帧")

        orchestrator = TwoStageOrchestrator(
            client=ctx.client,
            assembler=ctx.assembler,
            router=ctx.router,
            validator=ctx.validator,
            pending_writer=ctx.pending_writer,
            exp_reader=ctx.exp_reader,
            settings=ctx.settings,
            csv_logger=ctx.csv_logger,
            excel_logger=ctx.excel_logger,
        )
        record = orchestrator.submit(
            frame,
            token,
            on_event=lambda evt: None,
            local_gate=local_no_trade_gate if use_local_gate else None,
        )

        if record.exception is not None:
            exc = record.exception
            raise ValueError(
                f"{exc.get('type', 'error')}: {exc.get('message', '')}"
                if isinstance(exc, dict)
                else str(exc)
            )

        decision = record.stage2_decision or {}
        inner = decision.get("decision", {}) if isinstance(decision, dict) else {}
        order = str(inner.get("order_type") or "")
        result["order_type"] = order
        result["order_direction"] = str(inner.get("order_direction") or "")
        result["entry_price"] = inner.get("entry_price")
        result["stop_loss_price"] = inner.get("stop_loss_price")
        result["take_profit_price"] = inner.get("take_profit_price")
        result["trade_confidence"] = inner.get("trade_confidence")
        result["reasoning"] = str(inner.get("reasoning") or "")
        result["status"] = "opportunity" if order and order != NO_ORDER else "no_order"
        result["tokens_full"] = (record.usage_total or {}).get("total_tokens") or 0

    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    finally:
        if tdx is not None:
            try:
                tdx.disconnect()
            except Exception:  # noqa: BLE001
                pass

    return _finish(result, t0)


def _finish(result: dict, t0: float) -> dict:
    result["seconds"] = round(time.monotonic() - t0, 1)
    return result


def summarize_results(results: list[dict]) -> dict:
    """汇总计数（summary.json 的统计字段）。"""
    return {
        "total": len(results),
        "opportunities": sum(1 for r in results if r.get("status") == "opportunity"),
        "no_order": sum(1 for r in results if r.get("status") == "no_order"),
        "screened_out": sum(1 for r in results if r.get("status") == "screened_out"),
        "light_pass": sum(1 for r in results if r.get("status") == "light_pass"),
        "limit_skip": sum(1 for r in results if r.get("status") == "limit_skip"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "tokens_light": sum(r.get("tokens_light") or 0 for r in results),
        "tokens_full": sum(r.get("tokens_full") or 0 for r in results),
    }


def write_screen_summary(output_dir: str | Path, results: list[dict], meta: dict) -> Path:
    """写 batch_analysis_results/screen_YYYYmmdd_HHMMSS/summary.json，返回运行目录。

    meta 携带运行参数（timeframe/bar_count/mode/light_only/min_conviction/
    duration_seconds 等），与 summarize_results 的统计字段合并落盘。
    """
    out_dir = Path(output_dir) / datetime.now().strftime("screen_%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "batch_time": datetime.now().isoformat(),
        **meta,
        **summarize_results(results),
        "results": results,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_dir

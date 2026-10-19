"""轻量初筛：单次紧凑调用判断是否值得跑完整两阶段分析.

批量筛选自选时，完整两阶段分析每只约 16 万 token。本模块用一次约 0.2 万
token 的调用先粗筛（Brooks 做多初筛：前 60 根程序提取摘要 + 近 20 根
正序紧凑 CSV），只有初筛通过的股票才进入完整管线；铁丝网/漂移/停盘在
调用前先被 pa_agent.ai.pre_gate 免 token 拒掉。

设计约束：
- 初筛失败（JSON 解析不出、网络错误）一律放行（fail-open）——初筛的职责
  是省钱，不是替阶段二做决策；漏报真信号的代价高于多分析几只股票。
- 与 GUI 完全解耦：仅批量脚本调用，两阶段管线不感知本模块。
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable

from pa_agent.ai.kline_features import compute_kline_geometry_features

if TYPE_CHECKING:
    from pa_agent.data.base import KlineFrame
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

LIGHT_SCREEN_DEFAULT_BARS = 80
LIGHT_SCREEN_DETAIL_BARS = 20     # 近期明细 CSV 行数（正序 1..N）
_PIVOT_WING = 3                   # swing 高低点：两侧各 N 根内为极值
_LEVEL_MERGE_ATR = 0.25           # 支撑/阻力位合并半径（×ATR，去重复价位）

LIGHT_SCREEN_SYSTEM_PROMPT = """\
你是基于价格行为学（Al Brooks Price Action）的A股1h级别初筛器。仅筛选【做多（Bullish）】机会，决定是否进入深度分析。

## 核心原则
1. 顺序说明：K线按时间正序排列，序号1为最早，最后一行（序号20）为最新已收盘K线。
2. 通过条件（满足任一且上方有至少 1.5 倍 ATR 的无阻力空间）：
   - 顺势回调企稳：多头趋势或强势突破后，经历2-5根缩量回调至EMA20附近，最新1-2根K线出现高收盘位阳线（H1/H2入场点）。
   - 强势突破初段：打破近期震荡区间或前高，出现大实体趋势阳线（trend_bull）且有跟随（follow-through），未过度远离EMA20（偏离小于2倍ATR）。
   - 区间下沿反转：清晰大箱体底部支撑位附近，出现二次测试不破并伴随强多头反转棒。
3. 拒绝条件（满足任一即拒绝）：
   - 空头趋势且无明确双底反转结构。
   - 窄幅重叠（铁丝网）、多根十字星/长上影线连续出现。
   - 追高风险：最新收盘价紧贴前期显著波段高点（阻力位下方不足1倍ATR），或连续多根阳线极度远离EMA20。

## 输出格式
严格只输出一行 JSON，字段顺序不可更改（先推理后结论）：
{"setup": "<形态简述>", "reason": "<结合关键序号、EMA与前高阻力位的判断依据>", "conviction": <0-100整数>, "pass": true或false}\
"""

# 第二意见（复核官）：与 Brooks 主初筛并行、独立判断，双通过才进深析。
# 刻意与主初筛视角互补——主初筛按 Brooks 形态学找买点（倾向找信号），
# 复核官从结构清晰度与入场位置把关（倾向否决平庸位置）。
LIGHT_SCREEN_SYSTEM_PROMPT_B = """\
你是A股K线复核初筛器，与另一个基于价格行为的初筛器并行独立工作（双通过才进入深度分析）。\
你的任务：从「结构清晰度与入场位置」复核当前股票是否值得进入深度分析。

K线按时间正序排列，序号1为最早，最后一行为最新已收盘K线。

通过（"pass": true）需同时满足：
1. 存在清晰可定义的结构：上升趋势通道、明确震荡区间、或正在发生的突破，三者居其一；
2. 当前价格处于结构内的合理做多位置（回调支撑附近 / 区间下沿 / 突破初段回踩），而非结构中部或顶部；
3. 量价配合不背离（上攻放量、回调缩量）。

拒绝（"pass": false）——满足任一：
1. 结构不清晰：铁丝网、窄幅重叠、无趋势漂移；
2. 位置不佳：价格远离支撑、紧贴阻力、追高风险大；
3. 量价背离：上攻无量或回调放量。

严格只输出一行 JSON，字段顺序不可更改：
{"setup": "<形态简述>", "reason": "<判断依据>", "conviction": <0-100整数>, "pass": true或false}\
"""

LIGHT_SCREEN_USER_TEMPLATE = (
    "品种:{symbol} | 周期:{timeframe}\n\n"
    "## 远期背景（前{bg_count}根K线程序提取摘要）\n"
    "{background_block}\n\n"
    "## 近期K线明细（正序：1={detail_count}根前，{detail_count}=最新收盘）\n"
    "字段：序号,最高,最低,收盘,量(万),EMA20,ATR,类型,实体比,上影比,收盘位,近5突破\n"
    "{kline_table}\n\n"
    "请输出初筛 JSON。"
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _f2(v: float | None) -> str:
    if v is None or v != v:  # None/NaN 防御
        return "-"
    return f"{v:.2f}"


def _merge_levels(levels: list[float], tol: float) -> list[float]:
    """近似价位去重：按价排序后把间距小于 tol 的簇合成一个（保留簇内极值）。"""
    if not levels:
        return []
    out: list[float] = []
    for v in sorted(levels):
        if out and abs(v - out[-1]) <= tol:
            continue
        out.append(v)
    return out


def _render_background_summary(frame: "KlineFrame", shown: int, detail_bars: int) -> str:
    """远期背景：最近 detail_bars 根之外的更早 K 线程序提取摘要（确定性规则）。

    - 区间极值：背景窗口内最高/最低及距今根数（距今按整帧最新已收盘K线计）；
    - 关键阻力/支撑位：swing 高低点（两侧 _PIVOT_WING 根内的极值）+ 窗口极值，
      以 _LEVEL_MERGE_ATR×ATR 合并近似价位，取最新收盘上方/下方各 2 个最近的；
    - 前期结构：按窗口内极值出现先后 + 波幅相对 ATR 的宽窄生成一句话。
    """
    start, stop = detail_bars, shown
    if stop - start < 10:
        return "（更早K线不足，无远期背景）"
    bars = frame.bars
    close = bars[0].close
    atr = frame.indicators.atr14[0] if frame.indicators.atr14 else 0.0
    if not atr or atr != atr:
        atr = (max(b.high for b in bars) - min(b.low for b in bars)) / len(bars) or 1.0

    hi_i = max(range(start, stop), key=lambda i: bars[i].high)
    lo_i = min(range(start, stop), key=lambda i: bars[i].low)
    hi, lo = bars[hi_i].high, bars[lo_i].low

    def _pivots(key) -> list[int]:
        out = []
        for i in range(start, stop):
            lo_b, hi_b = max(start, i - _PIVOT_WING), min(stop, i + _PIVOT_WING + 1)
            if all(key(bars[j]) <= key(bars[i]) for j in range(lo_b, hi_b) if j != i):
                out.append(i)
        return out

    tol = _LEVEL_MERGE_ATR * atr
    resist = _merge_levels([bars[i].high for i in _pivots(lambda b: b.high)] + [hi], tol)
    support = _merge_levels([bars[i].low for i in _pivots(lambda b: b.low)] + [lo], tol)
    # 阻力：现价上方取最近 2 个，按从高到低输出；支撑同理取现价下方最近 2 个
    res = sorted((v for v in resist if v > close))[:2]
    sup = sorted((v for v in support if v < close), reverse=True)[:2]

    width = hi - lo
    width_label = "宽幅" if width >= 5 * atr else ("窄幅" if width <= 2.5 * atr else "中幅")
    if hi_i > lo_i:  # newest-first 索引越大越早：最高点先出现 = 冲高后回落
        structure = f"冲高{hi:.2f}后回落，形成{lo:.2f}-{hi:.2f}{width_label}震荡"
    else:
        structure = f"探底{lo:.2f}后回升，形成{lo:.2f}-{hi:.2f}{width_label}震荡"

    res_txt = ", ".join(f"{v:.2f}" for v in res) or "无"
    sup_txt = ", ".join(f"{v:.2f}" for v in sup) or "无"
    return (
        f"- 区间极值：最高 {hi:.2f} (距今{hi_i}根)，最低 {lo:.2f} (距今{lo_i}根)\n"
        f"- 关键阻力位：{res_txt} | 关键支撑位：{sup_txt}\n"
        f"- 前期结构：{structure}"
    )


def _render_detail_csv(frame: "KlineFrame", shown: int, detail_bars: int) -> str:
    """近期明细 CSV：最近 detail_bars 根，时间正序（1=最早，N=最新收盘）。

    类型/实体比/上影比/收盘位/近5突破复用 kline_features 的确定性计算
    （在全帧上下文上算好再切片，保证最老一根也有前置K线可比）。
    """
    n = min(detail_bars, shown)
    feats = compute_kline_geometry_features(frame)
    ema = frame.indicators.ema20
    atr = frame.indicators.atr14
    rows = list(zip(range(n), frame.bars[:n], feats[:n]))[::-1]  # 反转为正序
    lines = []
    for no, (i, bar, f) in enumerate(rows, start=1):
        lines.append(
            f"{no},{bar.high:.2f},{bar.low:.2f},{bar.close:.2f},"
            f"{bar.volume / 10000:.1f},"
            f"{_f2(ema[i]) if i < len(ema) else '-'},"
            f"{_f2(atr[i]) if i < len(atr) else '-'},{f.bar_type},"
            f"{_f2(f.body_ratio)},{_f2(f.upper_wick_ratio)},{_f2(f.close_position)},"
            f"{f.breakout_prev}"
        )
    return "\n".join(lines)


def build_light_screen_messages(
    frame: "KlineFrame",
    *,
    bar_limit: int = LIGHT_SCREEN_DEFAULT_BARS,
    system_prompt: str | None = None,
    user_template: str | None = None,
) -> list[dict[str, str]]:
    """构建初筛消息：Brooks 做多初筛 system + 远期背景摘要/近期明细 user。

    user 消息结构：品种/周期 → 前 (bar_limit-20) 根的程序提取摘要（区间极值、
    阻力/支撑、结构一句话）→ 最近 20 根正序紧凑 CSV（类型/实体比/上影比/
    收盘位/近5突破等确定性特征列）。

    system_prompt / user_template 非空时覆盖内置默认（GUI 批量窗口可编辑，
    持久化在 settings.batch）。user 模板占位符：{symbol}/{timeframe}/{shown}/
    {bg_count}/{background_block}/{detail_count}/{kline_table}；模板写了未知
    占位符或多余花括号时回退默认并告警——初筛的职责是省钱，不能让提示词
    编辑炸掉批量。
    """
    shown = min(bar_limit, len(frame.bars))
    detail_count = min(LIGHT_SCREEN_DETAIL_BARS, shown)
    fields = {
        "symbol": frame.symbol,
        "timeframe": frame.timeframe,
        "shown": shown,
        "bg_count": max(shown - detail_count, 0),
        "background_block": _render_background_summary(frame, shown, detail_count),
        "detail_count": detail_count,
        "kline_table": _render_detail_csv(frame, shown, detail_count),
    }
    template = user_template if user_template else LIGHT_SCREEN_USER_TEMPLATE
    try:
        user = template.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("自定义初筛 user 模板无效（%s），已回退默认", exc)
        user = LIGHT_SCREEN_USER_TEMPLATE.format(**fields)
    system = system_prompt if system_prompt else LIGHT_SCREEN_SYSTEM_PROMPT
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_light_screen_reply(content: str) -> dict[str, Any] | None:
    """解析初筛回复；容忍 ```json 围栏与前后杂文，失败返回 None。"""
    text = (content or "").strip()
    if not text:
        return None
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("pass"), bool):
        return None
    # direction 不再要求（新初筛只做多，无此字段）；旧记录/自定义模板带了也兼容
    obj.setdefault("setup", "")
    obj.setdefault("reason", "")
    try:
        obj["conviction"] = max(0, min(100, int(obj.get("conviction", 0))))
    except (TypeError, ValueError):
        obj["conviction"] = 0
    return obj


def run_light_screen(
    client: Any,
    frame: "KlineFrame",
    *,
    cancel_token: "CancelToken | None" = None,
    bar_limit: int = LIGHT_SCREEN_DEFAULT_BARS,
    thinking: bool = False,
    system_prompt: str | None = None,
    user_template: str | None = None,
) -> dict[str, Any]:
    """跑一次初筛调用，返回带 usage 的判定 dict；失败 fail-open 放行。

    system_prompt / user_template 非空时覆盖内置初筛提示词（见
    build_light_screen_messages）。

    返回结构：{"pass", "direction"(可缺省), "setup", "conviction", "reason",
              "usage_total": {prompt/completion/total_tokens}, "retries": n}
    """
    result: dict[str, Any] = {
        "pass": True,
        "direction": None,  # 新初筛只做多不输出方向；自定义模板带了则透传
        "setup": "",
        "conviction": 0,
        "reason": "",
        "usage_total": {},
        "retries": 0,
    }
    messages = build_light_screen_messages(
        frame,
        bar_limit=bar_limit,
        system_prompt=system_prompt,
        user_template=user_template,
    )
    usage_total: dict[str, int] = {}

    def _merge(reply: Any) -> None:
        u = getattr(reply, "usage", None)
        if u is None:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage_total[key] = usage_total.get(key, 0) + int(
                getattr(u, key, 0) or 0
            )

    last_error = ""
    for attempt in range(2):  # 初次 + 一次重试（重试时强调只输出 JSON）
        if cancel_token is not None and cancel_token.is_set():
            break
        msgs = messages
        if attempt:
            msgs = [
                messages[0],
                {
                    "role": "user",
                    "content": messages[1]["content"]
                    + "\n\n（上次回复无法解析。再次提醒：只输出一个 JSON 对象，"
                    "不要任何其他文字。）",
                },
            ]
            result["retries"] = attempt
        try:
            reply = client.stream_chat(
                msgs,
                on_reasoning_token=None,
                on_content_token=None,
                cancel_token=cancel_token,
                thinking=thinking,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("light screen call failed (attempt %d): %s", attempt + 1, exc)
            continue
        _merge(reply)
        parsed = parse_light_screen_reply(getattr(reply, "content", None))
        if parsed is not None:
            result.update(parsed)
            result["usage_total"] = usage_total
            return result
        last_error = "unparseable reply"

    # fail-open：解析/网络失败 → 放行进入完整分析
    if last_error:
        result["reason"] = f"（初筛失败已放行：{last_error}）"
    result["usage_total"] = usage_total
    return result


def run_light_screen_dual(
    client: Any,
    frame: "KlineFrame",
    *,
    cancel_token: "CancelToken | None" = None,
    bar_limit: int = LIGHT_SCREEN_DEFAULT_BARS,
    thinking: bool = False,
    system_prompt: str | None = None,
    user_template: str | None = None,
) -> dict[str, Any]:
    """双初筛共识：Brooks 主筛 + 复核官并行各跑一次，双通过才放行。

    两次调用共享同一份数据消息（user 内容相同、system 标准不同），
    线程并行执行不增加墙钟时间；token 约为单筛的 2 倍。合并语义：
    - pass = 主筛.pass 且 复核.pass（AND 门，更严）；
    - 任一方 fail-open（网络/解析失败按放行处理）时不否决另一方；
    - conviction 取两者较小值（短板决定，min_conviction 门槛随之更严）；
    - usage_total 为两路相加；双方明细存 "dual" 键（主筛 primary / 复核 review）。
    """
    from concurrent.futures import ThreadPoolExecutor

    def _run(sys_prompt: str | None) -> dict[str, Any]:
        return run_light_screen(
            client, frame,
            cancel_token=cancel_token, bar_limit=bar_limit,
            thinking=thinking, system_prompt=sys_prompt,
            user_template=user_template,
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lscreen") as ex:
        fut_a = ex.submit(_run, system_prompt)
        fut_b = ex.submit(_run, LIGHT_SCREEN_SYSTEM_PROMPT_B)
        a, b = fut_a.result(), fut_b.result()

    merged = {k: a.get(k) for k in ("pass", "direction", "setup", "conviction", "reason")}
    merged["pass"] = bool(a.get("pass") and b.get("pass"))
    if not a.get("pass"):
        merged["reason"] = a.get("reason") or "主初筛拒绝"
    elif not b.get("pass"):
        merged["reason"] = f"（复核拒：{b.get('reason') or ''}）"
    try:
        merged["conviction"] = min(
            int(a.get("conviction") or 0), int(b.get("conviction") or 0)
        )
    except (TypeError, ValueError):
        merged["conviction"] = 0
    merged["dual"] = {
        "primary": {k: a.get(k) for k in ("pass", "setup", "conviction", "reason")},
        "review": {k: b.get(k) for k in ("pass", "setup", "conviction", "reason")},
    }
    usage: dict[str, int] = {}
    for part in (a, b):
        for key, val in (part.get("usage_total") or {}).items():
            usage[key] = usage.get(key, 0) + int(val or 0)
    merged["usage_total"] = usage
    merged["retries"] = int(a.get("retries") or 0) + int(b.get("retries") or 0)
    return merged

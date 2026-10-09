"""轻量初筛：单次紧凑调用判断是否值得跑完整两阶段分析.

批量筛选 618 只自选时，完整两阶段分析每只约 16 万 token，其中约七成是
跨股相同的指令与策略文本。本模块用一个约 0.8 万 token 的单次调用先粗筛，
只有初筛通过的股票才进入完整管线。

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
from pa_agent.data.datetime_ts import format_epoch_for_display

if TYPE_CHECKING:
    from pa_agent.data.base import KlineFrame
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

LIGHT_SCREEN_DEFAULT_BARS = 60

LIGHT_SCREEN_SYSTEM_PROMPT = """\
你是A股K线初筛器。你的唯一任务：判断当前股票当前是否值得进入深度两阶段分析。\
你不做最终交易决策，不给出价格，只做「值不值得细看」的粗筛。

通过（"pass": true）——满足任一：
1. 存在可交易的方向性结构：趋势通道中的回调企稳（突破后回踩、H1/H2 类回调、\
二次入场），顺势方向存在近端入场点；
2. 正在发生强突破或强势 spike（大实体、创近期新高/新低、有跟随棒），且行情\
尚未明显走完；
3. 清晰震荡区间的边界附近出现明确的停顿/反转信号（区间交易机会）。

拒绝（"pass": false）——满足任一：
1. 铁丝网/窄幅重叠：多根小实体K线互相重叠，无方向无结构；
2. 无趋势也无区间的漂移行情：EMA20 走平、价格反复穿越 EMA20；
3. 趋势中段但价格远离任何回调位置，无近端入场点（只剩追高/追低风险）。

判断要点：
- EMA20 斜率与价格相对 EMA20 的位置判方向；近期 swing 高低点判通道/区间结构；
- K线序号：1=最新已收盘，序号越大越早；
- 边缘情况倾向通过（宁可多报不可漏报，深度分析会再过滤）。

只输出一个 JSON 对象，禁止输出 JSON 以外的任何内容：
{"pass": true, "direction": "bullish|bearish|neutral", \
"setup": "<一句话形态描述>", "conviction": <0-100 整数>, \
"reason": "<一句话理由>"}\
"""

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _fmt(v: float | None) -> str:
    try:
        return f"{v:.2f}" if v is not None and v == v else "-"  # None/NaN 防御
    except (TypeError, ValueError):
        return "-"


def _render_slim_kline_table(frame: "KlineFrame", limit: int) -> str:
    """紧凑 K 线表：与两阶段管线同源数据，列与精度略减。"""
    lines = [
        "序号 | 时间             | 开盘   | 最高   | 最低   | 收盘   | 阳阴 | 成交量   | EMA20  | ATR14",
        "----+----------------+-------+-------+-------+-------+------+---------+--------+-------",
    ]
    for i, bar in enumerate(frame.bars[:limit]):
        ema = frame.indicators.ema20[i]
        atr = frame.indicators.atr14[i]
        yang_yin = "阳" if bar.close > bar.open else ("阴" if bar.close < bar.open else "平")
        dt = format_epoch_for_display(bar.ts_open, short=True)
        lines.append(
            f"{bar.seq:<3} | {dt[:16]:<16} | {bar.open:<5.2f} | {bar.high:<5.2f} | "
            f"{bar.low:<5.2f} | {bar.close:<5.2f} | {yang_yin:<4} | {bar.volume:<7.0f} | "
            f"{_fmt(ema):<6} | {_fmt(atr)}"
        )
    return "\n".join(lines)


def _render_slim_feature_table(frame: "KlineFrame", limit: int) -> str:
    """精简几何特征表：保留单棒类型/实体/影线/收盘位置/EMA 关系/突破/跟随。"""
    lines = [
        "序号 | 类型        | 实体比 | 上影比 | 下影比 | 收盘位 | EMA关系 | 近5突破 | 后续",
        "----+------------+-------+-------+-------+-------+---------+--------+-----",
    ]
    for feat in compute_kline_geometry_features(frame, limit=limit):
        lines.append(
            f"{feat.seq:<3} | {feat.bar_type:<10} | {_fmt(feat.body_ratio):<5} | "
            f"{_fmt(feat.upper_wick_ratio):<5} | {_fmt(feat.lower_wick_ratio):<5} | "
            f"{_fmt(feat.close_position):<5} | {feat.ema_relation:<7} | "
            f"{feat.breakout_prev:<6} | {feat.follow_through_1_2}"
        )
    return "\n".join(lines)


def build_light_screen_messages(
    frame: "KlineFrame", *, bar_limit: int = LIGHT_SCREEN_DEFAULT_BARS
) -> list[dict[str, str]]:
    """构建初筛消息：紧凑系统提示 + 有限根数的 K 线/几何表。"""
    shown = min(bar_limit, len(frame.bars))
    user = (
        f"品种:{frame.symbol} 周期:{frame.timeframe} 已收盘K线:{shown} 根"
        f"（序号1=最新已收盘，序号越大越早）\n\n"
        f"## K线数据\n\n{_render_slim_kline_table(frame, shown)}\n\n"
        f"## 几何特征（程序预计算，客观辅助）\n\n"
        f"{_render_slim_feature_table(frame, shown)}\n\n"
        "请输出初筛 JSON。"
    )
    return [
        {"role": "system", "content": LIGHT_SCREEN_SYSTEM_PROMPT},
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
    obj.setdefault("direction", "neutral")
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
) -> dict[str, Any]:
    """跑一次初筛调用，返回带 usage 的判定 dict；失败 fail-open 放行。

    返回结构：{"pass", "direction", "setup", "conviction", "reason",
              "usage_total": {prompt/completion/total_tokens}, "retries": n}
    """
    result: dict[str, Any] = {
        "pass": True,
        "direction": "neutral",
        "setup": "",
        "conviction": 0,
        "reason": "",
        "usage_total": {},
        "retries": 0,
    }
    messages = build_light_screen_messages(frame, bar_limit=bar_limit)
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

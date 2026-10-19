"""本地预筛（免 token）：调用大模型前用确定性规则拒绝明显无意义的股票。

三类拒绝，全部在程序侧完成、不花一分 token：
1. 当天停盘：最新已收盘 K 线仍停留在之前的交易日（仅在当日交易时段结束后
   判定，早盘开盘前大家的最新K线都是昨天，不算停盘）；
2. 铁丝网：近 WIRE_BARS 根 K 线区间高度重叠且波幅小于 ATR（窄幅重叠无结构）；
3. 无趋势漂移：EMA20 斜率走平且价格反复穿越 EMA20（既非趋势也非可交易区间）。

阈值都是模块常量，宁可漏杀（多花一次初筛 token）不可误杀真信号——
被拒的股票直接标 screened_out，理由写入 reasoning，tokens_light=0。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, time as dtime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pa_agent.data.base import KlineFrame

logger = logging.getLogger(__name__)

# ── 阈值常量（调严 = 拒绝更多；调松 = 多花初筛 token）────────────────────
WIRE_BARS = 12           # 铁丝网检测窗口
WIRE_OVERLAP = 0.60      # 相邻K线区间重叠均值（重叠长度/并集）超过即疑似铁丝网
WIRE_RANGE_ATR = 1.20    # 且平均波幅需小于该倍数 ATR（排除大波动正常回调的重叠）

DRIFT_BARS = 20          # 漂移检测：穿越计数窗口
DRIFT_SLOPE_ATR = 0.30   # EMA20 近 SLOPE_BARS 根位移小于该倍数 ATR 视为走平
SLOPE_BARS = 10
DRIFT_CROSS_FLAT = 4     # EMA 走平时的穿越次数下限
DRIFT_CROSS_ALONE = 6    # 无论斜率，穿越次数达到即直接判漂移

# 停盘判定时刻：到了这个本地时间当天理应已有已收盘K线，最新仍非当日 → 停盘
_HALT_CHECK_FROM = {
    "1d": dtime(15, 10),   # 日线收盘后
}
_HALT_CHECK_FROM_INTRADAY = dtime(11, 40)  # 日内周期：上午至少收出第一根1h之后


def _halt_reason(frame: "KlineFrame", now: datetime) -> str:
    """当天停盘检测：最新已收盘K线的日期 < 今天，且现在已过了当日应出K线的时刻。

    局限（接受）：节假日跑批会被误判为全体停盘（本地无交易日历）；
    周末显式跳过。误判后果只是跳过分析、不产生错误数据。
    """
    if now.weekday() >= 5:  # 周末：所有人的最新K线都是周五，正常
        return ""
    bars = frame.bars
    if not bars:
        return ""
    ts = float(bars[0].ts_open)
    if ts <= 0:
        return ""
    if ts > 1e12:
        ts /= 1000.0
    # TDX 的 ts_open 是按北京时间直存的 naive 纪元（见 datetime_ts.format_epoch_for_display）
    bar_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    if bar_date >= now.date():
        return ""
    check_from = _HALT_CHECK_FROM.get(
        frame.timeframe, _HALT_CHECK_FROM_INTRADAY
    )
    if now.time() < check_from:
        return ""
    return f"当日停盘（最新K线停留在{bar_date.isoformat()}）"


def _wire_reason(frame: "KlineFrame") -> str:
    """铁丝网检测：近 WIRE_BARS 根相邻K线区间重叠均值高 + 平均波幅小于 ATR。"""
    bars = frame.bars[: WIRE_BARS + 1]
    if len(bars) < WIRE_BARS:
        return ""
    atr = frame.indicators.atr14[0] if frame.indicators.atr14 else math.nan
    if math.isnan(atr) or atr <= 0:
        return ""
    overlaps: list[float] = []
    ranges: list[float] = []
    for cur, prev in zip(bars, bars[1:]):  # bars 是 newest-first，相邻即时间相邻
        hi, lo = min(cur.high, prev.high), max(cur.low, prev.low)
        overlap = max(0.0, hi - lo)
        union = max(cur.high, prev.high) - min(cur.low, prev.low)
        if union <= 0:
            continue
        overlaps.append(overlap / union)
        ranges.append(union)
    if len(overlaps) < WIRE_BARS - 1:
        return ""
    mean_overlap = sum(overlaps) / len(overlaps)
    mean_range = sum(ranges) / len(ranges)
    if mean_overlap >= WIRE_OVERLAP and mean_range <= WIRE_RANGE_ATR * atr:
        return (
            f"铁丝网（近{len(overlaps) + 1}根区间重叠均值"
            f"{mean_overlap:.2f}、波幅{mean_range / atr:.1f}×ATR）"
        )
    return ""


def _drift_reason(frame: "KlineFrame") -> str:
    """无趋势漂移检测：EMA20 斜率走平 + 价格反复穿越 EMA20。"""
    bars = frame.bars[: DRIFT_BARS + 1]
    ema = frame.indicators.ema20
    atr = frame.indicators.atr14[0] if frame.indicators.atr14 else math.nan
    if (
        len(bars) < DRIFT_BARS
        or len(ema) < DRIFT_BARS
        or math.isnan(atr)
        or atr <= 0
    ):
        return ""
    slope_bars = frame.bars[: SLOPE_BARS + 1]
    if len(ema) < len(slope_bars):
        return ""
    # newest-first：ema[0]=最新。斜率 = |最新 - N根前| / ATR
    slope_atr = abs(ema[0] - ema[len(slope_bars) - 1]) / atr
    crosses = 0
    for i in range(len(bars) - 1):
        side_a = bars[i].close - ema[i]
        side_b = bars[i + 1].close - ema[i + 1]
        if side_a * side_b < 0:
            crosses += 1
    if crosses >= DRIFT_CROSS_ALONE or (
        slope_atr < DRIFT_SLOPE_ATR and crosses >= DRIFT_CROSS_FLAT
    ):
        return (
            f"无趋势漂移（EMA20近{SLOPE_BARS}根斜率{slope_atr:.2f}×ATR、"
            f"近{DRIFT_BARS}根穿越{crosses}次）"
        )
    return ""


def pre_gate_check(
    frame: "KlineFrame",
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """本地预筛总入口：返回 (是否放行, 拒绝原因)。

    任一规则命中即拒绝（原因字符串可直接展示）；全部通过返回 (True, "")。
    K 线/指标不足时跳过对应检测（放行）——预筛的职责是省钱，不是拦截面。
    """
    if now is None:
        now = datetime.now()
    for name, check in (
        ("停盘检测", lambda: _halt_reason(frame, now)),
        ("铁丝网检测", lambda: _wire_reason(frame)),
        ("漂移检测", lambda: _drift_reason(frame)),
    ):
        try:
            reason = check()
        except Exception:  # noqa: BLE001
            logger.exception("预筛%s异常，按放行处理", name)
            continue
        if reason:
            return False, reason
    return True, ""

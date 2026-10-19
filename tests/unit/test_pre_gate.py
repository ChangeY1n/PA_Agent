"""本地预筛（pre_gate）单测：停盘 / 铁丝网 / 无趋势漂移 三类拒绝 + 放行。"""
from __future__ import annotations

from datetime import datetime

from pa_agent.ai.pre_gate import pre_gate_check
from pa_agent.data.base import KlineBar
from pa_agent.data.snapshot import take_snapshot_from_bars

# 合成器约定：列表下标 0 = 最新（newest-first），与真实帧一致


def _frame_from(closes: list[float], *, ts0: float = 1_700_000_000_000,
                wiggle: float = 0.01, spread: float = 0.05):
    """按给定收盘序列（newest-first）造 1h 帧并计算指标。"""
    bars = []
    for i, c in enumerate(closes):
        o = c - wiggle
        bars.append(KlineBar(
            seq=i + 1,
            ts_open=ts0 + i * 3_600_000,
            open=o, high=max(o, c) + spread, low=min(o, c) - spread,
            close=c, volume=1000.0, closed=True,
        ))
    return take_snapshot_from_bars(bars, len(bars), "600001", "1h")


# ── 停盘 ───────────────────────────────────────────────────────────────────


def test_halted_detected_after_session():
    # 最新K线 2023-11-14（周二）；次日 13:00 仍停留在前一天 → 停盘
    frame = _frame_from([10 + i * 0.05 for i in range(30)][::-1])
    ok, reason = pre_gate_check(frame, now=datetime(2023, 11, 15, 13, 0))
    assert ok is False and "停盘" in reason and "2023-11-14" in reason


def test_halted_not_flagged_before_noon_or_same_day_or_weekend():
    frame = _frame_from([10 + i * 0.05 for i in range(30)][::-1])
    # 当天上午（第一根1h未收盘不算停盘）
    assert pre_gate_check(frame, now=datetime(2023, 11, 15, 9, 30))[0] is True
    # 当天下午但K线就是今天的
    assert pre_gate_check(frame, now=datetime(2023, 11, 14, 13, 0))[0] is True
    # 周末跑批：大家最新都是周五，正常
    assert pre_gate_check(frame, now=datetime(2023, 11, 18, 13, 0))[0] is True


# ── 铰丝网 / 漂移 / 放行 ──────────────────────────────────────────────────


def test_wire_rejects_tight_overlapping_bars():
    # 30 根几乎同价的十字线：区间完全重叠、波幅≈ATR → 铁丝网
    closes = [10.0 + (i % 3) * 0.002 for i in range(30)]
    ok, reason = pre_gate_check(_frame_from(closes), now=datetime(2023, 11, 14, 13, 0))
    assert ok is False and "铁丝网" in reason


def test_drift_rejects_flat_ema_repeated_crossings():
    # 大幅来回穿越走平的 EMA20（相邻K线跳空不重叠，避免先命中铁丝网）
    closes = [10.1 if i % 2 == 0 else 9.9 for i in range(30)]
    ok, reason = pre_gate_check(_frame_from(closes, spread=0.05),
                                now=datetime(2023, 11, 14, 13, 0))
    assert ok is False and "漂移" in reason


def test_trending_bars_pass():
    # 一路上扬：EMA20 斜率陡、无穿越、相邻K线不重叠 → 放行
    closes = [10 + i * 0.05 for i in range(30)][::-1]  # newest-first
    ok, reason = pre_gate_check(_frame_from(closes), now=datetime(2023, 11, 14, 13, 0))
    assert ok is True and reason == ""


def test_insufficient_bars_pass():
    # K线不足（如新上市股）：检测自动跳过，放行
    ok, _ = pre_gate_check(_frame_from([10.0, 10.1, 10.05]),
                           now=datetime(2023, 11, 14, 13, 0))
    assert ok is True

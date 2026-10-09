# -*- coding: utf-8 -*-
"""TDX 数据源 K 线 closed 标志回归测试.

背景：盘前（如 09:18）TDX 会预创建当日首根 K 线（标签时间为周期端点、
O=H=L=C=昨收、成交量为 0 的竞价伪 K 线）。若把它标记为已收盘，它会成为
AI 分析的 K1 并系统性带偏决策（批量筛选 10 只全部误判观望的根因）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("eltdx", reason="eltdx 未安装时跳过 TDX 测试")

import pa_agent.data.tdx_source as tdx_source
from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_display_frame


def _ms(cn_dt: datetime) -> float:
    return float(cn_dt.replace(tzinfo=tdx_source._CN_TZ).timestamp() * 1000)


def _bar(ts_ms: float, *, close: float = 8.0, volume: float = 100.0) -> dict:
    return {
        "ts_open": ts_ms,
        "open": close,
        "high": close + 0.05,
        "low": close - 0.05,
        "close": close,
        "volume": volume,
    }


@pytest.fixture
def preopen(monkeypatch):
    """固定“现在”为 2026-10-09（周五）09:18 盘前，非交易时间。"""
    now = datetime(2026, 10, 9, 9, 18)
    monkeypatch.setattr(tdx_source, "_cn_now", lambda: now)
    return now


def _bars_with_phantom() -> list[dict]:
    """oldest-first：昨日真实 1h 收线 K 线 + 今日盘前伪 K 线（V=0）。"""
    real = [
        _bar(_ms(datetime(2026, 10, 8, 13, 0))),
        _bar(_ms(datetime(2026, 10, 8, 14, 0))),
        _bar(_ms(datetime(2026, 10, 8, 15, 0))),  # 昨日收盘最后一根
    ]
    phantom = _bar(_ms(datetime(2026, 10, 9, 10, 30)), close=8.02, volume=0.0)
    return real + [phantom]


def test_preopen_phantom_head_bar_not_closed(preopen):
    """盘前伪 K 线（标签时间在未来）不得标记为已收盘。"""
    src = tdx_source.TDXSource()
    out = src._convert_to_kline_bars(_bars_with_phantom(), 4)
    assert out[0].closed is False, "盘前伪 K 线被标记为已收盘"
    assert out[1].closed is True, "昨日真实收盘 K 线应保持已收盘"


def test_after_close_head_bar_still_closed(monkeypatch):
    """收盘后（15:30）头部真实收线 K 线保持已收盘。"""
    monkeypatch.setattr(
        tdx_source, "_cn_now", lambda: datetime(2026, 10, 8, 15, 30)
    )
    src = tdx_source.TDXSource()
    bars = [
        _bar(_ms(datetime(2026, 10, 8, 14, 0))),
        _bar(_ms(datetime(2026, 10, 8, 15, 0))),
    ]
    out = src._convert_to_kline_bars(bars, 2)
    assert out[0].closed is True


def test_in_session_head_bar_not_closed(monkeypatch):
    """交易时间内头部 K 线保持未完成（原有行为不变）。"""
    monkeypatch.setattr(
        tdx_source, "_cn_now", lambda: datetime(2026, 10, 9, 10, 0)
    )
    src = tdx_source.TDXSource()
    out = src._convert_to_kline_bars(_bars_with_phantom(), 4)
    assert out[0].closed is False


def test_phantom_excluded_from_analysis_frame(preopen):
    """端到端：伪 K 线不得进入 AI 分析帧，K1 必须是昨日真实收线 K 线。"""
    from datetime import timedelta

    src = tdx_source.TDXSource()
    # 昨日 15:00 收线往前共 110 根真实 1h K 线（oldest-first）+ 盘前伪 K 线
    oldest = datetime(2026, 10, 8, 15, 0) - timedelta(hours=109)
    bars_oldest = [
        _bar(_ms(oldest + timedelta(hours=i))) for i in range(110)
    ]
    bars_oldest.append(
        _bar(_ms(datetime(2026, 10, 9, 10, 30)), close=8.02, volume=0.0)
    )
    bars = src._convert_to_kline_bars(bars_oldest, len(bars_oldest))

    now_ms = int(_ms(datetime(2026, 10, 9, 9, 18)))
    frame = build_display_frame(bars, 100, "600064", "1h", now_ms=now_ms)
    assert frame is not None
    assert len(frame.bars) == 100
    # K1 = 昨日 15:00 收线（而非 10:30 伪 K 线）
    assert frame.bars[0].volume > 0
    assert frame.bars[0].ts_open == _ms(datetime(2026, 10, 8, 15, 0))

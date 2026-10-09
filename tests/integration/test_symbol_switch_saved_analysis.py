# -*- coding: utf-8 -*-
"""Integration tests: R1 partial-input guard + R2 saved-analysis auto-show.

R1: 在 tdx/akshare 源输入残缺 6 位代码（如 "60"）不得触发订阅切换，
    也不得排队 500ms 防抖切换。
R2: 切换到有历史成功记录的品种时自动展示历史分析并跳转「实时」页；
    切到无记录品种时清空上一只股票的残留面板。

注意：MainWindow 会读写 config/settings.json（真实路径），测试必须先
monkeypatch SETTINGS_JSON_PATH 指向 tmp_path，防止污染真实配置。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# Guard: skip the whole module if PyQt6 is not available
pytest.importorskip("PyQt6")

from pa_agent.app_context import AppContext
from pa_agent.records.schema import AnalysisRecord, RecordMeta


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_record(symbol: str, timeframe: str) -> AnalysisRecord:
    """Successful analysis record with 30 closed bars (newest-first, ms ts)."""
    n = 30
    kline_data = [
        {
            "seq": i + 1,
            "ts_open": 1_700_000_000_000 - i * 3_600_000,
            "open": 2000.0 + (n - 1 - i) * 2.0,
            "high": 2010.0 + (n - 1 - i) * 2.0,
            "low": 1990.0 + (n - 1 - i) * 2.0,
            "close": 2005.0 + (n - 1 - i) * 2.0,
            "volume": 100.0,
            "closed": True,
        }
        for i in range(n)
    ]
    return AnalysisRecord(
        meta=RecordMeta(
            timestamp_local_iso="2026-01-02T10:00:00.000",
            timestamp_local_ms=1_700_000_000_000,
            symbol=symbol,
            timeframe=timeframe,
            bar_count=n,
            ai_provider={},
        ),
        kline_data=kline_data,
        htf_text="",
        stage1_messages=[],
        stage1_response=None,
        stage1_diagnosis={
            "cycle_position": "normal_channel",
            "direction": "bullish",
            "support_levels": ["1988-1992"],
            "resistance_levels": ["2060-2064"],
        },
        stage2_messages=[],
        stage2_response=None,
        stage2_decision={
            "decision": {
                "order_direction": "做多",
                "order_type": "突破单",
                "reasoning": "integration-test reasoning",
            },
            "diagnosis_summary": {
                "cycle_position": "normal_channel",
                "direction": "bullish",
            },
        },
        strategy_files_used=[],
        experience_loaded=[],
        exception=None,
        usage_total={},
    )


def _save_record(directory, name: str, record: AnalysisRecord) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(record.model_dump_json(), encoding="utf-8")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def records_dir(tmp_path, monkeypatch):
    """Isolated records/pending dir; GUI history lookups read from tmp_path."""
    pending = tmp_path / "pending"
    pending.mkdir()
    import pa_agent.records.analysis_history as analysis_history

    monkeypatch.setattr(analysis_history, "RECORDS_PENDING_DIR", pending)
    return pending


@pytest.fixture
def app_ctx():
    """Minimal AppContext with mocked data source (disconnected by default).

    ledger=None keeps _on_record_ready from building a real FreeChatSession.
    """
    ctx = AppContext()
    ctx.settings = None
    ds = MagicMock()
    ds._connected = False
    ds._symbol = "XAUUSDm"
    ds._timeframe = "15m"
    ds.supported_timeframes.return_value = [
        "1m", "5m", "15m", "30m", "1h", "4h", "1d"
    ]
    ctx.data_source = ds
    ctx.pending_writer = MagicMock()
    ctx.client = MagicMock()
    ctx.assembler = MagicMock()
    ctx.ledger = None
    return ctx


@pytest.fixture
def window(qtbot, app_ctx, tmp_path, monkeypatch, records_dir):
    """MainWindow with the real settings path redirected to tmp_path."""
    import pa_agent.config.paths as paths

    monkeypatch.setattr(paths, "SETTINGS_JSON_PATH", tmp_path / "settings.json")

    from pa_agent.gui.main_window import MainWindow

    win = MainWindow(ctx=app_ctx)
    qtbot.addWidget(win)
    return win


# ── R2: 历史分析自动展示 ──────────────────────────────────────────────────────


class TestSavedAnalysisAutoShow:
    def test_switch_to_analyzed_stock_shows_history(
        self, window, app_ctx, records_dir
    ):
        """切换到有成功记录的股票 → 面板加载历史分析并跳「实时」页。"""
        _save_record(
            records_dir,
            "2026-01-02_10-00-00_600519_1d.json",
            _make_record("600519", "1d"),
        )
        # 预置：停在别的标签页，徽章为空
        window._ai_sidebar._tabs.setCurrentIndex(5)  # 原始
        assert window._decision_badge.text() == ""

        window._on_symbol_or_tf_changed("600519", "1d")

        assert window._decision_badge.text() == "历史决策: 突破单"
        assert window._ai_sidebar._tabs.currentIndex() == 0  # 实时
        assert window._last_stage1_diagnosis is not None
        assert "已加载历史分析" in window._status_bar.currentMessage()

    def test_switch_to_fresh_stock_clears_panels(
        self, window, app_ctx, records_dir
    ):
        """先加载历史，再切到无记录股票 → 面板清空、无残留。"""
        _save_record(
            records_dir,
            "2026-01-02_10-00-00_600519_1d.json",
            _make_record("600519", "1d"),
        )
        window._on_symbol_or_tf_changed("600519", "1d")
        assert window._decision_badge.text().startswith("历史决策")

        window._on_symbol_or_tf_changed("000001", "1d")

        assert window._decision_badge.text() == ""
        assert window._last_stage1_diagnosis is None

    def test_startup_autoloads_last_symbol_history(
        self, qtbot, app_ctx, tmp_path, monkeypatch, records_dir
    ):
        """启动（事件循环首拍）即展示上次品种/周期的历史分析。"""
        import pa_agent.config.paths as paths

        monkeypatch.setattr(paths, "SETTINGS_JSON_PATH", tmp_path / "settings.json")

        # settings=None → 默认品种 XAUUSDm / 15m
        _save_record(
            records_dir,
            "2026-01-02_10-00-00_XAUUSDm_15m.json",
            _make_record("XAUUSDm", "15m"),
        )

        from pa_agent.gui.main_window import MainWindow

        win = MainWindow(ctx=app_ctx)
        qtbot.addWidget(win)

        qtbot.wait_until(
            lambda: win._decision_badge.text().startswith("历史决策"),
            timeout=3000,
        )
        assert win._ai_sidebar._tabs.currentIndex() == 0


# ── R1: 残缺输入守卫 ──────────────────────────────────────────────────────────


class TestPartialInputGuard:
    def test_partial_code_does_not_switch_subscription(self, window, app_ctx):
        """tdx 源输入 "60" → 不取消/重新订阅，状态栏提示完整代码。"""
        window._active_data_source_kind = "tdx"
        ds = app_ctx.data_source
        ds._connected = True

        window._on_symbol_or_tf_changed("60", "1d")

        ds.unsubscribe.assert_not_called()
        ds.subscribe.assert_not_called()
        assert "6 位" in window._status_bar.currentMessage()

    def test_partial_code_does_not_arm_debounce(self, window, app_ctx):
        """tdx 源输入残缺代码 → 500ms 防抖计时器不启动、无排队切换。"""
        window._active_data_source_kind = "tdx"

        window._symbol_combo.setCurrentText("60")

        assert not window._symbol_switch_timer.isActive()
        assert window._pending_symbol_switch is None

    def test_complete_code_arms_debounce(self, window, app_ctx):
        """输完整 6 位代码 → 防抖正常排队切换。"""
        window._active_data_source_kind = "tdx"

        window._symbol_combo.setCurrentText("600519")

        assert window._symbol_switch_timer.isActive()
        assert window._pending_symbol_switch == ("600519", "15m")
        window._symbol_switch_timer.stop()

    def test_same_subscription_switch_is_noop(self, window, app_ctx):
        """目标与当前订阅完全一致 → 直接返回（不进切换周期）。"""
        ds = app_ctx.data_source
        ds._connected = True

        window._on_symbol_or_tf_changed("XAUUSDm", "15m")

        ds.unsubscribe.assert_not_called()
        ds.subscribe.assert_not_called()

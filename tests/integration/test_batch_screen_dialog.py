"""批量自选筛选 GUI 集成测试：菜单入口 + 假 analyze_one 跑通窗口流程。

monkeypatch screen_runner.analyze_one 为无网络假实现，验证：
表格实时追加、汇总行、summary.json 落盘（tmp 目录）、设置持久化、
菜单可打开窗口。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")


def _fake_analyze_factory():
    """假 analyze_one：按代码尾号返回 机会/观望/淘汰 三类结果。"""

    def _fake(symbol, timeframe, bar_count, use_local_gate=True, *, mode="light",
              light_only=False, light_bars=60, long_only=True, min_conviction=0,
              dual_screen=True, cancel_token=None, system_prompt="", user_template=""):
        base = {
            "symbol": symbol, "timeframe": timeframe, "order_type": "",
            "order_direction": "", "entry_price": None, "stop_loss_price": None,
            "take_profit_price": None, "trade_confidence": None, "reasoning": "",
            "error": None, "tokens_light": 6000, "tokens_full": 0, "seconds": 0.1,
        }
        tail = int(symbol[-1]) % 3
        if tail == 0:
            base.update(status="opportunity", order_type="突破单",
                        order_direction="做多", entry_price=23.19,
                        stop_loss_price=22.27, take_profit_price=24.6,
                        trade_confidence=46, tokens_full=140000,
                        reasoning="紧凑通道顺势延续")
        elif tail == 1:
            base.update(status="screened_out",
                        light={"pass": True, "direction": "bearish",
                               "conviction": 58, "setup": "", "reason": ""},
                        reasoning="（只做多模式：初筛方向为空头）")
        else:
            base.update(status="no_order", light={"pass": True, "direction": "bullish",
                                                  "conviction": 65, "setup": "", "reason": ""})
        return base

    return _fake


def _make_dialog(qtbot, monkeypatch, tmp_path, symbols):
    from pa_agent.config.settings import Settings
    from pa_agent.gui.batch_screen_dialog import BatchScreenDialog
    from pa_agent.batch import screen_runner

    # 不发起真实 TDX 自选探测（测试直接注入 _symbols）
    monkeypatch.setattr(BatchScreenDialog, "_refresh_watchlist", lambda self: None)
    monkeypatch.setattr(screen_runner, "analyze_one", _fake_analyze_factory())

    picked: list[str] = []
    dlg = BatchScreenDialog(
        Settings(),
        get_timeframe=lambda: "1h",
        on_pick_symbol=picked.append,
        output_dir=str(tmp_path),
    )
    qtbot.addWidget(dlg)
    dlg._symbols = symbols
    dlg._watchlist_label.setText(f"自选：{len(symbols)} 只")
    return dlg, picked


def test_dialog_run_populates_table_and_summary(qtbot, monkeypatch, tmp_path):
    dlg, _ = _make_dialog(qtbot, monkeypatch, tmp_path,
                          ["600000", "600001", "600002"])
    dlg.show()

    dlg._on_start()
    qtbot.waitUntil(lambda: hasattr(dlg, "_last_summary"), timeout=10000)

    summary = dlg._last_summary
    assert summary["total"] == 3
    assert summary["opportunities"] == 1
    assert summary["screened_out"] == 1
    assert summary["no_order"] == 1
    assert summary["cancelled"] is False

    # 结果表 3 行，状态列带颜色标记
    assert dlg._table.rowCount() == 3
    texts = [dlg._table.item(r, 1).text() for r in range(3)]
    assert "★机会" in texts and "·淘汰" in texts and "—观望" in texts

    # 汇总行 + summary.json 落盘
    assert "机会 1" in dlg._summary_label.text()
    out_dir = Path(summary["out_dir"])
    data = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert data["total"] == 3
    assert data["min_conviction"] == 0
    assert dlg._open_dir_btn.isEnabled()


def test_dialog_start_persists_config(qtbot, monkeypatch, tmp_path):
    dlg, _ = _make_dialog(qtbot, monkeypatch, tmp_path, ["600001"])
    dlg._workers_spin.setValue(3)
    dlg._min_conviction_spin.setValue(55)
    dlg._light_bars_spin.setValue(40)

    dlg._on_start()
    qtbot.waitUntil(lambda: hasattr(dlg, "_last_summary"), timeout=10000)

    from pa_agent.config.settings import load_settings

    loaded = load_settings()
    assert loaded.batch.workers == 3
    assert loaded.batch.min_conviction == 55
    assert loaded.batch.light_bars == 40


def test_dialog_double_click_picks_symbol(qtbot, monkeypatch, tmp_path):
    dlg, picked = _make_dialog(qtbot, monkeypatch, tmp_path, ["600000"])
    dlg._on_start()
    qtbot.waitUntil(lambda: hasattr(dlg, "_last_summary"), timeout=10000)

    dlg._table.selectRow(0)
    dlg._on_cell_double_clicked(0, 0)
    assert picked == ["600000"]


def test_stop_cancels_and_keeps_partial_results(qtbot, monkeypatch, tmp_path):
    """停止：置 token 后未提交的不跑，已完成的结果照常写入。"""
    from pa_agent.batch import screen_runner
    from pa_agent.util.threading import CancelToken

    gate = {"token": None}

    def _slow(symbol, *args, cancel_token=None, **kwargs):
        gate["token"] = cancel_token
        # 模拟慢任务：等停止令牌（最多 2s）后带着 cancelled 痕迹返回
        deadline = time.monotonic() + 2.0
        while not (cancel_token and cancel_token.is_set()) and time.monotonic() < deadline:
            time.sleep(0.02)
        return {"symbol": symbol, "timeframe": "1h", "status": "no_order",
                "order_type": "", "order_direction": "", "entry_price": None,
                "stop_loss_price": None, "take_profit_price": None,
                "trade_confidence": None, "reasoning": "", "error": None,
                "light": None, "tokens_light": 100, "tokens_full": 0,
                "seconds": 0.1}

    monkeypatch.setattr(screen_runner, "analyze_one", _slow)

    from pa_agent.config.settings import Settings
    from pa_agent.gui.batch_screen_dialog import BatchScreenDialog

    monkeypatch.setattr(BatchScreenDialog, "_refresh_watchlist", lambda self: None)
    dlg = BatchScreenDialog(Settings(), get_timeframe=lambda: "1h",
                            on_pick_symbol=lambda s: None, output_dir=str(tmp_path))
    qtbot.addWidget(dlg)
    # workers=1 → 有界提交上限 4；8 只自选中其余 4 只在停止后不应被提交
    dlg._workers_spin.setValue(1)
    dlg._symbols = [f"60000{i}" for i in range(1, 9)]

    dlg._on_start()
    qtbot.wait_until(lambda: gate["token"] is not None, timeout=5000)
    dlg._start_btn.click()
    qtbot.waitUntil(lambda: hasattr(dlg, "_last_summary"), timeout=15000)

    summary = dlg._last_summary
    assert summary["cancelled"] is True
    # 只跑了在飞的部分（≤ 有界提交上限 4），其余未提交
    assert 1 <= summary["total"] <= 4


def test_main_window_menu_opens_dialog(qtbot, monkeypatch):
    from pa_agent.app_context import AppContext
    from pa_agent.config.settings import Settings
    from pa_agent.gui.batch_screen_dialog import BatchScreenDialog
    from pa_agent.gui.main_window import MainWindow

    monkeypatch.setattr(BatchScreenDialog, "_refresh_watchlist", lambda self: None)

    ctx = AppContext()
    ctx.settings = Settings()
    window = MainWindow(ctx)
    qtbot.addWidget(window)

    window._open_batch_screen_dialog()
    dlg = getattr(window, "_batch_screen_dialog", None)
    assert dlg is not None and dlg.isVisible()

    # 重复打开 raise 已有实例而非新建
    window._open_batch_screen_dialog()
    assert window._batch_screen_dialog is dlg

    dlg.close()

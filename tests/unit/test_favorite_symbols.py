"""收藏品种：★ 按钮切换 + 品种下拉列表置顶显示."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")


def _make_window(qtbot):
    from pa_agent.app_context import AppContext
    from pa_agent.config.settings import Settings
    from pa_agent.gui.main_window import MainWindow

    ctx = AppContext()
    ctx.settings = Settings()
    window = MainWindow(ctx)
    qtbot.addWidget(window)
    return window


def _set_symbol(window, symbol: str) -> None:
    """模拟用户输入品种（屏蔽 combo 信号，避免触发切换去抖定时器）."""
    window._symbol_combo.blockSignals(True)
    window._symbol_combo.setCurrentText(symbol)
    window._symbol_combo.blockSignals(False)
    window._update_favorite_button_state()


def test_toggle_favorite_pins_to_combo_top(qtbot):
    window = _make_window(qtbot)
    _set_symbol(window, "600519")

    assert window._fav_btn.text() == "☆"
    window._fav_btn.click()

    assert window._ctx.settings.general.favorite_symbols == ["600519"]
    assert window._fav_btn.text() == "★"
    assert window._symbol_combo.itemText(0) == "600519"

    # 已持久化（conftest 已把 SETTINGS_JSON_PATH 隔离到 tmp）
    from pa_agent.config.settings import load_settings

    assert load_settings().general.favorite_symbols == ["600519"]


def test_toggle_favorite_off(qtbot):
    window = _make_window(qtbot)
    _set_symbol(window, "600519")
    window._fav_btn.click()
    window._fav_btn.click()

    assert window._ctx.settings.general.favorite_symbols == []
    assert window._fav_btn.text() == "☆"


def test_multiple_favorites_keep_order_on_top(qtbot):
    window = _make_window(qtbot)
    window._ctx.settings.general.favorite_symbols = ["000001", "300750"]
    _set_symbol(window, "600519")
    window._fav_btn.click()

    combo = window._symbol_combo
    assert [combo.itemText(i) for i in range(3)] == ["000001", "300750", "600519"]

    # 切到已收藏品种时星标自动点亮
    _set_symbol(window, "300750")
    assert window._fav_btn.text() == "★"


def test_favorites_merge_above_source_symbols(qtbot):
    window = _make_window(qtbot)
    ds = MagicMock()
    ds._connected = True
    ds.list_symbols.return_value = ["601398", "600036"]
    window._ctx.data_source = ds
    window._ctx.settings.general.favorite_symbols = ["600519"]

    window._populate_symbol_combo_for_source()

    combo = window._symbol_combo
    assert combo.itemText(0) == "600519"
    assert combo.findText("600519") < combo.findText("601398")
    assert combo.findText("600519") < combo.findText("600036")

    # 收藏项带金色前景，与源符号区分
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor

    assert combo.itemData(0, Qt.ItemDataRole.ForegroundRole) == QColor("#e3b341")

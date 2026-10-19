"""多 API 配置方案：设置对话框的方案管理（切换/另存/删除/保存/取消）."""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")


def _make_settings():
    from pa_agent.config.settings import AIProviderSettings, Settings

    s = Settings()
    s.provider_profiles = {
        "DeepSeek": AIProviderSettings(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="sk-ds",
            reasoning_effort="high",
        ),
        "Packy": AIProviderSettings(
            model="claude-sonnet-4-6",
            base_url="https://www.packyapi.com/v1",
            api_key="sk-pk",
            reasoning_effort="max",
        ),
    }
    s.active_provider_name = "DeepSeek"
    # provider 与激活方案一致（模拟启动状态）
    s.provider = s.provider_profiles["DeepSeek"].model_copy()
    return s


def _make_dialog(qtbot, settings):
    from pa_agent.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    qtbot.addWidget(dlg)
    return dlg


def test_dialog_lists_profiles_and_selects_active(qtbot):
    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    names = [dlg._profile_combo.itemText(i) for i in range(dlg._profile_combo.count())]
    assert names == ["DeepSeek", "Packy"]
    assert dlg._selected_profile_name() == "DeepSeek"
    # 表单初值来自 settings.provider（当前生效配置）
    assert dlg._base_url_edit.text() == "https://api.deepseek.com"
    assert dlg._api_key_edit.text() == "sk-ds"


def test_switching_profile_loads_fields_into_form(qtbot):
    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    dlg._profile_combo.setCurrentIndex(dlg._profile_combo.findData("Packy"))

    assert dlg._base_url_edit.text() == "https://www.packyapi.com/v1"
    assert dlg._api_key_edit.text() == "sk-pk"
    assert dlg._model_edit.text() == "claude-sonnet-4-6"


def test_save_applies_selected_profile_and_persists(qtbot):
    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    dlg._profile_combo.setCurrentIndex(dlg._profile_combo.findData("Packy"))
    dlg._on_save()

    # provider（当前生效）切换为所选方案
    assert s.provider.api_key == "sk-pk"
    assert s.provider.model == "claude-sonnet-4-6"
    assert s.active_provider_name == "Packy"
    assert set(s.provider_profiles) == {"DeepSeek", "Packy"}

    # 持久化（conftest 已隔离 SETTINGS_JSON_PATH）
    from pa_agent.config.settings import load_settings

    reloaded = load_settings()
    assert reloaded.active_provider_name == "Packy"
    assert reloaded.provider.api_key == "sk-pk"


def test_save_as_new_profile(qtbot, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog

    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("备用通道", True))
    )
    dlg._on_profile_save_as()

    # 新方案由当前表单快照生成并选中
    assert dlg._selected_profile_name() == "备用通道"
    assert dlg._profiles["备用通道"].api_key == "sk-ds"

    dlg._on_save()
    assert set(s.provider_profiles) == {"DeepSeek", "Packy", "备用通道"}
    assert s.active_provider_name == "备用通道"


def test_delete_profile(qtbot, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    dlg._on_profile_delete()

    # 剩余第一个自动选中，表单同步切换
    assert dlg._selected_profile_name() == "Packy"
    assert dlg._base_url_edit.text() == "https://www.packyapi.com/v1"

    dlg._on_save()
    assert set(s.provider_profiles) == {"Packy"}

    # 仅剩一个方案时删除按钮禁用
    assert not dlg._profile_delete_btn.isEnabled()


def test_cancel_leaves_settings_untouched(qtbot):
    s = _make_settings()
    dlg = _make_dialog(qtbot, s)

    dlg._profile_combo.setCurrentIndex(dlg._profile_combo.findData("Packy"))
    dlg.reject()

    assert s.active_provider_name == "DeepSeek"
    assert s.provider.api_key == "sk-ds"
    assert set(s.provider_profiles) == {"DeepSeek", "Packy"}

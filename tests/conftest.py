# -*- coding: utf-8 -*-
"""全局测试隔离（autouse）。

任何测试——包括临时手写的 GUI 冒烟测试——都不得读写真实
``config/settings.json``。2026-10-12 曾因 scratch 测试创建 MainWindow
把真实配置覆盖成 pytest 临时路径（tdx_install_dir 指向已删除的
pytest-13 目录，导致客户端启动反复告警回退探测）。

``load_settings`` / ``save_settings`` 与 GUI 内部保存路径都在函数内
从 ``pa_agent.config.paths`` 重新导入 ``SETTINGS_JSON_PATH``，因此这里
只需 patch 模块常量即可全量生效。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """把 SETTINGS_JSON_PATH 指向每个测试独立的临时文件."""
    from pa_agent.config import paths

    monkeypatch.setattr(paths, "SETTINGS_JSON_PATH", tmp_path / "settings.json")

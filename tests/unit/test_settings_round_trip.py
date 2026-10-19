"""Unit tests for settings load/save round-trip (task 2.4)."""
from __future__ import annotations
import json
import pytest
from pathlib import Path
from pa_agent.config.settings import Settings, load_settings, save_settings


def test_defaults(tmp_path):
    """load_settings on a missing file returns defaults and creates the file."""
    p = tmp_path / "settings.json"
    s = load_settings(p)
    assert s.provider.model == "claude-sonnet-4-6"
    assert s.provider.base_url == "https://www.packyapi.com/v1"
    assert s.provider.thinking is True
    assert s.provider.reasoning_effort == "max"
    assert s.provider.context_window == 2_000_000
    assert s.general.analysis_bar_count == 100
    assert s.general.last_symbol == "XAUUSDm"
    assert s.general.last_timeframe == "15m"
    assert s.general.decision_stance == "balanced"
    assert s.general.decision_flow_auto_play is True
    assert s.general.auto_resume_chart_after_analysis is False
    assert p.exists(), "defaults should be written to disk"


def test_round_trip(tmp_path):
    """save → load preserves all fields."""
    p = tmp_path / "settings.json"
    original = Settings()
    original.provider.api_key = "sk-test-1234"
    original.general.last_symbol = "BTCUSDT"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.provider.api_key == "sk-test-1234"
    # Crypto symbols migrate to gold defaults on load
    assert loaded.general.last_symbol == "XAUUSDm"
    assert loaded.provider.model == original.provider.model


def test_api_key_present_on_disk(tmp_path):
    """The saved JSON contains the plaintext API key."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.provider.api_key = "sk-super-secret-key"
    save_settings(s, p)
    raw = p.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["provider"]["api_key"] == "sk-super-secret-key"


def test_corrupt_json_returns_defaults(tmp_path):
    """Corrupt settings.json falls back to defaults without raising."""
    p = tmp_path / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = load_settings(p)
    assert s.provider.model == "claude-sonnet-4-6"


def test_missing_api_key_leaves_api_key_blank(tmp_path):
    """If api_key is absent, api_key stays empty string."""
    p = tmp_path / "settings.json"
    data = Settings().model_dump()
    data["provider"].pop("api_key", None)
    data["provider"].pop("api_key_encrypted", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    s = load_settings(p)
    assert s.provider.api_key == ""


def test_batch_defaults(tmp_path):
    p = tmp_path / "settings.json"
    s = load_settings(p)
    assert s.batch.workers == 10
    assert s.batch.mode == "light"
    assert s.batch.long_only is True
    assert s.batch.light_only is False
    assert s.batch.dual_screen is True
    assert s.batch.light_bars == 80
    assert s.batch.min_conviction == 0
    assert s.batch.light_system_prompt == ""
    assert s.batch.light_user_template == ""


def test_batch_round_trip(tmp_path):
    """批量窗口参数与初筛提示词覆盖的 save → load 往返。"""
    p = tmp_path / "settings.json"
    original = Settings()
    original.batch.workers = 3
    original.batch.mode = "full"
    original.batch.long_only = False
    original.batch.light_only = True
    original.batch.dual_screen = False
    original.batch.light_bars = 40
    original.batch.min_conviction = 55
    original.batch.light_system_prompt = "自定义初筛 system 标准"
    original.batch.light_user_template = "品种 {symbol}\n{kline_table}"
    save_settings(original, p)
    loaded = load_settings(p)
    assert loaded.batch == original.batch


def test_batch_legacy_json_without_section_uses_defaults(tmp_path):
    """旧版 settings.json（无 batch 节）加载后用默认值，不报错。"""
    p = tmp_path / "settings.json"
    data = Settings().model_dump()
    data.pop("batch", None)
    p.write_text(json.dumps(data), encoding="utf-8")
    s = load_settings(p)
    assert s.batch.workers == 10
    assert s.batch.min_conviction == 0


def test_favorite_symbols_round_trip(tmp_path):
    """favorite_symbols persists through save/load, order preserved."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.general.favorite_symbols = ["600519", "000001", "300750"]
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded.general.favorite_symbols == ["600519", "000001", "300750"]


def test_favorite_symbols_cleaned_on_load(tmp_path):
    """Loader strips whitespace, drops empties, dedupes keeping order."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps({"general": {"favorite_symbols": [" 600519 ", "", "600519", "XAUUSDm"]}}),
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.general.favorite_symbols == ["600519", "XAUUSDm"]


def test_provider_profiles_round_trip(tmp_path):
    """Multiple named provider profiles persist with the active name."""
    from pa_agent.config.settings import AIProviderSettings

    p = tmp_path / "settings.json"
    s = Settings()
    s.provider_profiles = {
        "DeepSeek": AIProviderSettings(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key="sk-a",
        ),
        "Packy": AIProviderSettings(
            model="claude-sonnet-4-6",
            base_url="https://www.packyapi.com/v1",
            api_key="sk-b",
        ),
    }
    s.active_provider_name = "Packy"
    save_settings(s, p)
    loaded = load_settings(p)
    assert set(loaded.provider_profiles) == {"DeepSeek", "Packy"}
    assert loaded.provider_profiles["DeepSeek"].api_key == "sk-a"
    assert loaded.provider_profiles["Packy"].model == "claude-sonnet-4-6"
    assert loaded.active_provider_name == "Packy"


def test_provider_profiles_seeded_from_legacy_provider(tmp_path):
    """Legacy settings.json without profiles → seed「默认」from current provider."""
    p = tmp_path / "settings.json"
    s = Settings()
    s.provider.api_key = "sk-legacy"
    save_settings(s, p)
    # Strip profile fields to simulate a pre-profiles config file
    data = json.loads(p.read_text(encoding="utf-8"))
    data.pop("provider_profiles", None)
    data.pop("active_provider_name", None)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loaded = load_settings(p)
    assert list(loaded.provider_profiles) == ["默认"]
    assert loaded.provider_profiles["默认"].api_key == "sk-legacy"
    assert loaded.active_provider_name == "默认"


def test_dangling_active_provider_falls_back_to_first(tmp_path):
    """active_provider_name pointing at a removed profile falls back to first."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {
                "provider": {"api_key": "sk-x"},
                "provider_profiles": {
                    "A": {"api_key": "sk-a"},
                    "B": {"api_key": "sk-b"},
                },
                "active_provider_name": "gone",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(p)
    assert loaded.active_provider_name == "A"

"""初筛提示词覆盖单测：system/user 模板替换与坏模板回退。"""
from __future__ import annotations

from pa_agent.ai.light_screen import (
    LIGHT_SCREEN_SYSTEM_PROMPT,
    LIGHT_SCREEN_USER_TEMPLATE,
    build_light_screen_messages,
)


def _frame(n: int = 80):
    from pa_agent.data.snapshot import take_snapshot_from_bars
    from tests.unit.test_light_screen import _make_bars

    return take_snapshot_from_bars(_make_bars(n), min(n, 60), "600519", "1h")


class TestPromptOverride:
    def test_default_messages_unchanged(self):
        msgs = build_light_screen_messages(_frame(), bar_limit=80)
        assert msgs[0]["content"] == LIGHT_SCREEN_SYSTEM_PROMPT
        # 默认 user 仍含固定骨架、背景摘要与明细表头
        assert "品种:600519 | 周期:1h" in msgs[1]["content"]
        assert "## 远期背景" in msgs[1]["content"]
        assert "## 近期K线明细" in msgs[1]["content"]
        assert "请输出初筛 JSON。" in msgs[1]["content"]

    def test_system_prompt_override(self):
        msgs = build_light_screen_messages(
            _frame(), bar_limit=80, system_prompt="自定义初筛标准",
        )
        assert msgs[0]["content"] == "自定义初筛标准"
        assert msgs[1]["content"] == build_light_screen_messages(_frame(), bar_limit=80)[1]["content"]

    def test_user_template_override_with_placeholders(self):
        tpl = "股票 {symbol} ({timeframe}) 共{shown}根 背景{bg_count}根\n{background_block}\n{kline_table}\n给结论"
        msgs = build_light_screen_messages(_frame(), bar_limit=80, user_template=tpl)
        user = msgs[1]["content"]
        assert user.startswith("股票 600519 (1h) 共60根 背景40根")
        assert user.endswith("给结论")
        assert "## 近期K线明细" not in user  # 自定义骨架生效
        # 数据块仍被注入
        assert "- 区间极值：" in user
        assert "\n20," in user

    def test_bad_template_falls_back_to_default(self):
        # 未知占位符 / 多余花括号 → 回退默认模板，不抛异常
        msgs = build_light_screen_messages(
            _frame(), bar_limit=80, user_template="坏模板 {unknown_key} {{}}",
        )
        default_user = build_light_screen_messages(_frame(), bar_limit=80)[1]["content"]
        assert msgs[1]["content"] == default_user

    def test_default_template_format_safe(self):
        # 内置模板自身必须可 format（占位符拼写自洽）
        rendered = LIGHT_SCREEN_USER_TEMPLATE.format(
            symbol="600519", timeframe="1h", shown=80, bg_count=60,
            background_block="BG", detail_count=20, kline_table="KT",
        )
        assert "600519" in rendered and "BG" in rendered and "KT" in rendered

    def test_system_prompt_is_brooks_long_only(self):
        # 新初筛：Brooks 价格行为、只做多、先推理后结论的字段顺序
        assert "Al Brooks" in LIGHT_SCREEN_SYSTEM_PROMPT
        assert "做多（Bullish）" in LIGHT_SCREEN_SYSTEM_PROMPT
        assert "序号1为最早" in LIGHT_SCREEN_SYSTEM_PROMPT
        assert LIGHT_SCREEN_SYSTEM_PROMPT.rstrip().endswith(
            '{"setup": "<形态简述>", "reason": "<结合关键序号、EMA与前高阻力位的判断依据>", '
            '"conviction": <0-100整数>, "pass": true或false}'
        )

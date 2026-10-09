# -*- coding: utf-8 -*-
"""轻量初筛（light_screen）单元测试：解析容错、消息构建、fail-open 语义."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from pa_agent.ai.light_screen import (
    build_light_screen_messages,
    parse_light_screen_reply,
    run_light_screen,
)
from pa_agent.data.base import KlineBar


def _make_bars(n: int = 80, *, closed: bool = True) -> list[KlineBar]:
    """合成 newest-first 小时线：缓慢上行 + 微噪声，指标可正常计算。"""
    bars: list[KlineBar] = []
    base = 10.0
    for i in range(n):  # i 越大越早
        close = base + (n - i) * 0.05
        open_ = close - 0.02
        bars.append(
            KlineBar(
                seq=i + 1,  # 帧构建时会重排，占位即可
                ts_open=1_700_000_000_000 + i * 3_600_000,
                open=open_,
                high=max(open_, close) + 0.01,
                low=min(open_, close) - 0.01,
                close=close,
                volume=1000 + (i % 7) * 10,
                closed=closed,
            )
        )
    return bars


def _frame(n: int = 80):
    from pa_agent.data.snapshot import take_snapshot_from_bars

    return take_snapshot_from_bars(_make_bars(n), min(n, 60), "600001", "1h")


# ── parse_light_screen_reply ────────────────────────────────────────────────────


def test_parse_plain_json():
    r = parse_light_screen_reply(
        '{"pass": true, "direction": "bullish", "setup": "s", "conviction": 70, "reason": "r"}'
    )
    assert r == {
        "pass": True, "direction": "bullish", "setup": "s",
        "conviction": 70, "reason": "r",
    }


def test_parse_fenced_and_surrounding_text():
    r = parse_light_screen_reply(
        '好的，以下是判断：\n```json\n{"pass": false, "direction": "neutral",'
        ' "setup": "", "conviction": 10, "reason": "铁丝网"}\n```\n以上。'
    )
    assert r is not None and r["pass"] is False and r["reason"] == "铁丝网"


def test_parse_defaults_and_conviction_clamp():
    r = parse_light_screen_reply('{"pass": true}')
    assert r is not None
    assert r["direction"] == "neutral" and r["conviction"] == 0
    r2 = parse_light_screen_reply('{"pass": true, "conviction": 250}')
    assert r2["conviction"] == 100


def test_parse_rejects_garbage():
    assert parse_light_screen_reply("") is None
    assert parse_light_screen_reply("完全不是 JSON") is None
    assert parse_light_screen_reply('{"direction": "bullish"}') is None  # 缺 pass
    assert parse_light_screen_reply('{"pass": "yes"}') is None  # pass 非布尔


# ── build_light_screen_messages ─────────────────────────────────────────────────


def test_messages_compact_and_bar_limited():
    msgs = build_light_screen_messages(_frame(80), bar_limit=60)
    assert [m["role"] for m in msgs] == ["system", "user"]
    # 60 根 ×（K表+几何表）远小于完整两阶段 prompt
    total_chars = sum(len(m["content"]) for m in msgs)
    assert total_chars < 16_000, f"初筛 prompt 过大: {total_chars} chars"
    # 只包含前 60 根：最后一行行首是序号 60，且没有 61 开头的行
    assert "\n60 " in msgs[1]["content"]
    assert "\n61 " not in msgs[1]["content"]


# ── run_light_screen ───────────────────────────────────────────────────────────


def _reply(content: str, prompt: int = 500, completion: int = 60):
    return SimpleNamespace(
        content=content,
        reasoning_content="",
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            cached_prompt_tokens=0, total_tokens=prompt + completion,
        ),
    )


def test_run_light_screen_success_no_retry():
    client = MagicMock()
    client.stream_chat.return_value = _reply(
        '{"pass": false, "direction": "neutral", "conviction": 15,'
        ' "setup": "横盘", "reason": "无结构"}'
    )
    r = run_light_screen(client, _frame())
    assert r["pass"] is False and r["retries"] == 0
    assert r["usage_total"]["total_tokens"] == 560
    assert client.stream_chat.call_count == 1
    # 初筛默认关闭 thinking
    assert client.stream_chat.call_args.kwargs.get("thinking") is False


def test_run_light_screen_retry_then_success():
    client = MagicMock()
    client.stream_chat.side_effect = [
        _reply("我觉得这股票不太好"),  # 第一次不可解析
        _reply('{"pass": true, "direction": "bullish", "conviction": 80}'),
    ]
    r = run_light_screen(client, _frame())
    assert r["pass"] is True and r["retries"] == 1
    assert client.stream_chat.call_count == 2
    # 重试的那次必须强调只输出 JSON（messages 按位置传参）
    second_msgs = client.stream_chat.call_args_list[1].args[0]
    assert "只输出一个 JSON" in second_msgs[1]["content"]


def test_run_light_screen_fail_open_on_errors():
    client = MagicMock()
    client.stream_chat.side_effect = RuntimeError("gateway down")
    r = run_light_screen(client, _frame())
    # 两次都失败 → 放行进入完整分析（省钱的职责不允许漏报）
    assert r["pass"] is True
    assert "初筛失败已放行" in r["reason"]
    assert client.stream_chat.call_count == 2


def test_run_light_screen_unparseable_fail_open():
    client = MagicMock()
    client.stream_chat.return_value = _reply("not json at all")
    r = run_light_screen(client, _frame())
    assert r["pass"] is True and "初筛失败已放行" in r["reason"]

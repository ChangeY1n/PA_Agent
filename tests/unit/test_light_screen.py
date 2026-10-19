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
    assert "direction" not in r  # 新初筛只做多不输出方向，不再补默认值
    assert r["conviction"] == 0
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
    total_chars = sum(len(m["content"]) for m in msgs)
    assert total_chars < 8_000, f"初筛 prompt 过大: {total_chars} chars"
    user = msgs[1]["content"]
    # 结构：远期背景摘要 + 近 20 根正序明细，不再有 61 开头的明细行
    assert "## 远期背景" in user and "## 近期K线明细" in user
    assert "\n20," in user and "\n21," not in user
    assert "字段：序号,最高,最低,收盘,量(万),EMA20,ATR,类型,实体比,上影比,收盘位,近5突破" in user


def test_detail_csv_chronological_and_background_summary():
    """明细 CSV 时间正序（1=最早，20=最新）；背景摘要含极值/支撑阻力/结构。"""
    msgs = build_light_screen_messages(_frame(80), bar_limit=80)
    user = msgs[1]["content"]
    csv_lines = [l for l in user.splitlines() if l[:2].rstrip(",").isdigit()]
    assert len(csv_lines) == 20
    first = csv_lines[0].split(",")
    last = csv_lines[-1].split(",")
    assert first[0] == "1" and last[0] == "20"
    assert float(first[3]) < float(last[3])  # 收盘列：正序且一路上扬 → 1 行 < 20 行
    assert len(first) == 12 and len(last) == 12

    assert "- 区间极值：最高" in user and "距今" in user
    assert "关键阻力位" in user and "关键支撑位" in user
    assert "- 前期结构：" in user and "回升" in user  # 一路上扬 → 探底回升分支


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


# ── run_light_screen_dual（双初筛共识）────────────────────────────────────


def _dual_client(reply_a: str, reply_b: str | None = None):
    """按 system 提示词分流回复的假客户端（主筛/复核官各自独立判定）。"""
    from pa_agent.ai.light_screen import LIGHT_SCREEN_SYSTEM_PROMPT_B

    client = MagicMock()

    def _chat(msgs, **kwargs):
        if LIGHT_SCREEN_SYSTEM_PROMPT_B[:30] in msgs[0]["content"]:
            if reply_b is None:
                raise RuntimeError("review gateway down")
            return _reply(reply_b)
        return _reply(reply_a)

    client.stream_chat.side_effect = _chat
    return client


def test_dual_both_pass_merges_min_conviction_and_sums_usage():
    from pa_agent.ai.light_screen import run_light_screen_dual

    r = run_light_screen_dual(
        _dual_client(
            '{"pass": true, "setup": "回调企稳", "conviction": 72, "reason": "H2"}',
            '{"pass": true, "setup": "区间上沿", "conviction": 60, "reason": "位置合理"}',
        ),
        _frame(),
    )
    assert r["pass"] is True
    assert r["conviction"] == 60  # 取短板
    assert r["usage_total"]["total_tokens"] == 1120  # 两路相加（560×2）
    assert r["dual"]["primary"]["conviction"] == 72
    assert r["dual"]["review"]["conviction"] == 60


def test_dual_reviewer_rejects_blocks():
    from pa_agent.ai.light_screen import run_light_screen_dual

    r = run_light_screen_dual(
        _dual_client(
            '{"pass": true, "setup": "突破", "conviction": 70, "reason": "ok"}',
            '{"pass": false, "setup": "追高", "conviction": 30, "reason": "紧贴阻力"}',
        ),
        _frame(),
    )
    assert r["pass"] is False
    assert "复核拒" in r["reason"] and "紧贴阻力" in r["reason"]


def test_dual_primary_reject_reason_passes_through():
    from pa_agent.ai.light_screen import run_light_screen_dual

    r = run_light_screen_dual(
        _dual_client(
            '{"pass": false, "setup": "铁丝网", "conviction": 20, "reason": "重叠"}',
            '{"pass": true, "setup": "", "conviction": 66, "reason": ""}',
        ),
        _frame(),
    )
    assert r["pass"] is False and r["reason"] == "重叠"  # 主筛理由原样透传


def test_dual_fail_open_side_does_not_veto():
    from pa_agent.ai.light_screen import run_light_screen_dual

    # 复核官网络全挂 → fail-open 放行；主筛通过 → 共识通过（缺席不否决）
    r = run_light_screen_dual(
        _dual_client('{"pass": true, "setup": "x", "conviction": 65, "reason": "r"}'),
        _frame(),
    )
    assert r["pass"] is True and "放行" in r["dual"]["review"]["reason"]

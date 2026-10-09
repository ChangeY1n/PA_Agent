# -*- coding: utf-8 -*-
"""Integration test: local_gate 短路 — 阶段一本地规则命中时跳过阶段二模型调用."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt6")  # 仓库现有集成测试依赖一致的测试环境

from tests.fixtures.ai_payloads import VALID_STAGE1, VALID_STAGE2
from tests.integration.conftest import make_reply


@pytest.fixture
def orchestrator_deps():
    from tests.fixtures.validators import schema_test_validator
    from pa_agent.ai.router import route_strategy_files

    mock_client = MagicMock()
    call_count = [0]

    def chat_dispatch(messages, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return make_reply(VALID_STAGE1 if idx == 0 else VALID_STAGE2)

    mock_client.stream_chat.side_effect = chat_dispatch

    assembler = MagicMock()
    assembler.build_stage1.return_value = [{"role": "system", "content": "s1"}]
    assembler.build_stage2_continuation.return_value = [
        {"role": "system", "content": "s2"}
    ]
    exp_reader = MagicMock()
    exp_reader.read_top5.return_value = []
    pending_writer = MagicMock()

    deps = dict(
        client=mock_client,
        assembler=assembler,
        router=route_strategy_files,
        validator=schema_test_validator(),
        pending_writer=pending_writer,
        exp_reader=exp_reader,
        call_count=call_count,
    )
    return deps


def _submit(deps, local_gate):
    from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
    from pa_agent.util.threading import CancelToken
    from tests.integration.conftest import make_frame

    orch = TwoStageOrchestrator(
        client=deps["client"],
        assembler=deps["assembler"],
        router=deps["router"],
        validator=deps["validator"],
        pending_writer=deps["pending_writer"],
        exp_reader=deps["exp_reader"],
    )
    return orch.submit(
        make_frame(), CancelToken(), on_event=lambda evt: None,
        local_gate=local_gate,
    )


def test_local_gate_skips_stage2_api_call(orchestrator_deps):
    """local_gate 命中 → 只调用一次模型（阶段一），合成不下单结论并保存完整记录。"""
    deps = orchestrator_deps
    record = _submit(deps, local_gate=lambda s1: True)

    assert deps["call_count"][0] == 1, "阶段二模型调用未被跳过"
    assert record.stage2_response is None
    inner = (record.stage2_decision or {}).get("decision", {})
    assert inner.get("order_type") == "不下单"
    assert record.exception is None
    deps["pending_writer"].save_full.assert_called_once()


def test_local_gate_miss_runs_stage2(orchestrator_deps):
    """local_gate 未命中 → 阶段二正常调用（两次模型调用）。"""
    deps = orchestrator_deps
    record = _submit(deps, local_gate=lambda s1: False)

    assert deps["call_count"][0] == 2
    assert record.stage2_decision is not None


def test_no_local_gate_argument_keeps_default(orchestrator_deps):
    """不传 local_gate → 行为与未命中相同（默认完全不变）。"""
    deps = orchestrator_deps
    record = _submit(deps, local_gate=None)
    assert deps["call_count"][0] == 2
    assert record.stage2_decision is not None


def test_local_no_trade_gate_rule(monkeypatch):
    """批量脚本规则：仅在 router 零策略文件时拦截（neutral+spike 等）。"""
    import pa_agent.ai.router as router_mod
    import batch_watchlist_screen as bws

    loaded: list[str] = []
    monkeypatch.setattr(router_mod, "route_strategy_files", lambda s1: list(loaded))

    loaded.clear()
    assert bws.local_no_trade_gate(
        {"direction": "neutral", "cycle_position": "spike"}) is True
    # 有策略文件可加载的状态不得拦截（含 600333 反例：neutral+broad_channel
    # 曾给出限价单，breakout 类形态无法可靠区分，宁可不省）
    loaded[:] = ["strategies/文件13-窄通道与宽通道策略.txt"]
    assert bws.local_no_trade_gate(
        {"direction": "neutral", "cycle_position": "broad_channel"}) is False
    assert bws.local_no_trade_gate(
        {"direction": "bullish", "cycle_position": "normal_channel"}) is False
    # router 抛异常时守规矩放行（不拦）
    def _boom(s1):
        raise RuntimeError("router down")
    monkeypatch.setattr(router_mod, "route_strategy_files", _boom)
    assert bws.local_no_trade_gate(
        {"direction": "neutral", "cycle_position": "spike"}) is False

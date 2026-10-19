"""Integration test: transient network errors are auto-retried per stage.

Backoff is zeroed by the autouse ``_fast_network_retry`` fixture in conftest.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import openai
import pytest

from tests.fixtures.validators import schema_test_validator
from pa_agent.ai.deepseek_client import CancelledError
from pa_agent.ai.router import route_strategy_files
from pa_agent.orchestrator import two_stage
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.util.threading import CancelToken, OrchestratorEvent

from .conftest import VALID_STAGE1, VALID_STAGE2, make_reply


def _make_orchestrator(client, pending_writer, assembler, exp_reader):
    return TwoStageOrchestrator(
        client=client,
        assembler=assembler,
        router=route_strategy_files,
        validator=schema_test_validator(),
        pending_writer=pending_writer,
        exp_reader=exp_reader,
    )


def test_stage2_network_error_retry_then_success(
    frame, pending_writer, assembler, exp_reader
):
    """Stage2 stream drops mid-flight once → retry succeeds → full record saved."""
    client = MagicMock()
    client.stream_chat.side_effect = [
        make_reply(VALID_STAGE1),
        httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)"
        ),
        make_reply(VALID_STAGE2),
    ]

    orchestrator = _make_orchestrator(client, pending_writer, assembler, exp_reader)
    events: list[OrchestratorEvent] = []
    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=events.append,
    )

    assert OrchestratorEvent.Stage2NetworkRetry in events
    assert OrchestratorEvent.Stage1NetworkRetry not in events
    assert OrchestratorEvent.RecordSaved in events
    assert record.exception is None
    assert record.stage2_decision is not None
    assert client.stream_chat.call_count == 3
    pending_writer.save_full.assert_called_once()
    pending_writer.save_partial.assert_not_called()


def test_stage2_persistent_network_error_exhausts_retries(
    frame, pending_writer, assembler, exp_reader
):
    """Stage2 keeps failing → 1 attempt + 2 retries, then network_error partial."""
    client = MagicMock()
    read_err = httpx.ReadError("[WinError 10054] 远程主机强迫关闭了一个现有的连接。")
    client.stream_chat.side_effect = [make_reply(VALID_STAGE1)] + [
        read_err
    ] * two_stage.NETWORK_RETRY_MAX_ATTEMPTS

    orchestrator = _make_orchestrator(client, pending_writer, assembler, exp_reader)
    events: list[OrchestratorEvent] = []
    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=events.append,
    )

    assert OrchestratorEvent.Stage2Failed in events
    assert OrchestratorEvent.RecordSaved not in events
    assert events.count(OrchestratorEvent.Stage2NetworkRetry) == (
        two_stage.NETWORK_RETRY_MAX_ATTEMPTS - 1
    )
    assert record.exception == {
        "type": "network_error",
        "stage": "stage2",
        "message": str(read_err),
    }
    assert client.stream_chat.call_count == 1 + two_stage.NETWORK_RETRY_MAX_ATTEMPTS
    pending_writer.save_partial.assert_called_once()
    assert pending_writer.save_partial.call_args[0][1] == "network_error"
    pending_writer.save_full.assert_not_called()


def test_stage1_network_error_retry_then_success(
    frame, pending_writer, assembler, exp_reader
):
    """Stage1 stream drops once → retry succeeds → full pipeline completes."""
    client = MagicMock()
    client.stream_chat.side_effect = [
        httpx.ReadError("connection reset"),
        make_reply(VALID_STAGE1),
        make_reply(VALID_STAGE2),
    ]

    orchestrator = _make_orchestrator(client, pending_writer, assembler, exp_reader)
    events: list[OrchestratorEvent] = []
    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=events.append,
    )

    assert events.count(OrchestratorEvent.Stage1NetworkRetry) == 1
    assert OrchestratorEvent.Stage2NetworkRetry not in events
    assert OrchestratorEvent.RecordSaved in events
    assert record.stage2_decision is not None
    pending_writer.save_full.assert_called_once()
    pending_writer.save_partial.assert_not_called()


def test_cancelled_error_is_not_retried(frame, pending_writer, assembler, exp_reader):
    """CancelledError propagates immediately — no retry, no partial save."""
    client = MagicMock()
    client.stream_chat.side_effect = CancelledError("Request cancelled during streaming")

    orchestrator = _make_orchestrator(client, pending_writer, assembler, exp_reader)
    with pytest.raises(CancelledError):
        orchestrator.submit(
            frame=frame,
            cancel_token=CancelToken(),
            on_event=lambda evt: None,
        )

    assert client.stream_chat.call_count == 1
    pending_writer.save_partial.assert_not_called()
    pending_writer.save_full.assert_not_called()


def test_auth_error_is_not_retried(frame, pending_writer, assembler, exp_reader):
    """401 AuthenticationError is a network error but deterministic → no retry,
    recorded as network_error partial."""
    client = MagicMock()

    def _auth_error(*args, **kwargs):
        response = MagicMock()
        response.status_code = 401
        response.headers = {}
        raise openai.AuthenticationError(
            "Error code: 401 - Invalid API key", response=response, body=None
        )

    client.stream_chat.side_effect = _auth_error

    orchestrator = _make_orchestrator(client, pending_writer, assembler, exp_reader)
    events: list[OrchestratorEvent] = []
    record = orchestrator.submit(
        frame=frame,
        cancel_token=CancelToken(),
        on_event=events.append,
    )

    assert client.stream_chat.call_count == 1
    assert OrchestratorEvent.Stage1Failed in events
    assert OrchestratorEvent.Stage1NetworkRetry not in events
    pending_writer.save_partial.assert_called_once()
    assert pending_writer.save_partial.call_args[0][1] == "network_error"

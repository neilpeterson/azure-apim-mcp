"""Tests for the interactive Copilot efficiency observer."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


def _load_bench_module() -> ModuleType:
    script_path = Path(__file__).parents[1] / "scripts" / "bench" / "copilot_bench.py"
    spec = importlib.util.spec_from_file_location("copilot_bench", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _interaction_events() -> list[dict[str, object]]:
    return [
        {
            "type": "session.usage_checkpoint",
            "data": {"totalPremiumRequests": 2, "totalNanoAiu": 100},
            "timestamp": "2026-09-15T20:00:00Z",
        },
        {
            "type": "user.message",
            "data": {"interactionId": "interaction-1", "content": "List APIs"},
            "timestamp": "2026-09-15T20:01:00Z",
        },
        {
            "type": "tool.execution_start",
            "data": {"toolCallId": "call-1", "toolName": "AzureMCP-arm"},
            "timestamp": "2026-09-15T20:01:01Z",
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "call-1", "success": True},
            "timestamp": "2026-09-15T20:01:02Z",
        },
        {
            "type": "assistant.message",
            "data": {
                "model": "claude-sonnet-5",
                "content": "Found echo-api and hello-web.",
                "apiCallId": "api-call-1",
            },
            "timestamp": "2026-09-15T20:01:04Z",
        },
        {
            "type": "session.usage_checkpoint",
            "data": {
                "totalPremiumRequests": 3,
                "totalNanoAiu": 250,
                "promptCacheBreakState": [
                    {
                        "models": {
                            "claude-sonnet-5": {
                                "prompt_tokens": 1000,
                                "cache_read": 800,
                                "cache_write": 100,
                            }
                        },
                    }
                ],
            },
            "timestamp": "2026-09-15T20:01:05Z",
        },
    ]


def _parse_events(
    module: ModuleType,
    events: list[dict[str, object]],
    *,
    include_content: bool,
) -> list[dict[str, object]]:
    parser_class = cast(Callable[..., Any], module.SessionEventParser)
    parser = parser_class(
        session_id="session-1",
        label="azure-interactive",
        expectations=["echo-api", "hello-web"],
        include_content=include_content,
    )
    rows: list[dict[str, object]] = []
    for event in events:
        rows.extend(parser.consume_line(json.dumps(event) + "\n"))
    return rows


def test_parser_aggregates_interaction_without_content_by_default() -> None:
    module = _load_bench_module()

    rows = _parse_events(module, _interaction_events(), include_content=False)

    assert rows == [
        {
            "record_type": "interaction",
            "session_id": "session-1",
            "interaction_id": "interaction-1",
            "timestamp": "2026-09-15T20:01:00Z",
            "label": "azure-interactive",
            "wall_clock_s": 5.0,
            "tool_calls_total": 1,
            "tool_calls_by_name": {"AzureMCP-arm": 1},
            "tool_duration_ms": 1000,
            "prompt_tokens_snapshot": 1000,
            "cache_read_tokens_snapshot": 800,
            "cache_write_tokens_snapshot": 100,
            "premium_requests": 1,
            "nano_aiu": 150,
            "model_calls": 1,
            "model": "claude-sonnet-5",
            "prompt_chars": 9,
            "response_chars": 29,
            "expectations_met": True,
            "missing_expectations": [],
        }
    ]


def test_parser_includes_conversation_content_only_when_requested() -> None:
    module = _load_bench_module()

    rows = _parse_events(module, _interaction_events(), include_content=True)

    assert rows[0]["prompt"] == "List APIs"
    assert rows[0]["agent_response"] == "Found echo-api and hello-web."


def test_parser_reads_shutdown_totals() -> None:
    module = _load_bench_module()
    shutdown: dict[str, object] = {
        "type": "session.shutdown",
        "timestamp": "2026-09-15T23:12:43.215Z",
        "data": {
            "shutdownType": "routine",
            "totalPremiumRequests": 4,
            "totalNanoAiu": 28109040000,
            "totalApiDurationMs": 53988,
            "sessionStartTime": 1789513840892,
            "currentModel": "gpt-5.6-sol",
            "tokenDetails": {"input": {"tokenCount": 29978}},
            "modelMetrics": {
                "gpt-5.6-sol": {
                    "requests": {"count": 11, "cost": 4},
                    "usage": {
                        "inputTokens": 354074,
                        "outputTokens": 1577,
                        "cacheReadTokens": 324096,
                        "cacheWriteTokens": 0,
                        "reasoningTokens": 516,
                    },
                }
            },
        },
    }

    rows = _parse_events(module, [shutdown], include_content=False)

    assert rows[0]["record_type"] == "session_summary"
    assert rows[0]["tokens_in"] == 354074
    assert rows[0]["tokens_out"] == 1577
    assert rows[0]["tokens_cached"] == 324096
    assert rows[0]["reasoning_tokens"] == 516
    assert rows[0]["uncached_input_tokens"] == 29978
    assert rows[0]["ai_credits"] == 28.10904
    assert rows[0]["premium_requests"] == 4


def test_latest_session_prefers_the_only_active_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inactive = tmp_path / "session-state" / "with-events"
    active = tmp_path / "session-state" / "active"
    inactive.mkdir(parents=True)
    active.mkdir()
    (inactive / "events.jsonl").write_text("{}\n")
    (active / "inuse.123.lock").write_text("")

    module = _load_bench_module()
    monkeypatch.setattr(module, "_pid_is_running", lambda pid: pid == 123)
    find_latest = cast(Callable[[Path], Path | None], module.find_latest_session)

    assert find_latest(tmp_path) == active


def test_latest_session_chooses_newest_when_multiple_are_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "session-state"
    for name, pid in (("one", 123), ("two", 456)):
        session = session_root / name
        session.mkdir(parents=True)
        (session / f"inuse.{pid}.lock").write_text("")
    first = session_root / "one"
    second = session_root / "two"
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))

    module = _load_bench_module()
    monkeypatch.setattr(module, "_pid_is_running", lambda pid: True)
    find_latest = cast(Callable[[Path], Path | None], module.find_latest_session)

    assert find_latest(tmp_path) == second


def test_steering_message_extends_current_interaction() -> None:
    events = _interaction_events()
    events.insert(
        3,
        {
            "type": "user.message",
            "data": {
                "interactionId": "steering-1",
                "content": "Include revisions",
                "delivery": "steering",
            },
            "timestamp": "2026-09-15T20:01:01.500Z",
        },
    )
    module = _load_bench_module()

    rows = _parse_events(module, events, include_content=True)

    assert len(rows) == 1
    assert rows[0]["interaction_id"] == "interaction-1"
    assert rows[0]["prompt"] == "List APIs\nInclude revisions"
    assert rows[0]["tool_calls_total"] == 1
    assert rows[0]["wall_clock_s"] == 5.0


def test_missing_checkpoint_totals_invalidate_the_next_delta() -> None:
    events = _interaction_events()
    checkpoint = events[-1]
    checkpoint["data"] = {"promptCacheBreakState": []}
    events.extend(
        [
            {
                "type": "user.message",
                "data": {"interactionId": "interaction-2", "content": "List products"},
                "timestamp": "2026-09-15T20:02:00Z",
            },
            {
                "type": "session.usage_checkpoint",
                "data": {"totalPremiumRequests": 4, "totalNanoAiu": 400},
                "timestamp": "2026-09-15T20:02:05Z",
            },
        ]
    )
    module = _load_bench_module()

    rows = _parse_events(module, events, include_content=False)

    assert rows[0]["premium_requests"] is None
    assert rows[0]["nano_aiu"] is None
    assert rows[1]["premium_requests"] is None
    assert rows[1]["nano_aiu"] is None


def test_shutdown_flushes_an_interaction_without_a_checkpoint() -> None:
    module = _load_bench_module()
    events: list[dict[str, object]] = [
        {
            "type": "user.message",
            "data": {"interactionId": "interaction-1", "content": "List APIs"},
            "timestamp": "2026-09-15T20:01:00Z",
        },
        {
            "type": "assistant.message",
            "data": {"content": "Done", "apiCallId": "api-call-1"},
            "timestamp": "2026-09-15T20:01:04Z",
        },
        {
            "type": "session.shutdown",
            "data": {
                "totalPremiumRequests": 1,
                "totalNanoAiu": 1000000000,
                "modelMetrics": {},
            },
            "timestamp": "2026-09-15T20:01:05Z",
        },
    ]

    rows = _parse_events(module, events, include_content=False)

    assert [row["record_type"] for row in rows] == [
        "interaction",
        "session_summary",
    ]
    assert rows[0]["wall_clock_s"] == 5.0
    assert rows[0]["premium_requests"] == 1


def test_partial_event_line_is_retried() -> None:
    module = _load_bench_module()
    stream = io.StringIO('{"type":"user.message"')

    assert module._read_available_lines(stream) == []
    assert stream.tell() == 0


def test_resolve_session_rejects_ambiguous_prefix(tmp_path: Path) -> None:
    session_root = tmp_path / "session-state"
    (session_root / "abc-1").mkdir(parents=True)
    (session_root / "abc-2").mkdir()

    module = _load_bench_module()
    resolve_session = cast(Callable[[Path, str], Path], module.resolve_session)

    with pytest.raises(SystemExit, match="matched 2 sessions"):
        resolve_session(tmp_path, "abc")


def test_result_file_is_private_and_malformed_rows_are_ignored(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(
        '{"record_type":"interaction","session_id":"s0","interaction_id":"one"}\n{'
    )
    results_path.chmod(0o644)
    module = _load_bench_module()

    module._append_result(results_path, {"record_type": "session_summary", "session_id": "s1"})
    keys = module._load_result_keys(results_path)

    assert stat.S_IMODE(results_path.stat().st_mode) == 0o600
    assert ("interaction", None, "s0", "one") in keys
    assert ("session_summary", None, "s1", None) in keys
    assert "skipped malformed result line 2" in capsys.readouterr().err


def test_structurally_malformed_result_row_is_ignored(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    results_path = tmp_path / "results.jsonl"
    results_path.write_text('{"record_type":"interaction","label":[],"session_id":"s1"}\n')
    module = _load_bench_module()

    assert module._load_result_keys(results_path) == set()
    assert "skipped malformed result line 1" in capsys.readouterr().err


def test_result_file_rejects_symbolic_links(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("")
    link = tmp_path / "results.jsonl"
    link.symlink_to(target)
    module = _load_bench_module()

    with pytest.raises(SystemExit, match="symbolic-link"):
        module._append_result(link, {"record_type": "interaction"})


def test_poll_interval_must_be_positive() -> None:
    module = _load_bench_module()

    with pytest.raises(argparse.ArgumentTypeError, match="greater than zero"):
        module._positive_float("0")

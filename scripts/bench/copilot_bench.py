#!/usr/bin/env python3
"""Observe efficiency metrics from an existing interactive Copilot CLI session."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

TOOL_START_EVENT_TYPES = {"tool.execution_start", "tool.execution.start"}
TOOL_COMPLETE_EVENT_TYPES = {"tool.execution_complete", "tool.execution.complete"}
USAGE_CHECKPOINT_EVENT = "session.usage_checkpoint"
SHUTDOWN_EVENT = "session.shutdown"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _session_is_active(session_dir: Path) -> bool:
    for lock_path in session_dir.glob("inuse.*.lock"):
        parts = lock_path.name.split(".")
        if (
            len(parts) == 3
            and parts[1].isdigit()
            and int(parts[1]) > 0
            and _pid_is_running(int(parts[1]))
        ):
            return True
    return False


def find_latest_session(copilot_home: Path) -> Path | None:
    session_root = copilot_home / "session-state"
    if not session_root.exists():
        return None
    sessions = [path for path in session_root.iterdir() if path.is_dir()]
    active_sessions = [path for path in sessions if _session_is_active(path)]
    if active_sessions:
        candidates = active_sessions
    else:
        sessions_with_events = [path for path in sessions if (path / "events.jsonl").is_file()]
        candidates = sessions_with_events or sessions
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda path: (
            (path / "events.jsonl").stat().st_mtime
            if (path / "events.jsonl").is_file()
            else path.stat().st_mtime
        ),
    )


def resolve_session(copilot_home: Path, session: str) -> Path:
    if session == "latest":
        session_dir = find_latest_session(copilot_home)
        if session_dir is None:
            raise SystemExit(f"No Copilot sessions found under {copilot_home}")
        return session_dir

    session_root = copilot_home / "session-state"
    if not session_root.exists():
        raise SystemExit(f"No Copilot sessions found under {copilot_home}")
    candidates = [
        path for path in session_root.iterdir() if path.is_dir() and path.name.startswith(session)
    ]
    if len(candidates) != 1:
        raise SystemExit(f"Session prefix {session!r} matched {len(candidates)} sessions")
    return candidates[0]


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _elapsed_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        duration = _parse_timestamp(completed_at) - _parse_timestamp(started_at)
    except ValueError:
        return None
    return round(duration.total_seconds(), 3)


@dataclass
class Interaction:
    session_id: str
    interaction_id: str | None
    timestamp: str | None
    label: str
    prompt: str | None
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    tool_duration_ms: int = 0
    model_calls: int = 0
    model: str | None = None
    response: str | None = None

    def result(
        self,
        *,
        completed_at: str | None,
        premium_requests: int | None,
        nano_aiu: int | None,
        checkpoint_models: list[tuple[str, dict[str, Any]]],
        expectations: list[str],
        include_content: bool,
    ) -> dict[str, object]:
        response = self.response or ""
        missing = [
            expected for expected in expectations if expected.casefold() not in response.casefold()
        ]
        row: dict[str, object] = {
            "record_type": "interaction",
            "session_id": self.session_id,
            "interaction_id": self.interaction_id,
            "timestamp": self.timestamp,
            "label": self.label,
            "wall_clock_s": _elapsed_seconds(self.timestamp, completed_at),
            "tool_calls_total": sum(self.tool_calls_by_name.values()),
            "tool_calls_by_name": self.tool_calls_by_name,
            "tool_duration_ms": self.tool_duration_ms,
            "prompt_tokens_snapshot": None,
            "cache_read_tokens_snapshot": None,
            "cache_write_tokens_snapshot": None,
            "premium_requests": premium_requests,
            "nano_aiu": nano_aiu,
            "model_calls": self.model_calls,
            "model": self.model,
            "prompt_chars": len(self.prompt or ""),
            "response_chars": len(response),
            "expectations_met": not missing if expectations else None,
            "missing_expectations": missing,
        }
        if checkpoint_models:
            row["prompt_tokens_snapshot"] = sum(
                model_data.get("prompt_tokens", 0) or 0 for _, model_data in checkpoint_models
            )
            row["cache_read_tokens_snapshot"] = sum(
                model_data.get("cache_read", 0) or 0 for _, model_data in checkpoint_models
            )
            row["cache_write_tokens_snapshot"] = sum(
                model_data.get("cache_write", 0) or 0 for _, model_data in checkpoint_models
            )
            row["model"] = row["model"] or checkpoint_models[-1][0]
        if include_content:
            row["prompt"] = self.prompt
            row["agent_response"] = self.response
        return row


class SessionEventParser:
    """Incrementally convert Copilot events into benchmark result rows."""

    def __init__(
        self,
        *,
        session_id: str,
        label: str,
        expectations: list[str],
        include_content: bool,
    ) -> None:
        self.session_id = session_id
        self.label = label
        self.expectations = expectations
        self.include_content = include_content
        self.current: Interaction | None = None
        self.tool_starts: dict[str, tuple[str, str | None]] = {}
        self.previous_premium_requests = 0
        self.previous_nano_aiu = 0
        self.premium_baseline_valid = True
        self.nano_aiu_baseline_valid = True

    def consume_line(self, line: str) -> list[dict[str, object]]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []

        event_type = event.get("type", "")
        raw_data = event.get("data")
        data = raw_data if isinstance(raw_data, dict) else event
        timestamp = event.get("timestamp")
        timestamp = timestamp if isinstance(timestamp, str) else None

        if event_type == "user.message":
            interaction_id = data.get("interactionId")
            prompt = data.get("content")
            prompt = prompt if isinstance(prompt, str) else None
            if data.get("delivery") == "steering" and self.current is not None:
                if prompt:
                    existing_prompt = self.current.prompt or ""
                    self.current.prompt = (
                        f"{existing_prompt}\n{prompt}" if existing_prompt else prompt
                    )
                return []

            completed = []
            if self.current is not None:
                completed.append(
                    self.current.result(
                        completed_at=timestamp,
                        premium_requests=None,
                        nano_aiu=None,
                        checkpoint_models=[],
                        expectations=self.expectations,
                        include_content=self.include_content,
                    )
                )
            self.current = Interaction(
                session_id=self.session_id,
                interaction_id=(interaction_id if isinstance(interaction_id, str) else None),
                timestamp=timestamp,
                label=self.label,
                prompt=prompt,
            )
            self.tool_starts = {}
            return completed

        if event_type == USAGE_CHECKPOINT_EVENT:
            completed = self._complete_interaction(data, timestamp)
            return [completed] if completed is not None else []

        if event_type == SHUTDOWN_EVENT:
            rows = []
            completed = self._complete_interaction(data, timestamp)
            if completed is not None:
                rows.append(completed)
            rows.append(_session_summary(self.session_id, self.label, event, data))
            return rows

        if self.current is None:
            return []

        if event_type in TOOL_START_EVENT_TYPES:
            tool_call_id = data.get("toolCallId")
            tool_name = data.get("toolName") or data.get("mcpToolName")
            if isinstance(tool_call_id, str) and isinstance(tool_name, str):
                self.tool_starts[tool_call_id] = (tool_name, timestamp)
        elif event_type in TOOL_COMPLETE_EVENT_TYPES:
            self._complete_tool(data, timestamp)
        elif event_type == "assistant.message":
            content = data.get("content")
            if isinstance(content, str) and content:
                self.current.response = content
            if data.get("apiCallId"):
                self.current.model_calls += 1
            model = data.get("model")
            if self.current.model is None and isinstance(model, str):
                self.current.model = model
        return []

    def _complete_interaction(
        self,
        data: dict[str, Any],
        timestamp: str | None,
    ) -> dict[str, object] | None:
        (
            premium_requests,
            self.previous_premium_requests,
            self.premium_baseline_valid,
        ) = _counter_delta(
            data.get("totalPremiumRequests"),
            self.previous_premium_requests,
            self.premium_baseline_valid,
        )
        (
            nano_aiu,
            self.previous_nano_aiu,
            self.nano_aiu_baseline_valid,
        ) = _counter_delta(
            data.get("totalNanoAiu"),
            self.previous_nano_aiu,
            self.nano_aiu_baseline_valid,
        )
        result = None
        if self.current is not None:
            checkpoint_models = [
                (model_name, model_data)
                for state in data.get("promptCacheBreakState") or []
                if isinstance(state, dict)
                for model_name, model_data in (state.get("models") or {}).items()
                if isinstance(model_data, dict)
            ]
            result = self.current.result(
                completed_at=timestamp,
                premium_requests=premium_requests,
                nano_aiu=nano_aiu,
                checkpoint_models=checkpoint_models,
                expectations=self.expectations,
                include_content=self.include_content,
            )
            self.current = None
        return result

    def _complete_tool(
        self,
        data: dict[str, Any],
        timestamp: str | None,
    ) -> None:
        assert self.current is not None
        tool_call_id = data.get("toolCallId")
        tool_start = (
            self.tool_starts.pop(tool_call_id, None) if isinstance(tool_call_id, str) else None
        )
        tool_name = (
            data.get("toolName")
            or data.get("mcpToolName")
            or (tool_start[0] if tool_start else None)
            or "unknown"
        )
        tool_name = tool_name if isinstance(tool_name, str) else "unknown"
        self.current.tool_calls_by_name[tool_name] = (
            self.current.tool_calls_by_name.get(tool_name, 0) + 1
        )
        if tool_start and tool_start[1] and timestamp:
            duration = _elapsed_seconds(tool_start[1], timestamp)
            if duration is not None:
                self.current.tool_duration_ms += round(duration * 1000)


def _counter_delta(
    value: object,
    previous: int,
    baseline_valid: bool,
) -> tuple[int | None, int, bool]:
    if not isinstance(value, int | float):
        return None, previous, False
    current = int(value)
    if not baseline_valid:
        return None, current, True
    return max(current - previous, 0), current, True


def _session_summary(
    session_id: str,
    label: str,
    event: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, object]:
    raw_model_metrics = data.get("modelMetrics")
    model_metrics = raw_model_metrics if isinstance(raw_model_metrics, dict) else {}
    usage: list[dict[str, Any]] = []
    models: dict[str, dict[str, object]] = {}
    for model_name, raw_metrics in model_metrics.items():
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        raw_usage = metrics.get("usage")
        model_usage = raw_usage if isinstance(raw_usage, dict) else {}
        usage.append(model_usage)
        requests = metrics.get("requests")
        request_data = requests if isinstance(requests, dict) else {}
        models[str(model_name)] = {
            "requests": request_data.get("count"),
            "premium_request_cost": request_data.get("cost"),
            "tokens_in": model_usage.get("inputTokens"),
            "tokens_out": model_usage.get("outputTokens"),
            "tokens_cached": model_usage.get("cacheReadTokens"),
            "tokens_cache_write": model_usage.get("cacheWriteTokens"),
            "reasoning_tokens": model_usage.get("reasoningTokens"),
        }

    session_start_ms = data.get("sessionStartTime")
    shutdown_timestamp = event.get("timestamp")
    session_duration_s = None
    if isinstance(session_start_ms, int | float) and isinstance(shutdown_timestamp, str):
        try:
            started_at = datetime.fromtimestamp(session_start_ms / 1000, tz=UTC)
            session_duration_s = round(
                (_parse_timestamp(shutdown_timestamp) - started_at).total_seconds(),
                3,
            )
        except (OSError, OverflowError, ValueError):
            session_duration_s = None

    total_nano_aiu = data.get("totalNanoAiu", 0) or 0
    raw_token_details = data.get("tokenDetails")
    token_details = raw_token_details if isinstance(raw_token_details, dict) else {}
    raw_uncached_input = token_details.get("input")
    uncached_input = raw_uncached_input if isinstance(raw_uncached_input, dict) else {}
    return {
        "record_type": "session_summary",
        "session_id": session_id,
        "timestamp": shutdown_timestamp,
        "label": label,
        "shutdown_type": data.get("shutdownType"),
        "session_duration_s": session_duration_s,
        "api_duration_ms": data.get("totalApiDurationMs"),
        "premium_requests": data.get("totalPremiumRequests"),
        "nano_aiu": total_nano_aiu,
        "ai_credits": round(total_nano_aiu / 1_000_000_000, 5),
        "tokens_in": sum(item.get("inputTokens", 0) or 0 for item in usage),
        "tokens_out": sum(item.get("outputTokens", 0) or 0 for item in usage),
        "tokens_cached": sum(item.get("cacheReadTokens", 0) or 0 for item in usage),
        "tokens_cache_write": sum(item.get("cacheWriteTokens", 0) or 0 for item in usage),
        "reasoning_tokens": sum(item.get("reasoningTokens", 0) or 0 for item in usage),
        "uncached_input_tokens": uncached_input.get("tokenCount"),
        "model": data.get("currentModel"),
        "models": models,
    }


def _read_available_lines(stream: TextIO) -> list[str]:
    lines: list[str] = []
    while True:
        position = stream.tell()
        line = stream.readline()
        if not line:
            break
        if not line.endswith("\n"):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                stream.seek(position)
                break
        lines.append(line)
    return lines


def _result_key(
    row: dict[str, object],
) -> tuple[str, str | None, str, str | None] | None:
    raw_record_type = row.get("record_type") or "interaction"
    label = row.get("label")
    session_id = row.get("session_id")
    interaction_id = row.get("interaction_id")
    timestamp = row.get("timestamp")
    if (
        not isinstance(raw_record_type, str)
        or (label is not None and not isinstance(label, str))
        or not isinstance(session_id, str)
        or (interaction_id is not None and not isinstance(interaction_id, str))
        or (timestamp is not None and not isinstance(timestamp, str))
    ):
        return None
    identifier = interaction_id or timestamp
    if raw_record_type == "interaction" and identifier is None:
        return None
    return (
        raw_record_type,
        label,
        session_id,
        identifier if raw_record_type == "interaction" else None,
    )


def _load_result_keys(
    results_path: Path,
) -> set[tuple[str, str | None, str, str | None]]:
    if not results_path.exists():
        return set()
    if results_path.is_symlink():
        raise SystemExit(f"Refusing symbolic-link results path: {results_path}")
    metadata = results_path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"Results path is not a regular file: {results_path}")
    results_path.chmod(0o600)

    keys: set[tuple[str, str | None, str, str | None]] = set()
    for line_number, line in enumerate(
        results_path.read_text(errors="ignore").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(
                f"warning: skipped malformed result line {line_number}",
                file=sys.stderr,
            )
            continue
        if isinstance(row, dict):
            key = _result_key(row)
            if key is not None:
                keys.add(key)
                continue
        print(
            f"warning: skipped malformed result line {line_number}",
            file=sys.stderr,
        )
    return keys


def _append_result(results_path: Path, row: dict[str, object]) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    if results_path.is_symlink():
        raise SystemExit(f"Refusing symbolic-link results path: {results_path}")

    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(results_path, flags, 0o600)
    except OSError as error:
        raise SystemExit(f"Unable to open results file safely: {error}") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"Results path is not a regular file: {results_path}")
        os.fchmod(descriptor, 0o600)
        prefix = b""
        if metadata.st_size:
            os.lseek(descriptor, -1, os.SEEK_END)
            if os.read(descriptor, 1) != b"\n":
                prefix = b"\n"
        payload = prefix + (json.dumps(row) + "\n").encode()
        os.write(descriptor, payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def observe(args: argparse.Namespace) -> None:
    copilot_home = Path(args.copilot_home).expanduser().resolve()
    session_dir = resolve_session(copilot_home, args.session)
    events_path = session_dir / "events.jsonl"

    if not events_path.exists():
        if not args.follow:
            raise SystemExit(f"Session {session_dir.name} has not created events.jsonl yet")
        print(f"# session={session_dir.name} has no events; waiting for its first interaction")
        try:
            while not events_path.exists():
                time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\n-> stopped before the session produced any events")
            return

    results_arg = Path(args.results).expanduser()
    results_path = (
        results_arg if results_arg.is_absolute() else Path(args.base_dir).resolve() / results_arg
    )
    existing_keys = _load_result_keys(results_path)
    parser = SessionEventParser(
        session_id=session_dir.name,
        label=args.label,
        expectations=args.expect,
        include_content=args.include_content,
    )

    print(f"# observing session={session_dir.name} events={events_path}")
    try:
        with events_path.open(encoding="utf-8", errors="ignore") as events_file:
            initial_lines = _read_available_lines(events_file)
            skipped = 0
            for line in initial_lines:
                for row in parser.consume_line(line):
                    key = _result_key(row)
                    if key is None:
                        continue
                    if row["record_type"] == "session_summary":
                        if key not in existing_keys:
                            _append_result(results_path, row)
                            _print_session_summary(row)
                        print(f"-> session ended; results written to {results_path}")
                        return
                    if args.include_existing and key not in existing_keys:
                        _append_result(results_path, row)
                        existing_keys.add(key)
                        _print_interaction(row)
                    elif not args.include_existing and key not in existing_keys:
                        skipped += 1

            if skipped:
                print(
                    f"# skipped {skipped} existing interactions; "
                    "use --include-existing to import them"
                )

            if not args.follow:
                return

            while True:
                lines = _read_available_lines(events_file)
                if not lines:
                    time.sleep(args.poll)
                    continue
                for line in lines:
                    for row in parser.consume_line(line):
                        key = _result_key(row)
                        if key is None:
                            continue
                        if key in existing_keys:
                            continue
                        _append_result(results_path, row)
                        existing_keys.add(key)
                        if row["record_type"] == "session_summary":
                            _print_session_summary(row)
                            print(f"-> session ended; results written to {results_path}")
                            return
                        _print_interaction(row)
    except KeyboardInterrupt:
        print(f"\n-> stopped; appended interactions to {results_path}")


def _print_interaction(row: dict[str, object]) -> None:
    print(
        f"  interaction {row['interaction_id']}: "
        f"{row['wall_clock_s']}s tools={row['tool_calls_total']} "
        f"premium_requests={row['premium_requests']}"
    )


def _print_session_summary(row: dict[str, object]) -> None:
    print(
        f"  session summary: tokens_in={row['tokens_in']} "
        f"tokens_out={row['tokens_out']} cached={row['tokens_cached']} "
        f"ai_credits={row['ai_credits']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    observer = subparsers.add_parser(
        "observe",
        help="Observe an existing interactive Copilot session",
    )
    observer.add_argument("--base-dir", default=".")
    observer.add_argument("--results", default="interactive.bench-results.jsonl")
    observer.add_argument("--copilot-home", default="~/.copilot")
    observer.add_argument("--session", default="latest")
    observer.add_argument("--label", default="interactive")
    observer.add_argument(
        "--expect",
        action="append",
        default=[],
        help="Case-insensitive response marker; may be repeated",
    )
    observer.add_argument(
        "--include-content",
        action="store_true",
        help="Include full prompts and responses in the results file",
    )
    observer.add_argument(
        "--include-existing",
        action="store_true",
        help="Import interactions completed before the observer started",
    )
    observer.add_argument("--follow", action="store_true")
    observer.add_argument("--poll", type=_positive_float, default=2.0)
    observer.set_defaults(func=observe)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

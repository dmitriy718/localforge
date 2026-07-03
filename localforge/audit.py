from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, payload: dict[str, Any]) -> None:
        item = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "payload": _jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass(frozen=True)
class AuditEvent:
    event: str
    ts: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    run_dir: Path
    status: str
    started_at: str
    completed_at: str
    iterations: int
    tool_calls: int
    tool_failures: int
    protocol_errors: int
    invalid_event_lines: int
    final_preview: str


def list_run_summaries(runs_dir: Path, *, limit: int = 20) -> list[RunSummary]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not runs_dir.exists():
        return []
    run_dirs = sorted(
        [path for path in runs_dir.iterdir() if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    return [summarize_run(run_dir) for run_dir in run_dirs[:limit]]


def summarize_run(run_dir: Path) -> RunSummary:
    events_path = run_dir / "events.jsonl"
    events, invalid_lines = read_audit_events(events_path)
    started_at = ""
    completed_at = ""
    status = "missing-events"
    iterations = 0
    tool_calls = 0
    tool_failures = 0
    protocol_errors = 0
    final_preview = ""

    for item in events:
        if not started_at:
            started_at = item.ts
        completed_at = item.ts
        if item.event == "iteration_start":
            iterations += 1
        elif item.event == "tool_call":
            tool_calls += 1
        elif item.event == "tool_result":
            result = item.payload.get("result")
            if isinstance(result, dict):
                output = result.get("output")
                if result.get("ok") is False or (
                    isinstance(output, str) and output.lstrip().startswith("MCP error")
                ):
                    tool_failures += 1
        elif item.event == "protocol_error":
            protocol_errors += 1
        elif item.event == "run_final":
            status = "complete"
            final_preview = _preview(str(item.payload.get("final", "")))
        elif item.event == "run_incomplete":
            status = "incomplete"
            final_preview = _preview(str(item.payload.get("reason", "")))

    if events and status == "missing-events":
        status = "running-or-interrupted"
    if protocol_errors or tool_failures:
        status = f"{status}-with-errors" if status != "missing-events" else "error"

    return RunSummary(
        run_id=run_dir.name,
        run_dir=run_dir,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        iterations=iterations,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        protocol_errors=protocol_errors,
        invalid_event_lines=invalid_lines,
        final_preview=final_preview,
    )


def read_audit_events(path: Path) -> tuple[list[AuditEvent], int]:
    if not path.exists():
        return [], 0
    events: list[AuditEvent] = []
    invalid_lines = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(item, dict):
            invalid_lines += 1
            continue
        event = item.get("event")
        ts = item.get("ts")
        payload = item.get("payload", {})
        if not isinstance(event, str) or not isinstance(ts, str) or not isinstance(payload, dict):
            invalid_lines += 1
            continue
        events.append(AuditEvent(event=event, ts=ts, payload=payload))
    return events, invalid_lines


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _preview(value: str, *, limit: int = 140) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"

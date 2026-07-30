"""JSONL request logging for logs served directly by the application."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from utils import generate_id

REPOSITORY_ROOT = Path(__file__).resolve().parent


class LogBackend(Protocol):
    """Storage contract for request event logs."""

    def write(self, event: dict[str, Any]) -> None: ...

    @property
    def reference(self) -> str: ...


def _validate_run_id(run_id: str) -> str:
    """Reject path-like values before using a run ID as a filename."""
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty filename without path components")
    return run_id


def create_log(run_id: str | None = None, logs_dir: str | Path = "logs") -> Path:
    """Create and return a local ``logs/<run_id>.jsonl`` file."""
    resolved_run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{generate_id()}"
    directory = Path(logs_dir)
    if not directory.is_absolute():
        directory = REPOSITORY_ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_validate_run_id(resolved_run_id)}.jsonl"
    path.touch(exist_ok=True)
    return path


def append_event(log_path: str | Path, event: dict[str, Any]) -> None:
    """Append exactly one valid JSON object as a line in a JSONL log."""
    payload = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
    with Path(log_path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{payload}\n")


def finish_log(run_id: str) -> bool:
    """Complete a local log lifecycle; writes are already flushed per event."""
    _validate_run_id(run_id)
    return True


class JsonlLogBackend:
    """Append-only local JSONL backend served through FastAPI's ``/logs`` route."""

    def __init__(self, logs_dir: str, public_base_url: str = "", run_id: str | None = None) -> None:
        self.run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{generate_id()}"
        self.path = create_log(self.run_id, logs_dir)
        self.public_base_url = public_base_url.rstrip("/")

    @property
    def reference(self) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{self.run_id}.jsonl"
        return f"/logs/{self.run_id}.jsonl"

    def write(self, event: dict[str, Any]) -> None:
        append_event(self.path, event)

    def finish(self) -> bool:
        return finish_log(self.run_id)


class RunLogger:
    """Logger for the lifecycle of a single user request."""

    def __init__(self, backend: LogBackend) -> None:
        self.backend = backend

    @property
    def log_url(self) -> str:
        return self.backend.reference

    def event(self, event: str, details: dict[str, Any] | None = None, duration: float | None = None) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "details": details or {},
        }
        if duration is not None:
            payload["duration"] = round(duration, 4)
        self.backend.write(payload)

    def finish_log(self) -> bool:
        """Finish a local log without affecting the bot response."""
        finish = getattr(self.backend, "finish", None)
        return bool(finish()) if callable(finish) else True

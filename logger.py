"""Replaceable JSONL audit logging backend."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from utils import generate_id


class LogBackend(Protocol):
    """Storage contract for request event logs."""

    def write(self, event: dict[str, Any]) -> None: ...

    @property
    def reference(self) -> str: ...


class JsonlLogBackend:
    """Append-only local JSONL backend, one valid JSON object per line."""

    def __init__(self, logs_dir: str, public_base_url: str = "") -> None:
        directory = Path(logs_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{generate_id()}.jsonl"
        self.public_base_url = public_base_url.rstrip("/")

    @property
    def reference(self) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{self.path.name}"
        return f"/logs/{self.path.name}"

    def write(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


class RunLogger:
    """Logger for the lifecycle of a single user request."""

    def __init__(self, backend: LogBackend) -> None:
        self.backend = backend

    @property
    def log_url(self) -> str:
        return self.backend.reference

    def event(self, event: str, details: dict[str, Any] | None = None,
              duration: float | None = None) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "details": details or {},
        }
        if duration is not None:
            payload["duration"] = round(duration, 4)
        self.backend.write(payload)

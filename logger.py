"""JSONL request logging and safe Git publishing for public audit logs."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from utils import generate_id

LOGGER = logging.getLogger(__name__)
REPOSITORY_ROOT = Path(__file__).resolve().parent
_PUBLISH_LOCK = threading.Lock()


class LogBackend(Protocol):
    """Storage contract for request event logs."""

    def write(self, event: dict[str, Any]) -> None: ...

    @property
    def reference(self) -> str: ...


def _validate_run_id(run_id: str) -> str:
    """Reject path-like log identifiers before using one in the filesystem or Git."""
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a non-empty filename without path components")
    return run_id


def create_log(run_id: str | None = None, logs_dir: str | Path = "logs") -> Path:
    """Create and return ``logs/<run_id>.jsonl`` without overwriting its content."""
    resolved_run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%S}_{generate_id()}"
    filename = f"{_validate_run_id(resolved_run_id)}.jsonl"
    directory = Path(logs_dir)
    if not directory.is_absolute():
        directory = REPOSITORY_ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.touch(exist_ok=True)
    return path


def append_event(log_path: str | Path, event: dict[str, Any]) -> None:
    """Append one JSON object as one line to a JSONL log."""
    path = Path(log_path)
    payload = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{payload}\n")


def _run_git(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Git in the repository and capture output for diagnostics."""
    environment = os.environ.copy()
    github_token = environment.get("GITHUB_TOKEN")
    if github_token:
        authorization = base64.b64encode(f"x-access-token:{github_token}".encode("utf-8")).decode("ascii")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
    return subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


def _git_failure(command: tuple[str, ...], result: subprocess.CompletedProcess[str]) -> None:
    details = (result.stderr or result.stdout).strip()
    LOGGER.error("Git command failed (%s): %s", " ".join(command), details or f"exit code {result.returncode}")


def _default_branch() -> str | None:
    """Return origin's configured default branch, falling back to the checked-out branch."""
    remote_head = _run_git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head.returncode == 0:
        return remote_head.stdout.strip().removeprefix("origin/") or None
    branch = _run_git("branch", "--show-current")
    if branch.returncode == 0:
        return branch.stdout.strip() or None
    _git_failure(("branch", "--show-current"), branch)
    return None


def publish_log(run_id: str) -> bool:
    """Commit and push one log file, returning ``False`` instead of raising on Git errors."""
    try:
        return _publish_log(run_id)
    except (OSError, subprocess.SubprocessError, ValueError):
        LOGGER.exception("Unable to publish log %s", run_id)
        return False


def _publish_log(run_id: str) -> bool:
    """Perform the Git operations for :func:`publish_log`."""
    filename = f"{_validate_run_id(run_id)}.jsonl"

    log_path = REPOSITORY_ROOT / "logs" / filename
    if not log_path.is_file():
        LOGGER.error("Cannot publish missing log file: %s", log_path)
        return False

    with _PUBLISH_LOCK:
        branch = _default_branch()
        if not branch:
            LOGGER.error("Unable to determine the default branch for log publishing")
            return False

        synchronize = _run_git("pull", "--rebase", "origin", branch)
        if synchronize.returncode != 0:
            _git_failure(("pull", "--rebase", "origin", branch), synchronize)
            return False

        add = _run_git("add", "--force", "--", f"logs/{filename}")
        if add.returncode != 0:
            _git_failure(("add", "--force", "--", f"logs/{filename}"), add)
            return False

        staged = _run_git("diff", "--cached", "--quiet", "--", f"logs/{filename}")
        if staged.returncode == 0:
            push = _run_git("push", "origin", f"HEAD:refs/heads/{branch}")
            if push.returncode != 0:
                _git_failure(("push", "origin", f"HEAD:refs/heads/{branch}"), push)
                return False
            return True
        if staged.returncode != 1:
            _git_failure(("diff", "--cached", "--quiet", "--", f"logs/{filename}"), staged)
            return False

        for key, value in (
            ("user.name", "telegram-data-agent[bot]"),
            ("user.email", "telegram-data-agent[bot]@users.noreply.github.com"),
        ):
            configure = _run_git("config", key, value)
            if configure.returncode != 0:
                _git_failure(("config", key, value), configure)
                return False

        commit = _run_git("commit", "-m", f"Publish log {run_id}", "--", f"logs/{filename}")
        if commit.returncode != 0:
            _git_failure(("commit", "-m", f"Publish log {run_id}"), commit)
            return False

        push = _run_git("push", "origin", f"HEAD:refs/heads/{branch}")
        if push.returncode != 0:
            _git_failure(("push", "origin", f"HEAD:refs/heads/{branch}"), push)
            return False
    return True


def finish_log(run_id: str, logs_dir: str | Path = "logs") -> bool:
    """Finish a log lifecycle by publishing its completed JSONL file safely."""
    try:
        expected_path = REPOSITORY_ROOT / "logs" / f"{_validate_run_id(run_id)}.jsonl"
        if Path(logs_dir).resolve() != expected_path.parent.resolve():
            LOGGER.error("Log publishing only supports the repository logs directory")
            return False
        return publish_log(run_id)
    except (OSError, subprocess.SubprocessError, ValueError):
        LOGGER.exception("Unable to publish log %s", run_id)
        return False


class JsonlLogBackend:
    """Append-only local JSONL backend, publishing completed logs through Git."""

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
        try:
            return finish_log(self.run_id, self.path.parent)
        except (OSError, ValueError):
            LOGGER.exception("Unable to finish log %s", self.run_id)
            return False


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
        """Publish when the backend supports it; logging must never break the bot."""
        finish = getattr(self.backend, "finish", None)
        if not callable(finish):
            return True
        try:
            return bool(finish())
        except (OSError, ValueError):
            LOGGER.exception("Unable to finish request log")
            return False

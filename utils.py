"""Small, defensive parsing helpers."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

URL_PATTERN = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
CODE_BLOCK_PATTERN = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)```", re.DOTALL)


def generate_id() -> str:
    """Return a random, collision-resistant request identifier."""
    return uuid4().hex


def extract_urls(text: str) -> list[str]:
    """Extract HTTP(S) URLs, removing trailing sentence punctuation."""
    return [match.rstrip(".,;:!?") for match in URL_PATTERN.findall(text)]


def extract_code_blocks(text: str) -> list[str]:
    """Return fenced-code content from a message."""
    return [block.strip() for block in CODE_BLOCK_PATTERN.findall(text) if block.strip()]


def safe_json_loads(value: str) -> Any | None:
    """Parse JSON without propagating malformed user-input exceptions."""
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def looks_like_csv(value: str) -> bool:
    """Identify a plausible delimited table with a header and at least one row."""
    lines = [line for line in value.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:10]), delimiters=",\t;|")
    except csv.Error:
        return False
    return len(next(csv.reader([lines[0]], dialect))) > 1


def extract_inline_csv(text: str) -> str | None:
    """Find the first fenced or complete message body that resembles CSV/TSV."""
    candidates = extract_code_blocks(text) + [text]
    return next((candidate for candidate in candidates if looks_like_csv(candidate)), None)


def extract_inline_json(text: str) -> str | None:
    """Find the first fenced or complete message body containing JSON data."""
    candidates = extract_code_blocks(text) + [text]
    for candidate in candidates:
        parsed = safe_json_loads(candidate)
        if isinstance(parsed, (dict, list)):
            return candidate
    return None


def url_filename(url: str, default: str = "dataset") -> str:
    """Derive a safe simple filename from a URL path."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name or default


def csv_text_to_buffer(text: str) -> io.StringIO:
    """Return a StringIO buffer for pandas readers."""
    return io.StringIO(text.strip())

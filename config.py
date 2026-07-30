"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    bot_token: str = os.getenv("BOT_TOKEN", "")
    grok_api_key: str = os.getenv("GROK_API_KEY", "")
    grok_model: str = os.getenv("GROK_MODEL", "grok-4-latest")
    grok_base_url: str = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")
    public_base_log_url: str = os.getenv("PUBLIC_BASE_LOG_URL", "")
    port: int = int(os.getenv("PORT", "8000"))
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))
    max_download_bytes: int = int(os.getenv("MAX_DOWNLOAD_BYTES", "25000000"))
    temp_dir: str = os.getenv("TEMP_DIR", "temp")
    logs_dir: str = os.getenv("LOGS_DIR", "logs")


settings = Settings()

"""FastAPI webhook host for the Telegram data analyst bot."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from agent import DataAnalystAgent
from config import settings
from utils import generate_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
agent = DataAnalystAgent()
history: dict[int, list[dict[str, str]]] = defaultdict(list)
history_lock = asyncio.Lock()
telegram_app: Application | None = None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture a text message, retain per-chat context, and send one JSON response."""
    if not update.effective_chat or not update.effective_message:
        return
    chat_id = update.effective_chat.id
    message = update.effective_message
    user_text = message.text or message.caption or "Please analyze the attached dataset."
    dataset_path: Path | None = None
    if message.document:
        document = message.document
        if document.file_size and document.file_size > settings.max_download_bytes:
            response = {"answer": {"error": "Uploaded dataset exceeds configured size limit"}, "log_url": ""}
            await message.reply_text(json.dumps(response, separators=(",", ":")))
            return
        suffix = Path(document.file_name or "dataset").suffix.lower()
        if suffix not in {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".zip"}:
            response = {"answer": {"error": "Unsupported upload; use CSV, TSV, Excel, JSON, or ZIP"}, "log_url": ""}
            await message.reply_text(json.dumps(response, separators=(",", ":")))
            return
        dataset_path = Path(settings.temp_dir) / f"telegram_{generate_id()}{suffix}"
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=dataset_path)
    async with history_lock:
        prior = list(history[chat_id])
        conversation = (prior + [{"role": "user", "content": user_text}])[-10:]
    result = await agent.answer(conversation, user_text, dataset_path)
    response_text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    await update.effective_message.reply_text(response_text)
    async with history_lock:
        history[chat_id] = (conversation + [{"role": "assistant", "content": response_text}])[-10:]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global telegram_app
    if not settings.bot_token:
        logger.warning("BOT_TOKEN is not configured; webhook endpoint will reject updates")
        yield
        return
    telegram_app = Application.builder().token(settings.bot_token).build()
    telegram_app.add_handler(MessageHandler(filters.ALL, handle_message))
    await telegram_app.initialize()
    await telegram_app.start()
    try:
        yield
    finally:
        await telegram_app.stop()
        await telegram_app.shutdown()
        telegram_app = None


app = FastAPI(title="Telegram Data Analyst Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Container health endpoint."""
    return {"status": "ok"}


@app.get("/logs/{filename}")
async def get_log(filename: str) -> FileResponse:
    """Serve local logs when a public reverse proxy exposes this application."""
    if "/" in filename or "\\" in filename or not filename.endswith(".jsonl"):
        raise HTTPException(status_code=404, detail="Not found")
    path = Path(settings.logs_dir) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="application/x-ndjson")


@app.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    """Process a Telegram update delivered by Telegram's webhook service."""
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Bot is not configured")
    payload = await request.json()
    await telegram_app.process_update(Update.de_json(payload, telegram_app.bot))
    return {"ok": True}

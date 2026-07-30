# Telegram Data Analyst Agent

A webhook-based Telegram bot that ingests tabular data, produces auditable local analyses, and asks Groq for a JSON-only answer. Every Telegram reply is exactly one JSON object with `answer` and `log_url`.

## Installation

Requires Python 3.12.

```bash
git clone <your-repository-url> telegram-data-agent
cd telegram-data-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Set `BOT_TOKEN` (from BotFather) and `GROQ_API_KEY` in `.env`. `GROQ_MODEL` defaults to `llama-3.3-70b-versatile` and `GROQ_BASE_URL` defaults to `https://api.groq.com/openai/v1`.

## Telegram setup

1. Open [@BotFather](https://t.me/BotFather), create a bot with `/newbot`, and copy its token into `BOT_TOKEN`.
2. Deploy this service to an HTTPS URL.
3. Register the webhook, replacing the URL and token:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://your-domain/webhook"
```

## Run locally

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

For Telegram delivery, expose the port with a secure tunnel and register its HTTPS `/webhook` URL. Check `GET /health` for readiness. Local logs are exposed at `/logs/{filename}`; configure `PUBLIC_BASE_LOG_URL=https://your-domain/logs` so Telegram answers contain a reachable log URL.

## Public audit logs with GitHub Pages

Each completed Telegram request writes valid JSONL events to `logs/<run_id>.jsonl`. The bot then safely runs Git to commit and push that one file. Git errors are captured with Python logging and never interrupt a Telegram response. This runtime needs a clone with a configured `origin` remote and credentials that can push to the repository; local development can still run normally if publishing cannot authenticate.

The `Publish Logs to GitHub Pages` workflow runs whenever `logs/` changes (and can also be started manually). It synchronizes `logs/` into `docs/logs/` with stale-file deletion, then commits only when the public copy changed. The workflow uses the repository's default branch and `contents: write` permission.

Enable Pages once in GitHub: open **Settings** → **Pages**, choose **Deploy from a branch**, select the default branch, then select the **/docs** folder and save. Set the following value in `.env`, replacing the account and repository name if needed:

```text
PUBLIC_BASE_LOG_URL=https://YOUR_USERNAME.github.io/telegram-data-agent/logs
```

Published logs are then available at `https://YOUR_USERNAME.github.io/telegram-data-agent/logs/<run_id>.jsonl`. Logs can contain user messages and dataset-derived metadata, so use a private repository or remove sensitive values before enabling this public publishing flow.

## Data inputs and analysis

Messages can include CSV/TSV or JSON in a fenced block, public URLs for CSV, TSV, XLS/XLSX, JSON, or ZIP archives containing CSV/JSON, or an uploaded Telegram document in any of those formats. The bot profiles data, computes numeric summaries, optional correlation, common top/bottom rows and frequencies, and supplies the verified context to Groq. The `analysis` module also exposes safe DuckDB SELECT/WITH execution for application extensions.

Conversation state is retained per chat and limited to the latest 10 messages. JSONL logs capture `received`, `download`, `analysis`, LLM, and final-answer events.

## Docker

```bash
docker build -t telegram-data-agent .
docker run --env-file .env -p 8000:8000 telegram-data-agent
```

## Render deployment

Create a Web Service from the repository, choose Docker, add the variables from `.env.example`, and set `PUBLIC_BASE_LOG_URL` to `https://<service>.onrender.com/logs`. Once live, register `https://<service>.onrender.com/webhook` with Telegram.

## Google Cloud Run deployment

```bash
gcloud builds submit --tag gcr.io/PROJECT_ID/telegram-data-agent
gcloud run deploy telegram-data-agent --image gcr.io/PROJECT_ID/telegram-data-agent --region asia-south1 --allow-unauthenticated
```

Add the environment variables in Cloud Run, set `PUBLIC_BASE_LOG_URL` to the service `/logs` URL, then register the service `/webhook` with Telegram.

## Testing

```bash
python -m compileall app.py agent.py analysis.py logger.py utils.py config.py
curl http://localhost:8000/health
```

## Architecture

`Telegram -> FastAPI /webhook -> app conversation store -> DataAnalystAgent -> ingestion/analysis -> Groq -> JSON reply`

The agent is deliberately conservative: it never executes user-supplied Python or arbitrary SQL, rejects oversized downloads, and returns structured JSON errors for malformed datasets, download failures, and LLM failures.

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

## Logs served by the application

Each completed Telegram request writes valid JSONL events to `logs/<run_id>.jsonl`. FastAPI serves the file directly at `/logs/<run_id>.jsonl`; no GitHub Actions, Git commits, or GitHub Pages configuration is required. Configure the deployed service URL in `.env`:

```text
PUBLIC_BASE_LOG_URL=https://YOUR_RENDER_SERVICE.onrender.com/logs
```

The returned URL format is `https://YOUR_RENDER_SERVICE.onrender.com/logs/<run_id>.jsonl`. Logs can contain user messages and dataset-derived metadata, so do not share their URLs publicly unless that data is safe to expose. Render free services have an ephemeral filesystem, so logs disappear when the service restarts or spins down.

## Data inputs and analysis

Messages can include CSV/TSV or JSON in a fenced block, public URLs for CSV, TSV, XLS/XLSX, JSON, or ZIP archives containing CSV/JSON, or an uploaded Telegram document in any of those formats. The bot profiles data, computes numeric summaries, optional correlation, common top/bottom rows and frequencies, and supplies the verified context to Groq. The `analysis` module also exposes safe DuckDB SELECT/WITH execution for application extensions.

Conversation state is retained per chat and limited to the latest 10 messages. JSONL logs capture `received`, `download`, `analysis`, LLM, and final-answer events.

## Docker

```bash
docker build -t telegram-data-agent .
docker run --env-file .env -p 8000:8000 telegram-data-agent
```

## Render deployment

In the Render Dashboard, select **New** → **Web Service**, connect this repository, select the `main` branch, choose **Docker**, and select the **Free** instance type. Set the health-check path to `/health`, then add `BOT_TOKEN`, `GROQ_API_KEY`, and `PUBLIC_BASE_LOG_URL=https://<service>.onrender.com/logs`. Once live, register `https://<service>.onrender.com/webhook` with Telegram. The included `render.yaml` is optional and is not required for this manual free deployment.

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

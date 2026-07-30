"""Orchestration for ingestion, analysis, Groq reasoning, and final response."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from analysis import dataframe_profile, download_file, load_dataframe, run_pandas_analysis
from config import Settings, settings
from logger import JsonlLogBackend, RunLogger
from utils import extract_inline_csv, extract_inline_json, extract_urls, generate_id

SYSTEM_PROMPT = """You are an expert data analyst. Return exactly one valid JSON object and no markdown.
Use only the supplied conversation and dataset evidence. Never invent a dataset, values, columns, or computations.
Give concise, directly useful answers. If evidence is insufficient, return a JSON answer describing the limitation.
Never include a log_url field; the application adds the single log_url in its response envelope.
Pandas analysis and DuckDB-compatible analysis may be used conceptually, but do not claim execution not supported by evidence."""


class DataAnalystAgent:
    """Coordinates one data-analysis request."""

    def __init__(self, app_settings: Settings = settings) -> None:
        self.settings = app_settings
        Path(self.settings.temp_dir).mkdir(parents=True, exist_ok=True)
        Path(self.settings.logs_dir).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _groq_error_detail(response: httpx.Response) -> str:
        """Extract Groq's actionable error message without assuming a JSON body."""
        try:
            error = response.json().get("error", {})
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"]
        except (json.JSONDecodeError, ValueError):
            pass
        return response.text.strip() or response.reason_phrase

    @staticmethod
    def _parse_llm_json(content: str) -> Any:
        """Parse model output as JSON, tolerating markdown/code fences and leading text."""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            cleaned = re.sub(r"```(?:json)?\s*", "", content)
            cleaned = cleaned.replace("```", "")
            decoder = json.JSONDecoder()
            for match in re.finditer(r"[\{\[]", cleaned):
                try:
                    obj, _ = decoder.raw_decode(cleaned[match.start():])
                    return obj
                except json.JSONDecodeError:
                    continue
            raise

    async def _ask_groq(self, messages: list[dict[str, str]], context: dict[str, Any], logger: RunLogger) -> Any:
        if not self.settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        started = time.perf_counter()
        payload = {
            "model": self.settings.groq_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *messages,
                {"role": "system", "content": "Verified local analysis context: " + json.dumps(context, default=str)},
            ],
        }
        logger.event("llm_request", {"model": self.settings.groq_model})
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.groq_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.groq_api_key}"}, json=payload,
                )
                if response.status_code == httpx.codes.BAD_REQUEST:
                    fallback_payload = dict(payload)
                    fallback_payload.pop("response_format", None)
                    logger.event("llm_retry", {"reason": "Groq rejected JSON response mode"})
                    response = await client.post(
                        f"{self.settings.groq_base_url.rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.groq_api_key}"}, json=fallback_payload,
                    )
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_llm_json(content)
            if not isinstance(parsed, (dict, list, str, int, float, bool)) and parsed is not None:
                raise ValueError("Groq returned an unsupported JSON value")
            logger.event("llm_response", {"type": type(parsed).__name__}, time.perf_counter() - started)
            return parsed
        except httpx.HTTPStatusError as exc:
            detail = self._groq_error_detail(exc.response)
            logger.event("llm_response", {"status": exc.response.status_code, "error": detail}, time.perf_counter() - started)
            raise RuntimeError(f"Groq API request failed ({exc.response.status_code}): {detail}") from exc
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.event("llm_response", {"error": str(exc)}, time.perf_counter() - started)
            raise RuntimeError(f"LLM request failed: {exc}") from exc

    def _find_dataframe(self, message: str, logger: RunLogger) -> tuple[pd.DataFrame | None, str | None]:
        for url in extract_urls(message):
            try:
                started = time.perf_counter()
                path = download_file(url, self.settings.temp_dir, self.settings.max_download_bytes)
                logger.event("download", {"url": url, "path": path.name}, time.perf_counter() - started)
                return load_dataframe(path), f"download:{path.name}"
            except (ValueError, OSError, pd.errors.ParserError) as exc:
                logger.event("download", {"url": url, "error": str(exc)})
        inline_json = extract_inline_json(message)
        if inline_json:
            return load_dataframe(inline_json, inline=True), "inline_json"
        inline_csv = extract_inline_csv(message)
        if inline_csv:
            return load_dataframe(inline_csv, inline=True), "inline_csv"
        return None, None

    @staticmethod
    def _without_nested_log_url(answer: Any) -> Any:
        """Ensure the application response has exactly one, top-level ``log_url``."""
        if not isinstance(answer, dict):
            return answer
        sanitized = dict(answer)
        sanitized.pop("log_url", None)
        if set(sanitized) == {"answer"}:
            return sanitized["answer"]
        return sanitized

    @staticmethod
    def _deterministic_answer(question: str, analysis: dict[str, Any]) -> dict[str, Any] | None:
        """Return an answer directly from deterministic pandas analysis when possible."""
        q = question.lower()

        # Revenue / sales
        if "revenue" in analysis:
            revenue = analysis["revenue"]
            if any(word in q for word in ["revenue", "sales", "product"]):
                return {
                    "revenue_by_product": revenue["by_product"],
                    "highest_revenue_product": revenue["highest_product"],
                    "overall_revenue": revenue["overall_revenue"],
                }

        # Linear regression
        if "linear_regression" in analysis:
            lr = analysis["linear_regression"]

            # Predict next x value if requested
            if any(word in q for word in ["predict", "forecast"]):
                profile = analysis["profile"]
                x_name = lr["x"]

                # Find last observed x from sample/profile if available
                # Better: compute from dataframe before calling this.
                return {
                    "linear_regression": {
                        "slope": lr["slope"],
                        "intercept": lr["intercept"],
                        "r_squared": lr["r_squared"],
                    }
                }

        # Correlation
        if "correlation" in analysis and "correlation" in q:
            return {"correlation": analysis["correlation"]}

        return None

    async def answer(
        self,
        history: list[dict[str, str]],
        latest_message: str,
        dataset_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Produce the exact Telegram response envelope for one message."""
        request_id = generate_id()
        logger = RunLogger(JsonlLogBackend(self.settings.logs_dir, self.settings.public_base_log_url, request_id))
        logger.event("received", {"request_id": request_id, "history_messages": len(history)})
        try:
            if dataset_path is not None:
                frame = load_dataframe(dataset_path)
                source = f"telegram_upload:{Path(dataset_path).name}"
                logger.event("download", {"source": source, "method": "telegram_upload"})
            else:
                frame, source = self._find_dataframe(latest_message, logger)
            context: dict[str, Any] = {"dataset": None}
            if frame is not None:
                if frame.empty:
                    raise ValueError("Dataset is empty")
                started = time.perf_counter()
                context = {"dataset_source": source, "analysis": run_pandas_analysis(frame, latest_message)}
                logger.event("analysis", {"source": source, "rows": len(frame), "columns": len(frame.columns)}, time.perf_counter() - started)
            elif not history:
                context["note"] = "No dataset was provided in this message."
            analysis = context.get("analysis", {})

# Use deterministic pandas answer whenever possible
            deterministic = self._deterministic_answer(latest_message, analysis)

            if deterministic is not None:
                logger.event("deterministic_answer", {"used": True})
                result = {
                    "answer": deterministic,
                    "log_url": logger.log_url,
                }
            else:
                messages = [*history[-10:], {"role": "user", "content": latest_message}]
                answer = await self._ask_groq(messages, context, logger)

                result = {
                    "answer": self._without_nested_log_url(answer),
                    "log_url": logger.log_url,
                }
            logger.event("answer", {"success": True, "request_id": request_id})
            return result
        except Exception as exc:  # user-facing response must remain JSON even on failures
            logger.event("answer", {"success": False, "request_id": request_id, "error": str(exc)})
            return {"answer": {"error": str(exc)}, "log_url": logger.log_url}
        finally:
            logger.finish_log()

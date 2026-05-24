from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.services.workflow_jobs import DEFAULT_LIBRARY_NAME, normalize_library_name


def build_chat_completions_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/chat/completions"


def rewrite_ai_search_query(
    query: str,
    model: str | None,
    settings: Settings,
) -> tuple[str, str | None, str | None, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "mode": "raw_query",
        "requested_model": model,
    }
    api_base = (settings.writer_api_base or "").strip()
    api_key = (settings.writer_api_key or "").strip()
    if not api_base or not api_key:
        missing = []
        if not api_base:
            missing.append("writer_api_base")
        if not api_key:
            missing.append("writer_api_key")
        diagnostics["mode"] = "missing_configuration"
        diagnostics["missing_configuration"] = missing
        diagnostics["request_url"] = build_chat_completions_url(api_base) if api_base else None
        return query, "missing_configuration", None, diagnostics

    try:
        import httpx

        request_url = build_chat_completions_url(api_base)
        diagnostics["request_url"] = request_url
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an expert scientific literature search assistant. "
                    "Convert the user's natural language request into a precise academic search query. "
                    "Return only the rewritten query string without quotes or explanation."
                ),
            },
            {"role": "user", "content": query},
        ]
        with httpx.Client(timeout=settings.writer_timeout_seconds) as client:
            response = client.post(
                request_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": (model or "").strip() or settings.writer_model,
                    "messages": messages,
                    "temperature": 0.1,
                },
            )
        response.raise_for_status()
        data = response.json()
        rewritten = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        rewritten = rewritten.strip() if isinstance(rewritten, str) else ""
        if not rewritten:
            diagnostics["mode"] = "empty_response_fallback"
            return query, "fallback:empty_response", "LLM returned empty content", diagnostics
        diagnostics["mode"] = "live_llm"
        diagnostics["message_count"] = len(messages)
        return rewritten, "ok", None, diagnostics
    except Exception as exc:
        diagnostics["mode"] = "fallback"
        diagnostics["fallback_reason"] = type(exc).__name__
        logging.getLogger(__name__).warning("AI search query rewrite failed: %s", exc)
        return query, f"fallback:{type(exc).__name__}", str(exc), diagnostics

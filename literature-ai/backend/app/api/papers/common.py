from __future__ import annotations

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
        "mode": "disabled",
        "requested_model": model,
        "message": "Web-side AI query rewrite is disabled; use the raw query or IDE/MCP AI.",
    }
    return query, "disabled", None, diagnostics

from __future__ import annotations

import ast
import json
import re
from typing import Any


def compact_figure_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def figure_summary_meaningful_tokens(value: str | None) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", compact_figure_text(value).lower())
        if token not in {"fig", "figure"}
    ]


def figure_summary_echoes_caption(summary: str | None, caption: str | None) -> bool:
    summary_tokens = figure_summary_meaningful_tokens(summary)
    caption_tokens = figure_summary_meaningful_tokens(caption)
    if len(summary_tokens) < 8 or len(caption_tokens) < 8:
        return False
    summary_text = " ".join(summary_tokens)
    caption_text = " ".join(caption_tokens)
    if summary_text == caption_text:
        return True
    if summary_text.startswith(caption_text) or caption_text.startswith(summary_text):
        return True
    if len(summary_tokens) < max(10, int(len(caption_tokens) * 0.55)):
        return False
    summary_unique = set(summary_tokens)
    caption_unique = set(caption_tokens)
    extra_unique = summary_unique - caption_unique
    if len(summary_tokens) >= len(caption_tokens) * 2 and len(extra_unique) >= 6:
        return False
    overlap = len(summary_unique & caption_unique)
    return overlap / max(1, min(len(summary_unique), len(caption_unique))) >= 0.88


def normalize_figure_content_summary(summary: str | None, caption: str | None) -> str | None:
    summary_text = compact_figure_text(summary)
    if not summary_text:
        return None
    caption_text = compact_figure_text(caption)
    if not caption_text:
        return summary_text
    stripped = _strip_caption_prefix(summary_text, caption_text)
    if stripped is not None:
        summary_text = stripped
    return summary_text or None


def _strip_caption_prefix(summary_text: str, caption_text: str) -> str | None:
    summary_lower = summary_text.lower()
    caption_lower = caption_text.lower()
    if summary_lower == caption_lower:
        return None
    if not summary_lower.startswith(caption_lower):
        return summary_text
    remainder = summary_text[len(caption_text):].lstrip(" \t\r\n|:;,.-")
    return remainder or None


def normalize_figure_key_elements(value: Any) -> tuple[list[str] | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    parsed = _maybe_parse_key_elements_literal(value)
    detail = parsed if isinstance(parsed, dict) else None
    normalized = _normalize_key_elements_value(parsed)
    return normalized or None, detail


def flatten_figure_key_elements(value: Any) -> list[str]:
    normalized, _detail = normalize_figure_key_elements(value)
    return normalized or []


def _maybe_parse_key_elements_literal(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = compact_figure_text(value)
    if not text or text[0] not in "[{":
        return text
    for loader in (json.loads, ast.literal_eval):
        try:
            parsed = loader(text)
        except Exception:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return text


def _normalize_key_elements_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items: list[str] = []
        for nested in value:
            items.extend(_normalize_key_elements_value(_maybe_parse_key_elements_literal(nested)))
        return _dedupe_key_elements(items)
    if isinstance(value, dict):
        return _dedupe_key_elements(_flatten_key_element_value(value))
    text = compact_figure_text(value)
    return [text] if text else []


def _flatten_key_element_value(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"crop_issues", "visual_quality"}:
                continue
            if isinstance(nested, (dict, list)):
                items.extend(_flatten_key_element_value(nested))
            elif nested is not None:
                text = compact_figure_text(nested)
                if text and len(text) <= 240:
                    items.append(text)
    elif isinstance(value, list):
        for nested in value:
            items.extend(_flatten_key_element_value(nested))
    elif value is not None:
        text = compact_figure_text(value)
        if text and len(text) <= 240:
            items.append(text)
    return items


def _dedupe_key_elements(items: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        text = compact_figure_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
        if len(normalized) >= 16:
            break
    return normalized

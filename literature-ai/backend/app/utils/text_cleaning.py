from __future__ import annotations

from typing import Any


def normalize_text_tree(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [normalize_text_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_text_tree(item) for key, item in value.items()}
    return value


def repair_mojibake_text(text: str | None) -> str | None:
    if text is None or not isinstance(text, str):
        return text
    if not text:
        return text

    candidates = [text]
    if _looks_like_mojibake(text):
        for source_encoding in ("latin-1", "cp1252"):
            try:
                repaired = text.encode(source_encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            candidates.append(repaired)

    best = min(candidates, key=_mojibake_score)
    return best


def _looks_like_mojibake(text: str) -> bool:
    suspicious_tokens = ("Ã", "Â", "â", "ð", "€", "™", "œ", "�")
    return any(token in text for token in suspicious_tokens)


def _mojibake_score(text: str) -> tuple[int, int]:
    suspicious_tokens = ("Ã", "Â", "â", "ð", "€", "™", "œ", "�")
    suspicious_count = sum(text.count(token) for token in suspicious_tokens)
    replacement_count = text.count("\ufffd")
    return suspicious_count + (replacement_count * 2), len(text)

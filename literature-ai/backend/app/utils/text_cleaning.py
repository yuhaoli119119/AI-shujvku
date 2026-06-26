from __future__ import annotations

from typing import Any


def normalize_text_tree(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake_text(value)
    if isinstance(value, list):
        return [normalize_text_tree(item) for item in value]
    if isinstance(value, dict):
        normalized = {key: normalize_text_tree(item) for key, item in value.items()}
        journal = normalized.get("journal")
        if isinstance(journal, str):
            normalized["journal"] = repair_repeated_journal_title(journal)
        return normalized
    return value


def repair_repeated_journal_title(text: str | None) -> str | None:
    if text is None or not isinstance(text, str):
        return text
    compact = " ".join(text.split())
    if not compact:
        return compact
    words = compact.split(" ")
    if len(words) < 2 or len(words) % 2:
        return compact
    midpoint = len(words) // 2
    left = " ".join(words[:midpoint])
    right = " ".join(words[midpoint:])
    if left.casefold() == right.casefold():
        return left
    return compact


def repair_mojibake_text(text: str | None) -> str | None:
    if text is None or not isinstance(text, str):
        return text
    if not text:
        return text

    # Repair deterministic Unicode offset (e.g. Greek/Coptic block offsets for digits)
    # 0x0376 to 0x037F mapping to '0' through '9'
    digit_map = {chr(0x0376 + i): str(i) for i in range(10)}
    text = text.translate(str.maketrans(digit_map))

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
    suspicious_tokens = ("Ã", "Â", "â", "ð", "€", "™", "œ", "�", "Î", "Ï")
    return any(token in text for token in suspicious_tokens)


def _mojibake_score(text: str) -> tuple[int, int]:
    suspicious_tokens = ("Ã", "Â", "â", "ð", "€", "™", "œ", "�", "Î", "Ï")
    suspicious_count = sum(text.count(token) for token in suspicious_tokens)
    replacement_count = text.count("\ufffd")
    return suspicious_count + (replacement_count * 2), len(text)

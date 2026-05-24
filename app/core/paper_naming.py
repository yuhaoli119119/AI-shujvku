import re


def sanitize_filename_component(value: str, fallback: str = "paper") -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    text = re.sub(r'[<>:"/\\|?*]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        return fallback
    return text[:80]


def build_display_title(paper_number, chinese_title: str | None, title: str | None) -> str:
    prefix = f"[{int(paper_number):03d}] " if paper_number else ""
    original = (title or "").strip()
    zh = (chinese_title or "").strip()
    if zh and original and zh != original:
        return f"{prefix}{zh} / {original}"
    return prefix + (zh or original or "Untitled")

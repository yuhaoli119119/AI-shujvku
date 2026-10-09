from __future__ import annotations

from pathlib import Path

from app.config import Settings


class UnsafeLocalPDF(ValueError):
    pass


def _allowed_roots(settings: Settings) -> list[Path]:
    roots: list[Path] = []
    for raw in settings.local_ingest_roots.split(","):
        value = raw.strip()
        if value:
            roots.append(Path(value).expanduser().resolve())
    return roots


def validate_pdf_file(path: Path) -> Path:
    source = path.expanduser()
    if source.is_symlink():
        raise UnsafeLocalPDF("Symbolic-link PDF sources are not allowed")
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafeLocalPDF("PDF path does not exist") from exc
    if not resolved.is_file():
        raise UnsafeLocalPDF("PDF source must be a regular file")
    if resolved.suffix.lower() != ".pdf":
        raise UnsafeLocalPDF("PDF source must use the .pdf extension")
    try:
        with resolved.open("rb") as handle:
            magic = handle.read(5)
    except OSError as exc:
        raise UnsafeLocalPDF("PDF source cannot be read") from exc
    if magic != b"%PDF-":
        raise UnsafeLocalPDF("PDF source has an invalid PDF signature")
    return resolved


def validate_local_ingest_pdf(path: Path, settings: Settings) -> Path:
    resolved = validate_pdf_file(path)
    roots = _allowed_roots(settings)
    if not roots:
        raise UnsafeLocalPDF("No local PDF ingest roots are configured")
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise UnsafeLocalPDF("PDF path is outside configured local ingest roots")
    return resolved


def validate_local_ingest_directory(path: Path, settings: Settings) -> Path:
    folder = path.expanduser()
    if folder.is_symlink():
        raise UnsafeLocalPDF("Symbolic-link ingest directories are not allowed")
    try:
        resolved = folder.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafeLocalPDF("Folder path does not exist") from exc
    if not resolved.is_dir():
        raise UnsafeLocalPDF("Folder path is not a directory")
    roots = _allowed_roots(settings)
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise UnsafeLocalPDF("Folder path is outside configured local ingest roots")
    return resolved

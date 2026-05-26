from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.utils.active_database import get_active_database_info


ARTIFACT_FIELDS = ("pdf_path", "tei_path", "docling_json_path", "markdown_path")
UUID_ONLY_PDF_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.pdf$",
    re.IGNORECASE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_utc(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _db_referenced_paths(db_path: Path, library_root: Path) -> set[Path]:
    referenced: set[Path] = set()
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT pdf_path, tei_path, docling_json_path, markdown_path FROM papers"
        ).fetchall()
    finally:
        connection.close()

    for row in rows:
        for field in ARTIFACT_FIELDS:
            value = row[field]
            if value is None or not str(value).strip():
                continue
            raw = str(value).strip()
            candidates = [Path(raw)]
            if not Path(raw).is_absolute():
                candidates.append(library_root / raw)
            for candidate in candidates:
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.exists():
                    referenced.add(resolved)
                    break
    return referenced


def _papers_total(db_path: Path) -> int:
    connection = sqlite3.connect(str(db_path))
    try:
        row = connection.execute("SELECT COUNT(*) FROM papers").fetchone()
        return int(row[0] or 0)
    finally:
        connection.close()


def build_report(*, cleanup: bool = False, apply: bool = False) -> dict[str, Any]:
    info = get_active_database_info()
    active_db = Path(str(info["active_library_db_path"] or info["effective_db_path"])).resolve()
    library_root = active_db.parent.resolve()
    pdf_dir = library_root / "storage" / "pdf"
    referenced_paths = _db_referenced_paths(active_db, library_root)
    pdf_files = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.exists() else []

    unreferenced: list[dict[str, Any]] = []
    tiny_uuid_unreferenced: list[dict[str, Any]] = []
    for path in pdf_files:
        resolved = path.resolve()
        referenced = resolved in referenced_paths
        item = {
            "name": path.name,
            "path": str(resolved),
            "relative_path": resolved.relative_to(library_root).as_posix(),
            "size": int(path.stat().st_size),
            "mtime_utc": _mtime_utc(path),
            "sha256": _sha256(path),
            "is_uuid_only_pdf": bool(UUID_ONLY_PDF_RE.match(path.name)),
            "is_tiny": int(path.stat().st_size) < 1024,
            "is_db_referenced": referenced,
        }
        if not referenced:
            unreferenced.append(item)
        if item["is_uuid_only_pdf"] and item["is_tiny"] and not referenced:
            tiny_uuid_unreferenced.append(item)

    cleanup_deleted: list[dict[str, Any]] = []
    if cleanup and apply:
        for item in tiny_uuid_unreferenced:
            target = Path(str(item["path"])).resolve()
            if target.parent != pdf_dir.resolve():
                raise RuntimeError(f"Refusing cleanup outside active storage/pdf: {target}")
            target.unlink()
            cleanup_deleted.append(item)

    return {
        "mode": "cleanup_apply" if cleanup and apply else ("cleanup_dry_run" if cleanup else "dry_run"),
        "cleanup_requested": cleanup,
        "cleanup_apply": cleanup and apply,
        "active_library_root": str(library_root),
        "active_sqlite_path": str(active_db),
        "db_kind": info.get("db_kind"),
        "active_database_papers_total": _papers_total(active_db),
        "recovered_from_candidate_scan": bool(info.get("recovered_from_candidate_scan")),
        "pdf_storage_dir": str(pdf_dir.resolve()),
        "db_referenced_pdf_count": len([path for path in referenced_paths if path.suffix.lower() == ".pdf"]),
        "pdf_files_count": len(pdf_files),
        "unreferenced_pdf_count": len(unreferenced),
        "tiny_uuid_unreferenced_pdf_count": len(tiny_uuid_unreferenced),
        "tiny_uuid_only_unref_pdf_count": len(tiny_uuid_unreferenced),
        "pollution_detected": len(tiny_uuid_unreferenced) > 0,
        "tiny_uuid_unreferenced_pdfs": tiny_uuid_unreferenced,
        "cleanup_deleted_count": len(cleanup_deleted),
        "cleanup_deleted_files": cleanup_deleted,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit active library tiny UUID-only unreferenced PDF pollution.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit audit mode (default).")
    parser.add_argument("--cleanup", action="store_true", help="Plan cleanup of tiny UUID-only unreferenced PDFs.")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Requires --cleanup.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass
    args = _parse_args()
    if args.apply and not args.cleanup:
        raise SystemExit("--apply requires --cleanup")
    print(json.dumps(build_report(cleanup=args.cleanup, apply=args.apply), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

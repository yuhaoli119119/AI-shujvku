from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


DEFAULT_DATABASE_URL = "postgresql://literature_ai:literature_ai@localhost:5432/literature_ai"
ARTIFACT_SUBDIRS = ("pdf", "markdown", "docling_json", "figures", "tables", "tei", "text")


def _database_url(raw: str | None) -> str:
    value = (raw or DEFAULT_DATABASE_URL).strip()
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _resolve_data_root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    return (Path(__file__).resolve().parents[2] / "data").resolve()


def _db_rows(database_url: str, library_name: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id::text, title, pdf_path, docling_json_path, markdown_path, workspace_path
                from papers
                where library_name = %s
                order by created_at
                """,
                (library_name,),
            )
            names = [item.name for item in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]


def _safe_relative(target: Path, root: Path) -> str:
    resolved = target.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Refusing to touch path outside cleanup root: {resolved}") from exc


def _resolve_artifact_path(data_root: Path, raw: str) -> Path:
    value = raw.strip()
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    if value.startswith("storage/"):
        return (data_root / value).resolve()
    if value.startswith("by_id/"):
        return (data_root / "storage" / value).resolve()
    if value.startswith("libraries/"):
        return (data_root / value).resolve()
    return (data_root / value).resolve()


def _referenced_paths(data_root: Path, rows: list[dict[str, Any]]) -> set[Path]:
    refs: set[Path] = set()
    for row in rows:
        for key in ("pdf_path", "docling_json_path", "markdown_path", "workspace_path"):
            raw = str(row.get(key) or "").strip()
            if not raw:
                continue
            refs.add(_resolve_artifact_path(data_root, raw))
    return refs


def _missing_referenced_paths(data_root: Path, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for row in rows:
        for key in ("pdf_path", "docling_json_path", "markdown_path", "workspace_path"):
            raw = str(row.get(key) or "").strip()
            if not raw:
                continue
            path = _resolve_artifact_path(data_root, raw)
            if not path.exists():
                missing.append(
                    {
                        "paper_id": str(row["id"]),
                        "title": str(row.get("title") or ""),
                        "field": key,
                        "path": raw,
                    }
                )
    return missing


def _collect_stale_targets(data_root: Path, library_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    papers_root = (data_root / "libraries" / library_name / "papers").resolve()
    if not papers_root.exists():
        return []
    paper_ids = {str(row["id"]) for row in rows}
    refs = _referenced_paths(data_root, rows)
    targets: list[dict[str, Any]] = []

    by_id_root = papers_root / "by_id"
    if by_id_root.exists():
        for child in sorted(path for path in by_id_root.iterdir() if path.is_dir()):
            if child.name not in paper_ids and child.resolve() not in refs:
                targets.append(
                    {
                        "kind": "orphan_by_id_workspace",
                        "path": _safe_relative(child, papers_root),
                    }
                )

    for child in sorted(path for path in papers_root.iterdir() if path.is_dir() and path.name.startswith("_orphan_")):
        targets.append({"kind": "legacy_orphan_directory", "path": _safe_relative(child, papers_root)})

    for subdir in ARTIFACT_SUBDIRS:
        root = papers_root / subdir
        if not root.exists():
            continue
        for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
            if file_path.resolve() not in refs:
                targets.append(
                    {
                        "kind": f"unreferenced_{subdir}_file",
                        "path": _safe_relative(file_path, papers_root),
                    }
                )
    return targets


def _delete_targets(papers_root: Path, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for item in targets:
        target = (papers_root / str(item["path"])).resolve()
        _safe_relative(target, papers_root)
        if not target.exists():
            deleted.append({**item, "status": "missing"})
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        deleted.append({**item, "status": "deleted"})
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean stale filesystem artifacts that are not referenced by the DB.")
    parser.add_argument("--library", required=True, help="Exact library name, e.g. 石墨炔")
    parser.add_argument("--data-root", default=None, help="Path to literature-ai/data")
    parser.add_argument("--database-url", default=os.environ.get("LITAI_DATABASE_URL"))
    parser.add_argument("--apply", action="store_true", help="Actually change the filesystem. Without this, dry-run only.")
    parser.add_argument("--delete", action="store_true", help="Delete stale targets. Required together with --apply.")
    args = parser.parse_args()

    data_root = _resolve_data_root(args.data_root)
    database_url = _database_url(args.database_url)
    rows = _db_rows(database_url, args.library)
    papers_root = (data_root / "libraries" / args.library / "papers").resolve()
    targets = _collect_stale_targets(data_root, args.library, rows)
    missing_refs = _missing_referenced_paths(data_root, rows)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library_name": args.library,
        "data_root": str(data_root),
        "database_url": database_url.rsplit("@", 1)[-1],
        "db_paper_count": len(rows),
        "db_paper_ids": [row["id"] for row in rows],
        "mode": "delete" if args.apply and args.delete else "dry_run",
        "target_count": len(targets),
        "targets": targets,
        "missing_referenced_path_count": len(missing_refs),
        "missing_referenced_paths": missing_refs,
    }
    if args.apply:
        if not args.delete:
            raise SystemExit("--apply requires --delete for this cleanup tool.")
        manifest["results"] = _delete_targets(papers_root, targets)

    manifest_dir = data_root / "cleanup_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest_path = manifest_dir / f"{stamp}_{args.library}_artifact_cleanup.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "target_count": len(targets),
                "missing_referenced_path_count": len(missing_refs),
                "mode": manifest["mode"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

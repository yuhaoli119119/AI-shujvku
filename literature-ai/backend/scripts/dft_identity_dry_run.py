from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.dft_identity_dry_run_service import DFTIdentityDryRunService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic, PostgreSQL-enforced READ ONLY DFT Identity v2 analysis."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--confirm-temporary-database", required=True)
    parser.add_argument("--output-manifest", required=True, type=Path)
    parser.add_argument("--backup-path", required=True, type=Path)
    parser.add_argument("--expected-backup-sha256", required=True)
    parser.add_argument("--paper-code")
    parser.add_argument("--data-root", type=Path)
    return parser


def assert_safe_temporary_database(database_url: str, confirmation: str) -> None:
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("postgresql"):
        raise ValueError("database_url_must_be_postgresql")
    if parsed.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("database_url_must_use_loopback_temporary_postgresql")
    database = str(parsed.database or "").strip()
    if not database or confirmation != database:
        raise ValueError("temporary_database_confirmation_mismatch")


def execute_dry_run(
    *,
    database_url: str,
    paper_code: str | None,
    data_root: Path | None,
    backup_path: Path,
    expected_backup_sha256: str,
) -> dict:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                with Session(
                    bind=connection,
                    autoflush=False,
                    expire_on_commit=False,
                    future=True,
                ) as session:
                    manifest = DFTIdentityDryRunService(session).run(
                        paper_code=paper_code,
                        data_root=data_root,
                        backup_path=backup_path,
                        expected_backup_sha256=expected_backup_sha256,
                    )
                transaction.rollback()
                return manifest
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
    finally:
        engine.dispose()


def atomic_write_manifest(path: Path, manifest: dict) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        assert_safe_temporary_database(
            args.database_url,
            args.confirm_temporary_database,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.paper_code and args.data_root is None:
        parser.error("--data-root is required with --paper-code")

    manifest = execute_dry_run(
        database_url=args.database_url,
        paper_code=args.paper_code,
        data_root=args.data_root,
        backup_path=args.backup_path,
        expected_backup_sha256=args.expected_backup_sha256,
    )
    atomic_write_manifest(args.output_manifest, manifest)
    payload = manifest["canonical_payload"]
    print(
        json.dumps(
            {
                "status": "completed",
                "mode": "dry_run",
                "canonical_sha256": manifest["canonical_sha256"],
                "database_data_fingerprint": payload["database_data_fingerprint"]["before"]["sha256"],
                "pdf_snapshot_fingerprint": (
                    payload["pdf_snapshot_fingerprint"]["before"]["sha256"]
                    if payload["pdf_snapshot_fingerprint"]
                    else None
                ),
                "dry_run_database_writes": payload["dry_run_database_writes"],
                "output_manifest": str(args.output_manifest.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

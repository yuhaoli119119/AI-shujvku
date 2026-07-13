from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.dft_b0102_reconciliation_service import (
    DFTB0102ReconciliationService,
)
from app.services.dft_identity_dry_run_service import DFTIdentityDryRunService
from scripts.dft_identity_dry_run import (
    assert_safe_temporary_database,
    atomic_write_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strict B0102 reconciliation on a confirmed loopback temporary PostgreSQL database."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--confirm-temporary-database", required=True)
    parser.add_argument("--paper-code", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-manifest-canonical-sha256", required=True)
    parser.add_argument("--backup-path", required=True, type=Path)
    parser.add_argument("--expected-backup-sha256", required=True)
    parser.add_argument("--expected-database-fingerprint", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--expected-pdf-fingerprint", required=True)
    parser.add_argument("--output-report", required=True, type=Path)
    return parser


def execute_reconciliation(
    *,
    database_url: str,
    database_name: str,
    mode: str,
    paper_code: str,
    manifest_path: Path,
    expected_manifest_sha256: str,
    backup_path: Path,
    expected_backup_sha256: str,
    expected_database_fingerprint: str,
    data_root: Path,
    expected_pdf_fingerprint: str,
) -> dict:
    assert_safe_temporary_database(database_url, database_name)
    manifest = DFTB0102ReconciliationService.load_and_validate_manifest(
        manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
        backup_path=backup_path,
        expected_backup_sha256=expected_backup_sha256,
        expected_database_fingerprint=expected_database_fingerprint,
        expected_pdf_fingerprint=expected_pdf_fingerprint,
        paper_code=paper_code,
    )
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                actual_database = str(connection.scalar(text("SELECT current_database()")))
                if actual_database != database_name:
                    raise ValueError("temporary_database_confirmation_mismatch")
                with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                    pdf_snapshot = DFTB0102ReconciliationService(session).assert_pdf_preflight(
                        data_root=data_root,
                        expected_pdf_fingerprint=expected_pdf_fingerprint,
                    )
                transaction.rollback()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                actual_database = str(connection.scalar(text("SELECT current_database()")))
                if actual_database != database_name:
                    raise ValueError("temporary_database_confirmation_mismatch")
                with Session(
                    bind=connection,
                    autoflush=False,
                    expire_on_commit=False,
                    future=True,
                ) as session:
                    result = DFTB0102ReconciliationService(session).reconcile(
                        manifest=manifest,
                        expected_manifest_sha256=expected_manifest_sha256,
                        expected_database_fingerprint=expected_database_fingerprint,
                        expected_pdf_fingerprint=expected_pdf_fingerprint,
                        pdf_preflight_fingerprint=pdf_snapshot["sha256"],
                    )
                    session.flush()
                if mode == "dry_run":
                    transaction.rollback()
                else:
                    transaction.commit()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise

        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                    restored_fingerprint = DFTIdentityDryRunService(session).database_data_fingerprint()
                    readback = DFTB0102ReconciliationService(session).readback(
                        require_final=mode == "apply"
                    )
                transaction.rollback()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
    finally:
        engine.dispose()

    before_sha = result["database_fingerprint_before"]["sha256"]
    if mode == "dry_run" and restored_fingerprint["sha256"] != before_sha:
        raise RuntimeError("dry_run_rollback_fingerprint_mismatch")
    if mode == "apply" and restored_fingerprint["sha256"] != result["database_fingerprint_after"]["sha256"]:
        raise RuntimeError("apply_readback_fingerprint_mismatch")
    return {
        "report_version": "b0102_dft_reconciliation_report_v1",
        "status": result["status"],
        "mode": mode,
        "paper_code": paper_code,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_canonical_sha256": manifest["canonical_sha256"],
        "backup_path": str(backup_path.resolve()),
        "backup_sha256": expected_backup_sha256.upper(),
        "pdf_snapshot_fingerprint": pdf_snapshot["sha256"],
        "database_fingerprint_before": result["database_fingerprint_before"],
        "database_fingerprint_after_transaction": result["database_fingerprint_after"],
        "database_fingerprint_after_command": restored_fingerprint,
        "database_writes": 0 if mode == "dry_run" else result["database_writes"],
        "attempted_write_events": result["write_events"] if mode == "dry_run" else [],
        "rollback_fingerprint_equal": (
            restored_fingerprint["sha256"] == before_sha if mode == "dry_run" else None
        ),
        "safe_366": result.get("safe_366"),
        "splits": result.get("splits", []),
        "audit_counts_before": result.get("audit_counts_before", {}),
        "audit_counts_after": result.get("audit_counts_after", {}),
        "audit_counts_delta": result.get("audit_counts_delta", {}),
        "other_papers_unchanged": result.get("other_papers_unchanged", True),
        "final_readback": readback,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        assert_safe_temporary_database(args.database_url, args.confirm_temporary_database)
    except ValueError as exc:
        parser.error(str(exc))
    report = execute_reconciliation(
        database_url=args.database_url,
        database_name=args.confirm_temporary_database,
        mode="dry_run" if args.dry_run else "apply",
        paper_code=args.paper_code,
        manifest_path=args.manifest,
        expected_manifest_sha256=args.expected_manifest_canonical_sha256,
        backup_path=args.backup_path,
        expected_backup_sha256=args.expected_backup_sha256,
        expected_database_fingerprint=args.expected_database_fingerprint,
        data_root=args.data_root,
        expected_pdf_fingerprint=args.expected_pdf_fingerprint,
    )
    atomic_write_manifest(args.output_report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": report["mode"],
                "database_writes": report["database_writes"],
                "database_fingerprint_before": report["database_fingerprint_before"]["sha256"],
                "database_fingerprint_after": report["database_fingerprint_after_command"]["sha256"],
                "output_report": str(args.output_report.resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.dft_failed_batch_recovery_service import (  # noqa: E402
    DFTFailedBatchRecoveryService,
    RECOVERY_PAPER_CODE,
    RecoveryRefused,
    load_manifest,
)


DEFAULT_MANIFEST_PATH = (
    BACKEND_ROOT.parent
    / "data"
    / "maintenance"
    / "reports"
    / "b0102_failed_batch_recovery_dry_run.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and transactionally recover the failed B0102 DFT batch. Defaults to dry-run."
    )
    parser.add_argument(
        "--backup-database-url",
        default=os.getenv("LITAI_RECOVERY_BACKUP_DATABASE_URL"),
        help="SQLAlchemy URL for the independently restored backup database.",
    )
    parser.add_argument(
        "--live-database-url",
        default=os.getenv("LITAI_RECOVERY_LIVE_DATABASE_URL") or os.getenv("LITAI_DATABASE_URL"),
        help="SQLAlchemy URL for the live database. Dry-run forces a read-only transaction.",
    )
    parser.add_argument(
        "--backup-dump-path",
        type=Path,
        required=True,
        help="Backup dump whose SHA256 is bound into the manifest.",
    )
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-paper-code")
    parser.add_argument("--expected-backup-sha256")
    parser.add_argument("--expected-live-fingerprint")
    return parser.parse_args()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    args = parse_args()
    if not args.backup_database_url or not args.live_database_url:
        raise RecoveryRefused("backup_and_live_database_urls_are_required")
    if not args.backup_dump_path.is_file():
        raise RecoveryRefused("backup_dump_missing")
    backup_sha256 = file_sha256(args.backup_dump_path)

    if args.apply:
        missing = [
            flag
            for flag, value in (
                ("--confirm-paper-code", args.confirm_paper_code),
                ("--expected-backup-sha256", args.expected_backup_sha256),
                ("--expected-live-fingerprint", args.expected_live_fingerprint),
                ("--manifest-path", args.manifest_path),
            )
            if not value
        ]
        if missing:
            raise RecoveryRefused("missing_apply_flags:" + ",".join(missing))
        if args.confirm_paper_code != RECOVERY_PAPER_CODE:
            raise RecoveryRefused("apply_confirmation_must_be_B0102")
        if not args.manifest_path.is_file():
            raise RecoveryRefused("manifest_file_missing")

    backup_engine = create_engine(args.backup_database_url, future=True, pool_pre_ping=True)
    live_engine = create_engine(args.live_database_url, future=True, pool_pre_ping=True)
    try:
        service = DFTFailedBatchRecoveryService(
            backup_engine=backup_engine,
            live_engine=live_engine,
            backup_sha256=backup_sha256,
        )
        if args.apply:
            manifest = load_manifest(args.manifest_path)
            result = service.apply_manifest(
                manifest,
                confirm_paper_code=args.confirm_paper_code,
                expected_backup_sha256=args.expected_backup_sha256,
                expected_live_fingerprint=args.expected_live_fingerprint,
            )
        else:
            manifest = service.build_dry_run_manifest(output_path=args.manifest_path)
            result = {
                "status": manifest["status"],
                "mode": "dry_run",
                "manifest_path": str(args.manifest_path.resolve()),
                "manifest_sha256": manifest["manifest_sha256"],
                "identified_issue_count": manifest["identified_issue_count"],
                "identified_result_count": manifest["identified_result_count"],
                "live_fingerprint": manifest["live"]["fingerprint"],
                "database_writes": 0,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        backup_engine.dispose()
        live_engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RecoveryRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)

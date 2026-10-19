from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout


MODULE_PATH = Path(__file__).resolve().parents[1] / "cleanup_download_staging.py"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("cleanup_download_staging", MODULE_PATH)
assert SPEC and SPEC.loader
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def write_file(path: Path, data: bytes, *, age_days: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = NOW.timestamp() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return hashlib.sha256(data).hexdigest()


def age_directory(path: Path, *, age_days: int) -> None:
    stamp = NOW.timestamp() - age_days * 86400
    os.utime(path, (stamp, stamp))


def mark_download_complete(directory: Path, *, age_days: int) -> Path:
    marker = directory / ".download-complete"
    marker.write_bytes(b"complete\n")
    stamp = NOW.timestamp() - age_days * 86400
    os.utime(marker, (stamp, stamp))
    return marker


class DownloadStagingCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        PROJECT_ROOT.joinpath("outputs").mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="cleanup-download-staging-test-", dir=PROJECT_ROOT / "outputs"
        )
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_deletes_old_matched_pdf_and_manifest(self) -> None:
        pdf = self.root / "outputs" / "qingli-downloads-old" / "paper.pdf"
        manifest = self.root / "outputs" / "qingli-downloads-old" / "SHA256SUMS.txt"
        digest = write_file(pdf, b"verified-pdf", age_days=8)
        write_file(manifest, f"{digest}  paper.pdf\n".encode(), age_days=8)
        marker = mark_download_complete(pdf.parent, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"verified-pdf", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"verified-pdf"), Path("pdf/formal.pdf"))]
        age_directory(pdf.parent, age_days=8)

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertEqual([item.path for item in result.deleted], [str(pdf), str(manifest), str(marker)])
        self.assertFalse(pdf.exists())
        self.assertFalse(manifest.exists())
        self.assertFalse(marker.exists())
        self.assertFalse(pdf.parent.exists())

    def test_unmatched_old_pdf_and_manifest_are_kept(self) -> None:
        pdf = self.root / "outputs" / "ablesci-downloads-old" / "paper.pdf"
        manifest = self.root / "outputs" / "ablesci-downloads-old" / "SHA256SUMS.txt"
        write_file(pdf, b"not-imported", age_days=8)
        write_file(manifest, b"manifest", age_days=8)
        mark_download_complete(pdf.parent, age_days=8)
        age_directory(pdf.parent, age_days=8)

        result = cleanup.cleanup_download_staging(self.root, 7, [], apply=True, now=NOW)

        self.assertFalse(result.deleted)
        self.assertTrue(pdf.exists())
        self.assertTrue(manifest.exists())
        self.assertEqual(
            result.kept,
            [{"path": str(pdf), "reason": "no_verified_formal_storage_copy"}],
        )

    def test_missing_completion_marker_keeps_whole_directory(self) -> None:
        directory = self.root / "outputs" / "download-staging-no-marker"
        pdf = directory / "paper.pdf"
        digest = write_file(pdf, b"no-marker", age_days=8)
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"no-marker", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"no-marker"), Path("pdf/formal.pdf"))]

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertFalse(result.deleted)
        self.assertEqual(
            result.kept,
            [{"path": str(pdf), "reason": "completion_marker_missing"}],
        )

    def test_mismatched_formal_storage_copy_is_kept(self) -> None:
        pdf = self.root / "outputs" / "qingli-downloads-old" / "paper.pdf"
        digest = write_file(pdf, b"expected", age_days=8)
        mark_download_complete(pdf.parent, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"corrupt!", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"expected"), Path("pdf/formal.pdf"))]
        age_directory(pdf.parent, age_days=8)

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertFalse(result.deleted)
        self.assertEqual(
            result.kept,
            [{"path": str(pdf), "reason": "no_verified_formal_storage_copy"}],
        )

    def test_recent_matched_pdf_is_kept(self) -> None:
        pdf = self.root / "outputs" / "qingli-downloads-recent" / "paper.pdf"
        digest = write_file(pdf, b"recent", age_days=6)
        mark_download_complete(pdf.parent, age_days=6)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"recent", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"recent"), Path("pdf/formal.pdf"))]
        age_directory(pdf.parent, age_days=6)

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertEqual(result.kept, [{"path": str(pdf), "reason": "directory_not_old_enough"}])

    def test_dry_run_and_unrelated_directory_do_not_delete(self) -> None:
        staged = self.root / "outputs" / "qingli-downloads-old" / "paper.pdf"
        staged_manifest = self.root / "outputs" / "qingli-downloads-old" / "SHA256SUMS.txt"
        unrelated = self.root / "outputs" / "default-library-import-old" / "paper.pdf"
        digest = write_file(staged, b"matched", age_days=8)
        write_file(staged_manifest, f"{digest}  paper.pdf\n".encode(), age_days=8)
        staged_marker = mark_download_complete(staged.parent, age_days=8)
        write_file(unrelated, b"unrelated", age_days=30)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"matched", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"matched"), Path("pdf/formal.pdf"))]
        age_directory(staged.parent, age_days=8)
        age_directory(unrelated.parent, age_days=30)

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=False, now=NOW
        )

        self.assertTrue(staged.exists())
        self.assertTrue(staged_manifest.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual([item.path for item in result.deleted], [str(staged), str(staged_manifest), str(staged_marker)])
        self.assertEqual(result.removed_directories, [str(staged.parent)])
        self.assertTrue(result.dry_run)

        actual = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )
        self.assertEqual([item.path for item in actual.deleted], [str(staged), str(staged_manifest), str(staged_marker)])
        self.assertEqual(actual.removed_directories, [str(staged.parent)])
        self.assertFalse(staged.parent.exists())

    def test_malformed_manifest_is_kept_after_verified_pdf(self) -> None:
        directory = self.root / "outputs" / "download-staging-bad-manifest"
        pdf = directory / "paper.pdf"
        manifest = directory / "SHA256SUMS.txt"
        digest = write_file(pdf, b"verified", age_days=8)
        write_file(manifest, b"not-a-valid-manifest", age_days=8)
        mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"verified", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"verified"), Path("pdf/formal.pdf"))]

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertTrue(manifest.exists())
        self.assertIn(
            {"path": str(manifest), "reason": "manifest_not_verified"},
            result.kept,
        )

    def test_windows_transfer_manifest_is_validated_and_removed(self) -> None:
        directory = self.root / "outputs" / "download-staging-windows-manifest"
        pdf = directory / "paper.pdf"
        manifest = directory / "windows-transfer-verify.tsv"
        data = b"windows"
        digest = write_file(pdf, data, age_days=8)
        write_file(manifest, f"COUNT=1\npaper.pdf\t{len(data)}\t{digest}\n".encode(), age_days=8)
        marker = mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, data, age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(data), Path("pdf/formal.pdf"))]

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertEqual(
            [item.path for item in result.deleted],
            [str(pdf), str(manifest), str(marker)],
        )
        self.assertFalse(directory.exists())

    def test_symlinked_staging_directory_is_ignored(self) -> None:
        target = self.root / "outside"
        target.mkdir()
        pdf = target / "paper.pdf"
        write_file(pdf, b"outside", age_days=30)
        outputs = self.root / "outputs"
        outputs.mkdir()
        link = outputs / "qingli-downloads-link"
        link.symlink_to(target, target_is_directory=True)

        result = cleanup.cleanup_download_staging(self.root, 7, [], apply=True, now=NOW)

        self.assertEqual(result.scanned_directories, 0)
        self.assertTrue(pdf.exists())

    def test_symlinked_outputs_root_is_rejected(self) -> None:
        target = self.root / "outside-outputs"
        target.mkdir()
        (self.root / "outputs").symlink_to(target, target_is_directory=True)

        with self.assertRaises(RuntimeError):
            cleanup.cleanup_download_staging(self.root, 7, [], apply=True, now=NOW)

    def test_main_rejects_symlinked_root_before_writing_logs(self) -> None:
        target = self.root / "outside-main"
        target.mkdir()
        (self.root / "outputs").symlink_to(target, target_is_directory=True)

        output = io.StringIO()
        with redirect_stdout(output):
            code = cleanup.main(["--project-root", str(self.root), "--apply"])

        self.assertEqual(code, 2)
        self.assertIn('"status": "refused"', output.getvalue())
        self.assertFalse((target / "cleanup-logs").exists())

    def test_overlapping_formal_storage_root_is_rejected(self) -> None:
        outputs = self.root / "outputs"
        outputs.mkdir()
        (self.root / "data").mkdir()
        (self.root / "data" / "storage").symlink_to(outputs, target_is_directory=True)

        with self.assertRaises(RuntimeError):
            cleanup.cleanup_download_staging(self.root, 7, [], apply=True, now=NOW)

    def test_undeclared_directory_that_merely_contains_downloads_is_ignored(self) -> None:
        directory = self.root / "outputs" / "review-downloads-export"
        pdf = directory / "paper.pdf"
        digest = write_file(pdf, b"deliverable", age_days=30)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"deliverable", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"deliverable"), Path("pdf/formal.pdf"))]
        age_directory(directory, age_days=30)

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertEqual(result.scanned_directories, 0)

    def test_orphan_manifest_is_kept_without_pdf_or_cleanup_receipt(self) -> None:
        directory = self.root / "outputs" / "download-staging-orphan"
        manifest = directory / "SHA256SUMS.txt"
        write_file(manifest, b"orphan", age_days=30)
        age_directory(directory, age_days=30)

        result = cleanup.cleanup_download_staging(self.root, 7, [], apply=True, now=NOW)

        self.assertTrue(manifest.exists())
        self.assertFalse(result.deleted)

    def test_exactly_seven_days_old_is_not_yet_old_enough(self) -> None:
        directory = self.root / "outputs" / "download-staging-boundary"
        pdf = directory / "paper.pdf"
        digest = write_file(pdf, b"boundary", age_days=7)
        mark_download_complete(directory, age_days=7)
        age_directory(directory, age_days=7)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"boundary", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"boundary"), Path("pdf/formal.pdf"))]

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertEqual(
            result.kept,
            [{"path": str(pdf), "reason": "directory_not_old_enough"}],
        )

    def test_pdf_changed_after_completion_marker_is_kept(self) -> None:
        directory = self.root / "outputs" / "download-staging-late-write"
        pdf = directory / "paper.pdf"
        digest = write_file(pdf, b"original", age_days=8)
        mark_download_complete(directory, age_days=8)
        pdf.write_bytes(b"late write")
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, b"original", age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(b"original"), Path("pdf/formal.pdf"))]

        result = cleanup.cleanup_download_staging(
            self.root, 7, registered, apply=True, now=NOW
        )

        self.assertTrue(pdf.exists())
        self.assertFalse(result.deleted)
        self.assertEqual(
            result.kept,
            [{"path": str(pdf), "reason": "changed_after_completion_marker"}],
        )

    def test_fifo_named_like_pdf_is_kept_without_blocking(self) -> None:
        directory = self.root / "outputs" / "download-staging-fifo"
        directory.mkdir(parents=True)
        fifo = directory / "paper.pdf"
        os.mkfifo(fifo)
        mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)

        result = cleanup.cleanup_download_staging(
            self.root, 7, [], apply=True, now=NOW
        )

        self.assertTrue(fifo.exists())
        self.assertFalse(result.deleted)
        self.assertIn(
            {"path": str(fifo), "reason": "inspection_failed:not a regular file"},
            result.kept,
        )

    def test_restore_quarantine_does_not_overwrite_concurrent_file(self) -> None:
        directory = self.root / "restore"
        directory.mkdir()
        quarantine = directory / ".cleanup-pending-test"
        quarantine.write_bytes(b"old")
        original = directory / "paper.pdf"
        original.write_bytes(b"new")
        directory_fd = os.open(directory, cleanup.DIRECTORY_FLAGS)
        try:
            recovered = cleanup._restore_quarantine(
                directory_fd, quarantine.name, original.name
            )
        finally:
            os.close(directory_fd)

        self.assertEqual(original.read_bytes(), b"new")
        self.assertTrue(recovered.startswith(".cleanup-recovered-"))
        self.assertEqual((directory / recovered).read_bytes(), b"old")
        self.assertFalse(quarantine.exists())

    def test_formal_copy_removed_after_planning_restores_staged_file(self) -> None:
        directory = self.root / "outputs" / "download-staging-formal-race"
        pdf = directory / "paper.pdf"
        data = b"formal-race"
        digest = write_file(pdf, data, age_days=8)
        mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, data, age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(data), Path("pdf/formal.pdf"))]
        cutoff = NOW.timestamp() - 7 * 86400
        kept: list[dict[str, str]] = []
        outputs_fd = os.open(self.root / "outputs", cleanup.DIRECTORY_FLAGS)
        storage_fd = os.open(self.root / "data" / "storage", cleanup.DIRECTORY_FLAGS)
        try:
            plan = cleanup._plan_directory(
                directory.name,
                self.root / "outputs",
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
                cutoff,
                kept,
            )
            self.assertIsNotNone(plan)
            formal.unlink()
            deleted, apply_kept, removed = cleanup._apply_directory_plan(
                plan,
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
            )
        finally:
            os.close(storage_fd)
            os.close(outputs_fd)

        self.assertFalse(deleted)
        self.assertFalse(removed)
        self.assertTrue(pdf.exists())
        self.assertEqual(
            apply_kept[0]["reason"],
            "directory_apply_failed:formal storage copy no longer verifies",
        )

    def test_directory_change_after_planning_prevents_delete(self) -> None:
        directory = self.root / "outputs" / "download-staging-directory-race"
        pdf = directory / "paper.pdf"
        data = b"directory-race"
        digest = write_file(pdf, data, age_days=8)
        mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)
        formal = self.root / "data" / "storage" / "pdf" / "formal.pdf"
        write_file(formal, data, age_days=1)
        registered = [cleanup.RegisteredFile(digest, len(data), Path("pdf/formal.pdf"))]
        cutoff = NOW.timestamp() - 7 * 86400
        kept: list[dict[str, str]] = []
        outputs_fd = os.open(self.root / "outputs", cleanup.DIRECTORY_FLAGS)
        storage_fd = os.open(self.root / "data" / "storage", cleanup.DIRECTORY_FLAGS)
        try:
            plan = cleanup._plan_directory(
                directory.name,
                self.root / "outputs",
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
                cutoff,
                kept,
            )
            self.assertIsNotNone(plan)
            (directory / "new-entry.txt").write_text("new", encoding="utf-8")
            deleted, apply_kept, removed = cleanup._apply_directory_plan(
                plan,
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
            )
        finally:
            os.close(storage_fd)
            os.close(outputs_fd)

        self.assertFalse(deleted)
        self.assertFalse(removed)
        self.assertTrue(pdf.exists())
        self.assertEqual(apply_kept, [{"path": str(directory), "reason": "directory_changed_after_planning"}])

    def test_second_pdf_failure_rolls_back_first_pdf(self) -> None:
        directory = self.root / "outputs" / "download-staging-two-pdfs"
        first = directory / "first.pdf"
        second = directory / "second.pdf"
        first_data = b"first"
        second_data = b"second"
        first_digest = write_file(first, first_data, age_days=8)
        second_digest = write_file(second, second_data, age_days=8)
        marker = mark_download_complete(directory, age_days=8)
        age_directory(directory, age_days=8)
        first_formal = self.root / "data" / "storage" / "pdf" / "first-formal.pdf"
        second_formal = self.root / "data" / "storage" / "pdf" / "second-formal.pdf"
        write_file(first_formal, first_data, age_days=1)
        write_file(second_formal, second_data, age_days=1)
        registered = [
            cleanup.RegisteredFile(first_digest, len(first_data), Path("pdf/first-formal.pdf")),
            cleanup.RegisteredFile(second_digest, len(second_data), Path("pdf/second-formal.pdf")),
        ]
        cutoff = NOW.timestamp() - 7 * 86400
        kept: list[dict[str, str]] = []
        outputs_fd = os.open(self.root / "outputs", cleanup.DIRECTORY_FLAGS)
        storage_fd = os.open(self.root / "data" / "storage", cleanup.DIRECTORY_FLAGS)
        try:
            plan = cleanup._plan_directory(
                directory.name,
                self.root / "outputs",
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
                cutoff,
                kept,
            )
            self.assertIsNotNone(plan)
            second.write_bytes(b"changed after planning")
            deleted, apply_kept, removed = cleanup._apply_directory_plan(
                plan,
                outputs_fd,
                self.root / "data" / "storage",
                storage_fd,
                registered,
            )
        finally:
            os.close(storage_fd)
            os.close(outputs_fd)

        self.assertFalse(deleted)
        self.assertFalse(removed)
        self.assertEqual(first.read_bytes(), first_data)
        self.assertEqual(second.read_bytes(), b"changed after planning")
        self.assertTrue(marker.exists())
        self.assertFalse(any(path.name.startswith(".cleanup-") for path in directory.iterdir()))
        self.assertEqual(
            apply_kept,
            [{"path": str(directory), "reason": "directory_apply_failed:quarantined file identity changed"}],
        )

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cleanup.cleanup_download_staging(
                self.root, 7, [], apply=True, now=datetime(2026, 10, 19)
            )

    def test_database_failure_raises_before_cleanup(self) -> None:
        process = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="database unavailable")
        with mock.patch.object(cleanup.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                cleanup.load_registered_files(self.root)

    def test_malformed_and_unsafe_database_rows_are_ignored(self) -> None:
        digest = "a" * 64
        stdout = "\n".join(
            [
                "malformed",
                f"{digest}|10|../outside.pdf",
                f"{digest}|10|/absolute.pdf",
                f"{digest}|10|pdf/safe.pdf",
            ]
        )
        process = subprocess.CompletedProcess([], returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(cleanup.subprocess, "run", return_value=process):
            rows = cleanup.load_registered_files(self.root)

        self.assertEqual(rows, [cleanup.RegisteredFile(digest, 10, Path("pdf/safe.pdf"))])


if __name__ == "__main__":
    unittest.main()

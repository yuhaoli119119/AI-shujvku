#!/usr/bin/env python3
"""Safely expire download-only copies after they have entered formal storage."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
from typing import Iterable, Sequence
from uuid import uuid4


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGING_NAME_PREFIXES = (
    "download-staging-",
    "qingli-downloads-",
    "ablesci-downloads-",
)
AUXILIARY_FILENAMES = frozenset({"SHA256SUMS.txt", "windows-transfer-verify.tsv"})
COMPLETION_MARKER = ".download-complete"
ARTIFACT_DELETE_LOCK = ".artifact-delete.lock"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class RegisteredFile:
    sha256: str
    file_size: int
    storage_path: Path


@dataclass(frozen=True)
class PlannedFile:
    directory_path: str
    directory_name: str
    directory_device: int
    directory_inode: int
    file_name: str
    file_device: int
    file_inode: int
    file_size: int
    sha256: str
    kind: str
    formal_storage_copy: str | None


@dataclass(frozen=True)
class PlannedDirectory:
    directory_path: str
    directory_name: str
    directory_device: int
    directory_inode: int
    directory_mtime_ns: int
    marker_device: int
    marker_inode: int
    marker_ctime_ns: int
    files: tuple[PlannedFile, ...]
    remove_directory: bool


@dataclass(frozen=True)
class Deletion:
    path: str
    sha256: str
    formal_storage_copy: str


@dataclass
class CleanupResult:
    dry_run: bool
    retention_days: int
    scanned_directories: int
    deleted: list[Deletion]
    removed_directories: list[str]
    kept: list[dict[str, str]]


def is_staging_name(name: str) -> bool:
    return any(name.startswith(prefix) and len(name) > len(prefix) for prefix in STAGING_NAME_PREFIXES)


def staging_directories(outputs_root: Path) -> list[Path]:
    if not outputs_root.is_dir() or outputs_root.is_symlink():
        return []
    result: list[Path] = []
    for child in sorted(outputs_root.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir() or not is_staging_name(child.name):
            continue
        result.append(child)
    return result


def load_registered_files(project_root: Path, timeout_seconds: int = 60) -> list[RegisteredFile]:
    """Read only registered file identities from PostgreSQL through docker compose."""
    query = (
        "SELECT sha256,file_size,storage_path FROM rebuild_paper_files "
        "WHERE sha256 IS NOT NULL AND file_size IS NOT NULL AND storage_path IS NOT NULL;"
    )
    shell_command = (
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|" -c '
        + shlex.quote(query)
    )
    process = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", shell_command],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "database query failed"
        raise RuntimeError(detail)

    registered: list[RegisteredFile] = []
    for line in process.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("|", 2)
        if len(fields) != 3:
            logging.warning("Ignoring malformed registered-file row: %r", line)
            continue
        digest, raw_size, raw_path = fields
        digest = digest.lower()
        if not SHA256_RE.fullmatch(digest):
            logging.warning("Ignoring registered file with invalid hash: %r", raw_path)
            continue
        try:
            size = int(raw_size)
        except ValueError:
            logging.warning("Ignoring registered file with invalid size: %r", line)
            continue
        relative = Path(raw_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            logging.warning("Ignoring unsafe registered storage path: %r", raw_path)
            continue
        registered.append(RegisteredFile(digest, size, relative))
    return registered


def _is_beneath(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _stable_digest_fd(fd: int) -> tuple[str, os.stat_result]:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("not a regular file")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(fd)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise OSError("file changed while it was being hashed")
    return digest.hexdigest(), after


def _open_regular_at(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    entry_snapshot = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(entry_snapshot.st_mode):
        raise OSError("not a regular file")
    fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
    try:
        snapshot = os.fstat(fd)
        if not stat.S_ISREG(snapshot.st_mode):
            raise OSError("not a regular file")
        return fd, snapshot
    except Exception:
        os.close(fd)
        raise


def _entry_matches(directory_fd: int, name: str, snapshot: os.stat_result) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (snapshot.st_dev, snapshot.st_ino)


def _open_relative_regular(root_fd: int, relative: Path) -> tuple[int, os.stat_result]:
    anchor = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            next_anchor = os.open(part, DIRECTORY_FLAGS, dir_fd=anchor)
            os.close(anchor)
            anchor = next_anchor
        return _open_regular_at(anchor, relative.parts[-1])
    finally:
        os.close(anchor)


def _lock_artifact_deletions(storage_fd: int | None) -> int:
    if storage_fd is None:
        raise OSError("formal storage is unavailable")
    try:
        fd = os.open(
            ARTIFACT_DELETE_LOCK,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o666,
            dir_fd=storage_fd,
        )
        try:
            os.fchmod(fd, 0o644)
        except PermissionError:
            pass
    except PermissionError:
        fd = os.open(ARTIFACT_DELETE_LOCK, os.O_RDONLY | os.O_CLOEXEC, dir_fd=storage_fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except Exception:
        os.close(fd)
        raise


def _verified_formal_copy(
    digest: str,
    size: int,
    storage_root: Path,
    storage_fd: int | None,
    registered: Sequence[RegisteredFile],
) -> str | None:
    if storage_fd is None:
        return None
    for record in registered:
        if record.sha256 != digest or record.file_size != size:
            continue
        fd = -1
        try:
            fd, _ = _open_relative_regular(storage_fd, record.storage_path)
            actual_digest, snapshot = _stable_digest_fd(fd)
            if snapshot.st_size == size and actual_digest == digest:
                return str(storage_root / record.storage_path)
        except OSError:
            continue
        finally:
            if fd >= 0:
                os.close(fd)
    return None


def _read_manifest(directory_fd: int, name: str) -> tuple[bytes, str, os.stat_result] | None:
    fd = -1
    try:
        fd, _ = _open_regular_at(directory_fd, name)
        digest, snapshot = _stable_digest_fd(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), digest, snapshot
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def _manifest_matches(
    name: str, content: bytes, directory_name: str, pdf_plans: Sequence[PlannedFile]
) -> bool:
    expected = {item.file_name: (item.sha256, item.file_size) for item in pdf_plans}
    if not expected:
        return False
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if name == "SHA256SUMS.txt":
        seen: set[str] = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split(maxsplit=1)
            if len(fields) != 2:
                return False
            digest, raw_path = fields
            normalized_path = raw_path.lstrip("*").replace("\\", "/").removeprefix("./")
            file_name = Path(normalized_path).name
            if file_name not in expected or expected[file_name][0] != digest.lower():
                return False
            allowed_paths = {
                file_name,
                f"{directory_name}/{file_name}",
                f"outputs/{directory_name}/{file_name}",
            }
            if normalized_path not in allowed_paths or file_name in seen:
                return False
            seen.add(file_name)
        return seen == set(expected)
    if name == "windows-transfer-verify.tsv":
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines or lines[0].strip() != f"COUNT={len(expected)}":
            return False
        seen: set[str] = set()
        for line in lines[1:]:
            fields = line.split("\t")
            if len(fields) != 3:
                return False
            file_name, raw_size, digest = fields
            try:
                size = int(raw_size)
            except ValueError:
                return False
            if expected.get(file_name) != (digest.lower(), size):
                return False
            if file_name in seen:
                return False
            seen.add(file_name)
        return seen == set(expected)
    return False


def _project_roots(project_root: Path) -> tuple[Path, Path]:
    outputs_path = project_root / "outputs"
    if outputs_path.is_symlink():
        raise RuntimeError("Refusing to clean through a symlinked outputs directory")
    outputs_root = outputs_path.resolve()
    storage_path = project_root / "data" / "storage"
    if storage_path.is_symlink():
        raise RuntimeError("Refusing to validate formal copies through a symlinked storage directory")
    storage_root = storage_path.resolve()
    if _is_beneath(outputs_root, storage_root) or _is_beneath(storage_root, outputs_root):
        raise RuntimeError("outputs and data/storage must not overlap")
    return outputs_root, storage_root


def _plan_directory(
    directory_name: str,
    outputs_root: Path,
    outputs_fd: int,
    storage_root: Path,
    storage_fd: int | None,
    registered: Sequence[RegisteredFile],
    cutoff: float,
    kept: list[dict[str, str]],
) -> PlannedDirectory | None:
    directory_fd = -1
    directory_path = str(outputs_root / directory_name)
    try:
        directory_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=outputs_fd)
        directory_snapshot = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd))
        pdf_names = [name for name in names if name.lower().endswith(".pdf")]
        if COMPLETION_MARKER not in names:
            kept.extend({"path": str(Path(directory_path) / name), "reason": "completion_marker_missing"} for name in pdf_names)
            return None
        if directory_snapshot.st_mtime >= cutoff:
            kept.extend({"path": str(Path(directory_path) / name), "reason": "directory_not_old_enough"} for name in pdf_names)
            return None

        marker_fd = -1
        try:
            marker_fd, marker_snapshot = _open_regular_at(directory_fd, COMPLETION_MARKER)
            marker_digest, marker_stable = _stable_digest_fd(marker_fd)
            if not _entry_matches(directory_fd, COMPLETION_MARKER, marker_stable):
                raise OSError("completion marker changed during inspection")
        finally:
            if marker_fd >= 0:
                os.close(marker_fd)
        if directory_snapshot.st_mtime >= cutoff or marker_stable.st_mtime >= cutoff:
            kept.extend({"path": str(Path(directory_path) / name), "reason": "directory_not_old_enough"} for name in pdf_names)
            return None

        pdf_plans: list[PlannedFile] = []
        unsafe = False
        for name in pdf_names:
            fd = -1
            try:
                fd, snapshot = _open_regular_at(directory_fd, name)
                digest, stable_snapshot = _stable_digest_fd(fd)
                if not _entry_matches(directory_fd, name, stable_snapshot):
                    raise OSError("entry changed while it was being inspected")
                if (
                    stable_snapshot.st_mtime_ns > marker_stable.st_mtime_ns
                    or stable_snapshot.st_ctime_ns > marker_stable.st_ctime_ns
                ):
                    kept.append({"path": str(Path(directory_path) / name), "reason": "changed_after_completion_marker"})
                    unsafe = True
                    continue
                formal = _verified_formal_copy(
                    digest, stable_snapshot.st_size, storage_root, storage_fd, registered
                )
                if formal is None:
                    kept.append({"path": str(Path(directory_path) / name), "reason": "no_verified_formal_storage_copy"})
                    unsafe = True
                    continue
                item = PlannedFile(
                    directory_path,
                    directory_name,
                    directory_snapshot.st_dev,
                    directory_snapshot.st_ino,
                    name,
                    stable_snapshot.st_dev,
                    stable_snapshot.st_ino,
                    stable_snapshot.st_size,
                    digest,
                    "pdf",
                    formal,
                )
                pdf_plans.append(item)
            except OSError as exc:
                kept.append({"path": str(Path(directory_path) / name), "reason": f"inspection_failed:{exc}"})
                unsafe = True
            finally:
                if fd >= 0:
                    os.close(fd)

        if not pdf_plans or unsafe:
            return None

        plans: list[PlannedFile] = list(pdf_plans)
        for name in sorted(AUXILIARY_FILENAMES.intersection(names)):
            manifest = _read_manifest(directory_fd, name)
            if manifest is None or not _manifest_matches(name, manifest[0], directory_name, pdf_plans):
                kept.append({"path": str(Path(directory_path) / name), "reason": "manifest_not_verified"})
                unsafe = True
                continue
            content, digest, snapshot = manifest
            if (
                snapshot.st_mtime_ns > marker_stable.st_mtime_ns
                or snapshot.st_ctime_ns > marker_stable.st_ctime_ns
            ):
                kept.append({"path": str(Path(directory_path) / name), "reason": "changed_after_completion_marker"})
                unsafe = True
                continue
            plans.append(
                PlannedFile(
                    directory_path,
                    directory_name,
                    directory_snapshot.st_dev,
                    directory_snapshot.st_ino,
                    name,
                    snapshot.st_dev,
                    snapshot.st_ino,
                    len(content),
                    digest,
                    "manifest",
                    None,
                )
            )

        expected_names = set(pdf_names) | AUXILIARY_FILENAMES.intersection(names) | {COMPLETION_MARKER}
        if set(names) != expected_names:
            for name in sorted(set(names) - expected_names):
                kept.append({"path": str(Path(directory_path) / name), "reason": "unsupported_entry"})
            unsafe = True
        if unsafe:
            return None

        plans.append(
            PlannedFile(
                directory_path,
                directory_name,
                directory_snapshot.st_dev,
                directory_snapshot.st_ino,
                COMPLETION_MARKER,
                marker_stable.st_dev,
                marker_stable.st_ino,
                marker_stable.st_size,
                marker_digest,
                "marker",
                None,
            )
        )

        planned_names = {item.file_name for item in plans}
        return PlannedDirectory(
            directory_path,
            directory_name,
            directory_snapshot.st_dev,
            directory_snapshot.st_ino,
            directory_snapshot.st_mtime_ns,
            marker_stable.st_dev,
            marker_stable.st_ino,
            marker_stable.st_ctime_ns,
            tuple(plans),
            planned_names == set(names),
        )
    except OSError as exc:
        kept.append({"path": directory_path, "reason": f"directory_inspection_failed:{exc}"})
        return None
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _restore_quarantine(directory_fd: int, quarantine_name: str, original_name: str) -> str:
    try:
        os.link(
            quarantine_name,
            original_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(quarantine_name, dir_fd=directory_fd)
        return original_name
    except FileExistsError:
        pass
    recovery_name = f".cleanup-recovered-{uuid4().hex}-{original_name}"
    os.link(
        quarantine_name,
        recovery_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.unlink(quarantine_name, dir_fd=directory_fd)
    return recovery_name


def _validate_quarantine(directory_fd: int, item: PlannedFile, quarantine_name: str) -> None:
    fd, _ = _open_regular_at(directory_fd, quarantine_name)
    try:
        digest, snapshot = _stable_digest_fd(fd)
    finally:
        os.close(fd)
    if (
        (snapshot.st_dev, snapshot.st_ino) != (item.file_device, item.file_inode)
        or snapshot.st_size != item.file_size
        or digest != item.sha256
    ):
        raise OSError("quarantined file identity changed")


def _rollback_quarantined(
    directory_fd: int,
    quarantined: Sequence[tuple[PlannedFile, str]],
    rollback_names: Sequence[tuple[PlannedFile, str]],
) -> None:
    rollback_by_name = {item.file_name: name for item, name in rollback_names}
    for item, quarantine_name in reversed(quarantined):
        source_name = rollback_by_name.get(item.file_name, quarantine_name)
        try:
            recovery = _restore_quarantine(directory_fd, source_name, item.file_name)
            logging.warning("Restored quarantined file as %s", recovery)
        except OSError:
            logging.exception("Unable to restore quarantine file %s", source_name)
        if quarantine_name != source_name and quarantine_name in os.listdir(directory_fd):
            try:
                os.unlink(quarantine_name, dir_fd=directory_fd)
            except OSError:
                logging.exception("Unable to remove duplicate quarantine file %s", quarantine_name)


def _apply_directory_plan(
    plan: PlannedDirectory,
    outputs_fd: int,
    storage_root: Path,
    storage_fd: int | None,
    registered: Sequence[RegisteredFile],
) -> tuple[list[Deletion], list[dict[str, str]], bool]:
    deleted: list[Deletion] = []
    kept: list[dict[str, str]] = []
    directory_fd = -1
    artifact_lock_fd = -1
    removed = False
    quarantined: list[tuple[PlannedFile, str]] = []
    rollback_names: list[tuple[PlannedFile, str]] = []
    try:
        directory_fd = os.open(plan.directory_name, DIRECTORY_FLAGS, dir_fd=outputs_fd)
        snapshot = os.fstat(directory_fd)
        marker_snapshot = os.stat(COMPLETION_MARKER, dir_fd=directory_fd, follow_symlinks=False)
        if (
            (snapshot.st_dev, snapshot.st_ino) != (plan.directory_device, plan.directory_inode)
            or snapshot.st_mtime_ns != plan.directory_mtime_ns
            or (marker_snapshot.st_dev, marker_snapshot.st_ino) != (plan.marker_device, plan.marker_inode)
            or marker_snapshot.st_ctime_ns != plan.marker_ctime_ns
        ):
            kept.append({"path": plan.directory_path, "reason": "directory_changed_after_planning"})
            return [], kept, False

        # Stage every file first. No submitted file is removed until every item
        # and every formal PDF copy has passed final verification.
        for item in plan.files:
            quarantine_name = f".cleanup-pending-{uuid4().hex}"
            os.rename(item.file_name, quarantine_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            quarantined.append((item, quarantine_name))
            _validate_quarantine(directory_fd, item, quarantine_name)

        artifact_lock_fd = _lock_artifact_deletions(storage_fd)
        for item, quarantine_name in quarantined:
            _validate_quarantine(directory_fd, item, quarantine_name)
            if item.kind == "pdf":
                formal = _verified_formal_copy(
                    item.sha256, item.file_size, storage_root, storage_fd, registered
                )
                if formal is None:
                    raise OSError("formal storage copy no longer verifies")

        for item, quarantine_name in quarantined:
            rollback_name = f".cleanup-rollback-{uuid4().hex}"
            os.link(
                quarantine_name,
                rollback_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            rollback_names.append((item, rollback_name))

        for item, quarantine_name in quarantined:
            _validate_quarantine(directory_fd, item, quarantine_name)
            os.unlink(quarantine_name, dir_fd=directory_fd)

        deleted.extend(
            Deletion(
                str(Path(plan.directory_path) / item.file_name),
                item.sha256,
                item.formal_storage_copy or "manifest-only",
            )
            for item, _ in quarantined
        )

        rollback_cleanup_failed = False
        for _, rollback_name in rollback_names:
            try:
                os.unlink(rollback_name, dir_fd=directory_fd)
            except OSError as exc:
                rollback_cleanup_failed = True
                kept.append({"path": str(Path(plan.directory_path) / rollback_name), "reason": f"rollback_cleanup_failed:{exc}"})
        if rollback_cleanup_failed:
            return deleted, kept, False
        if plan.remove_directory and not os.listdir(directory_fd):
            current = os.stat(plan.directory_name, dir_fd=outputs_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (plan.directory_device, plan.directory_inode):
                os.rmdir(plan.directory_name, dir_fd=outputs_fd)
                removed = True
        return deleted, kept, removed
    except OSError as exc:
        if quarantined:
            _rollback_quarantined(directory_fd, quarantined, rollback_names)
        kept.append({"path": plan.directory_path, "reason": f"directory_apply_failed:{exc}"})
        return [], kept, False
    finally:
        if artifact_lock_fd >= 0:
            fcntl.flock(artifact_lock_fd, fcntl.LOCK_UN)
            os.close(artifact_lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def cleanup_download_staging(
    project_root: Path,
    retention_days: int,
    registered: Sequence[RegisteredFile],
    *,
    apply: bool,
    now: datetime | None = None,
) -> CleanupResult:
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    cutoff = (now.astimezone(timezone.utc) - timedelta(days=retention_days)).timestamp()
    outputs_root, storage_root = _project_roots(project_root)
    if not outputs_root.is_dir():
        return CleanupResult(not apply, retention_days, 0, [], [], [])

    outputs_fd = os.open(outputs_root, DIRECTORY_FLAGS)
    lock_fd = -1
    storage_fd = -1
    try:
        lock_fd = os.open(
            ".download-staging-cleanup.lock",
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600,
            dir_fd=outputs_fd,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if storage_root.is_dir():
            storage_fd = os.open(storage_root, DIRECTORY_FLAGS)
        directory_plans: list[PlannedDirectory] = []
        kept: list[dict[str, str]] = []
        names = sorted(os.listdir(outputs_fd))
        scanned = 0
        for name in names:
            if not is_staging_name(name):
                continue
            try:
                snapshot = os.stat(name, dir_fd=outputs_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(snapshot.st_mode):
                continue
            scanned += 1
            plan = _plan_directory(
                name,
                outputs_root,
                outputs_fd,
                storage_root,
                storage_fd if storage_fd >= 0 else None,
                registered,
                cutoff,
                kept,
            )
            if plan is not None:
                directory_plans.append(plan)

        deleted: list[Deletion] = []
        removed_directories: list[str] = []
        if apply:
            for plan in directory_plans:
                plan_deleted, plan_kept, removed = _apply_directory_plan(
                    plan,
                    outputs_fd,
                    storage_root,
                    storage_fd if storage_fd >= 0 else None,
                    registered,
                )
                deleted.extend(plan_deleted)
                kept.extend(plan_kept)
                if removed:
                    removed_directories.append(plan.directory_path)
        else:
            for plan in directory_plans:
                deleted.extend(
                    Deletion(str(Path(plan.directory_path) / item.file_name), item.sha256, item.formal_storage_copy or "manifest-only")
                    for item in plan.files
                )
                if plan.remove_directory:
                    removed_directories.append(plan.directory_path)

        return CleanupResult(
            dry_run=not apply,
            retention_days=retention_days,
            scanned_directories=scanned,
            deleted=deleted,
            removed_directories=removed_directories,
            kept=kept,
        )
    finally:
        if storage_fd >= 0:
            os.close(storage_fd)
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(outputs_fd)


def configure_logging(project_root: Path) -> None:
    log_dir = project_root / "outputs" / "cleanup-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    rotating = RotatingFileHandler(
        log_dir / "download-staging-cleanup.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, rotating])


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Literature AI project root",
    )
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("--apply", action="store_true", help="Delete planned files; default is dry-run")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        outputs_root, _ = _project_roots(project_root)
    except Exception as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    configure_logging(project_root)
    directories = staging_directories(outputs_root)
    if not directories:
        result = CleanupResult(not args.apply, args.retention_days, 0, [], [], [])
        logging.info("No download staging directories found")
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.retention_days)).timestamp()
        needs_registered_files = any(directory.stat().st_mtime < cutoff for directory in directories)
        try:
            registered = load_registered_files(project_root) if needs_registered_files else []
            result = cleanup_download_staging(
                project_root,
                args.retention_days,
                registered,
                apply=args.apply,
            )
        except Exception:
            logging.exception("Download staging cleanup aborted during planning; no deletion was attempted")
            return 2
    payload = asdict(result)
    logging.info("Cleanup result: %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from app.services.artifact_delete_lock import LOCK_FILENAME, artifact_delete_lock


class StubSettings:
    def __init__(self, root: Path) -> None:
        self.storage_paths = {"pdf": root / "storage" / "pdf"}


@pytest.mark.no_test_database
def test_artifact_delete_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    settings = StubSettings(tmp_path)
    lock_path = settings.storage_paths["pdf"].parent / LOCK_FILENAME

    with artifact_delete_lock(settings):
        probe = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    probe = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


@pytest.mark.no_test_database
def test_artifact_delete_lock_falls_back_to_read_only_lock_file(tmp_path: Path) -> None:
    settings = StubSettings(tmp_path)
    storage_root = settings.storage_paths["pdf"].parent
    storage_root.mkdir(parents=True)
    lock_path = storage_root / LOCK_FILENAME
    lock_path.write_bytes(b"")
    lock_path.chmod(0o444)

    with artifact_delete_lock(settings):
        assert lock_path.stat().st_mode & 0o777 == 0o444

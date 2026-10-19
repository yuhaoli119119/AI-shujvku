from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from typing import Iterator

from app.config import Settings


LOCK_FILENAME = ".artifact-delete.lock"


@contextmanager
def artifact_delete_lock(settings: Settings) -> Iterator[None]:
    """Serialize application deletions with download-staging cleanup."""
    storage_root = settings.storage_paths["pdf"].parent
    storage_root.mkdir(parents=True, exist_ok=True)
    lock_path = storage_root / LOCK_FILENAME
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o666)
    except PermissionError:
        # A host-side cleanup process may own a read-only lock file. flock works
        # on a read-only descriptor and does not require mutating the lock bytes.
        fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

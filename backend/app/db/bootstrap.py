from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from typing import Iterator

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class BootstrapOutcome:
    """Observable result of one database bootstrap attempt."""

    initialized: bool
    skipped: bool = False
    required_failures: tuple[str, ...] = ()
    optional_failures: tuple[str, ...] = ()


class DatabaseBootstrapError(RuntimeError):
    """Raised when a required schema/bootstrap step did not complete."""

    def __init__(self, failures: list[str] | tuple[str, ...]) -> None:
        self.failures = tuple(failures)
        super().__init__("Required database bootstrap steps failed: " + ", ".join(self.failures))


def bootstrap_lock_key(database_url: str) -> int:
    """Return a stable signed bigint key for PostgreSQL advisory locking."""

    digest = hashlib.sha256(database_url.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def database_bootstrap_lock(engine: Engine, database_url: str) -> Iterator[None]:
    """Serialize bootstrap attempts across backend and worker processes.

    PostgreSQL advisory locks are session scoped, so the dedicated connection
    must remain open until every bootstrap step has completed or failed.
    """

    key = bootstrap_lock_key(database_url)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

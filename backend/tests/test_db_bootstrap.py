from __future__ import annotations

import threading

import pytest
from sqlalchemy import text

from app.db.bootstrap import bootstrap_lock_key, database_bootstrap_lock
from app.db import session as db_session
from app.services import paper_codes

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_required_paper_code_backfill_failure_is_retryable(setup_test_db, monkeypatch):
    database_url = setup_test_db.url.render_as_string(hide_password=False)
    db_session._initialized_urls.discard(database_url)
    calls = 0

    def fail_backfill(_session):
        nonlocal calls
        calls += 1
        raise RuntimeError("required paper code backfill failed")

    monkeypatch.setattr(paper_codes, "ensure_paper_codes", fail_backfill)

    with pytest.raises(RuntimeError, match="required paper code backfill failed"):
        db_session.init_db(database_url, force=True)

    assert database_url not in db_session._initialized_urls

    def successful_backfill(_session):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(paper_codes, "ensure_paper_codes", successful_backfill)
    outcome = db_session.init_db(database_url)

    assert outcome.initialized is True
    assert outcome.required_failures == ()
    assert database_url in db_session._initialized_urls
    assert calls == 2


def test_database_bootstrap_advisory_lock_serializes_connections(setup_test_db):
    database_url = setup_test_db.url.render_as_string(hide_password=False)
    first_acquired = threading.Event()
    allow_first_release = threading.Event()
    second_acquired = threading.Event()
    failures: list[BaseException] = []

    def first_holder() -> None:
        try:
            with database_bootstrap_lock(setup_test_db, database_url):
                first_acquired.set()
                assert allow_first_release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - surfaced in parent thread
            failures.append(exc)

    def second_holder() -> None:
        try:
            assert first_acquired.wait(timeout=5)
            with database_bootstrap_lock(setup_test_db, database_url):
                second_acquired.set()
        except BaseException as exc:  # pragma: no cover - surfaced in parent thread
            failures.append(exc)

    first = threading.Thread(target=first_holder, daemon=True)
    second = threading.Thread(target=second_holder, daemon=True)
    first.start()
    second.start()

    assert first_acquired.wait(timeout=5)
    assert second_acquired.wait(timeout=0.25) is False
    allow_first_release.set()
    assert second_acquired.wait(timeout=5)
    first.join(timeout=5)
    second.join(timeout=5)

    assert failures == []
    assert first.is_alive() is False
    assert second.is_alive() is False


def test_database_bootstrap_advisory_lock_releases_after_exception(setup_test_db):
    database_url = setup_test_db.url.render_as_string(hide_password=False)
    key = bootstrap_lock_key(database_url)

    with pytest.raises(RuntimeError, match="bootstrap exploded"):
        with database_bootstrap_lock(setup_test_db, database_url):
            raise RuntimeError("bootstrap exploded")

    with setup_test_db.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        assert acquired is True
        released = connection.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        assert released is True

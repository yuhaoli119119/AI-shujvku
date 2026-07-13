from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def pytest_collection_modifyitems(items):
    """Expose the suite's real database boundary to marker-based runners."""
    for item in items:
        if item.get_closest_marker("no_test_database"):
            item.add_marker(pytest.mark.unit)
        else:
            item.add_marker(pytest.mark.postgres)


@pytest.fixture(scope="session")
def shared_test_database():
    """Create one isolated schema per pytest process, not one full schema per test."""
    from app.config import get_settings

    base_url = os.getenv("LITAI_TEST_ROOT_DATABASE_URL") or get_settings().database_url
    parsed = make_url(base_url)
    if not parsed.drivername.startswith("postgresql"):
        raise RuntimeError("Tests require PostgreSQL")

    schema = f"pytest_{uuid4().hex}"
    admin_engine = create_engine(base_url, future=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    query = dict(parsed.query)
    query["options"] = f"-csearch_path={schema},public"
    test_url = parsed.set(query=query).render_as_string(hide_password=False)
    test_engine = create_engine(test_url, future=True)
    from app.db.models import Base

    Base.metadata.create_all(test_engine, checkfirst=False)
    old_database_url = os.environ.get("LITAI_DATABASE_URL")
    old_test_database_url = os.environ.get("LITAI_TEST_DATABASE_URL")
    os.environ["LITAI_DATABASE_URL"] = test_url
    os.environ["LITAI_TEST_DATABASE_URL"] = test_url

    get_settings.cache_clear()
    try:
        yield PostgreSQLTestDatabase(
            url=test_url,
            engine=test_engine,
            session_factory=sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True),
            schema=schema,
        )
    finally:
        if old_database_url is None:
            os.environ.pop("LITAI_DATABASE_URL", None)
        else:
            os.environ["LITAI_DATABASE_URL"] = old_database_url
        if old_test_database_url is None:
            os.environ.pop("LITAI_TEST_DATABASE_URL", None)
        else:
            os.environ["LITAI_TEST_DATABASE_URL"] = old_test_database_url
        get_settings.cache_clear()
        test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture(autouse=True)
def default_test_database_mode(request, shared_test_database):
    if request.node.get_closest_marker("no_test_database"):
        yield
        return

    from app.config import get_settings
    from app.db.models import Base
    from app.db.session import _engines, _initialized_urls, _session_factories

    get_settings.cache_clear()
    try:
        yield shared_test_database
    finally:
        for engine in list(_engines.values()):
            engine.dispose()
        _engines.clear()
        _session_factories.clear()
        _initialized_urls.clear()
        get_settings.cache_clear()
        shared_test_database.engine.dispose()

        base_table_names = {table.name for table in Base.metadata.sorted_tables}
        with shared_test_database.engine.begin() as connection:
            existing_names = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                {"schema": shared_test_database.schema},
            ).scalars().all()
            extra_names = sorted(set(existing_names) - base_table_names)
            for table_name in extra_names:
                escaped = table_name.replace('"', '""')
                connection.execute(
                    text(f'DROP TABLE IF EXISTS "{shared_test_database.schema}"."{escaped}" CASCADE')
                )
            remaining_names = sorted(set(existing_names) & base_table_names)
            if remaining_names:
                qualified = ", ".join(
                    f'"{shared_test_database.schema}"."{name.replace(chr(34), chr(34) * 2)}"'
                    for name in remaining_names
                )
                connection.execute(text(f"TRUNCATE TABLE {qualified} RESTART IDENTITY CASCADE"))
        Base.metadata.create_all(shared_test_database.engine, checkfirst=True)


@dataclass
class PostgreSQLTestDatabase:
    url: str
    engine: object
    session_factory: sessionmaker
    schema: str


@pytest.fixture
def postgres_test_database(monkeypatch):
    """Create the application schema inside the test's isolated PostgreSQL namespace."""
    from app.config import get_settings
    from app.db.models import Base

    test_url = os.environ["LITAI_TEST_DATABASE_URL"]
    engine = create_engine(test_url, future=True)
    Base.metadata.create_all(engine, checkfirst=False)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    try:
        yield PostgreSQLTestDatabase(
            url=test_url,
            engine=engine,
            session_factory=factory,
            schema=make_url(test_url).query["options"].split("=", 1)[1].split(",", 1)[0],
        )
    finally:
        get_settings.cache_clear()
        engine.dispose()


@pytest.fixture
def setup_test_db(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.db.session import get_db_session
    from app.main import app

    test_url = os.environ["LITAI_TEST_DATABASE_URL"]
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("LITAI_DATABASE_URL", test_url)
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("LITAI_LOCAL_INGEST_ROOTS", str(tmp_path))
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "true")
    monkeypatch.setenv(
        "LITAI_MCP_API_KEYS",
        "reader|Reader|test-reader-key|read_papers;"
        "primary_ai|Primary AI|test-primary-repair-key|read_papers,repair_dft_issues;"
        "audit_ai|Audit AI|test-audit-only-key|read_papers,review_dft;"
        "correction_reviewer|Correction Reviewer|test-correction-only-key|read_papers,review_corrections;"
        "ordinary_ide_ai|Ordinary IDE AI|test-propose-only-key|read_papers,append_notes,propose_corrections;"
        "dft_primary_repair|DFT Primary Repair AI|test-primary-repair-e2e-key|read_papers,repair_dft_issues;"
        "assigned_dft_audit|Assigned DFT Audit AI|test-audit-only-e2e-key|read_papers,review_dft",
    )
    get_settings.cache_clear()

    engine = create_engine(test_url, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_get_db_session():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    try:
        yield engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        get_settings.cache_clear()

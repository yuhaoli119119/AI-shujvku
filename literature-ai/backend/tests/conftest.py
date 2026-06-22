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


@pytest.fixture(autouse=True)
def default_test_database_mode(monkeypatch):
    from app.config import get_settings
    from app.db.session import _engines, _session_factories

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
    monkeypatch.setenv("LITAI_DATABASE_URL", test_url)
    monkeypatch.setenv("LITAI_TEST_DATABASE_URL", test_url)

    get_settings.cache_clear()
    try:
        yield
    finally:
        for engine in list(_engines.values()):
            engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()
        test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


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

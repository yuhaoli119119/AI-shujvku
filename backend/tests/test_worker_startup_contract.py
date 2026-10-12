from __future__ import annotations

from pathlib import Path
import re
import asyncio
import importlib

import pytest


pytestmark = pytest.mark.no_test_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _compose_service_block(compose_text: str, service_name: str) -> str:
    lines = compose_text.splitlines()
    start = lines.index(f"  {service_name}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z0-9_-]+:$", lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def test_worker_import_never_runs_database_schema_bootstrap() -> None:
    worker_source = (
        PROJECT_ROOT / "backend" / "app" / "workers" / "celery_app.py"
    ).read_text(encoding="utf-8")
    assert "activate_active_library_database" not in worker_source
    assert "init_db" not in worker_source


def test_backend_lifespan_checks_existing_schema_without_initializing(monkeypatch):
    main = importlib.import_module("app.main")
    from app.db import session as db
    from app.utils import active_database
    queries = []
    def forbidden(*args, **kwargs):
        raise AssertionError("startup must never initialize or execute DDL")
    monkeypatch.setattr(db, "init_db", forbidden)
    monkeypatch.setattr(active_database, "activate_active_library_database", forbidden)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def begin(self): return self
        def rollback(self): pass
        def execute(self, statement):
            sql = str(statement)
            if not (sql.startswith("SELECT ") or sql == "SET TRANSACTION READ ONLY"):
                forbidden()
            queries.append(sql)
    class Engine:
        def connect(self): return Connection()
    monkeypatch.setattr(db, "get_engine", lambda url: Engine())
    info = {"db_kind": "postgresql", "active_library": "测试现有库", "db_url_masked": "testhost/testdb"}
    calls = []
    monkeypatch.setattr(main, "get_active_database_info", lambda: calls.append("read_info") or info)
    monkeypatch.setattr(main, "apply_persisted_settings_to_runtime", lambda: None)
    async def start():
        async with main.lifespan(main.app):
            assert calls == ["read_info"]
    asyncio.run(start())
    assert queries[0] == "SET TRANSACTION READ ONLY"
    assert len(queries) == 5
    assert all("LIMIT" in sql for sql in queries[1:])


@pytest.mark.parametrize("failure", ["database", "papers", "paper_sections", "audit_logs", "paper_sections.section_level"])
def test_backend_missing_schema_fails_without_ddl_or_initialization(monkeypatch, failure):
    main = importlib.import_module("app.main")
    from app.db import session as db
    def forbidden(*args, **kwargs):
        raise AssertionError("no initialization or false healthy fallback")
    class BrokenConnection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def begin(self): return self
        def rollback(self): pass
        def execute(self, statement):
            sql = str(statement)
            if not (sql.startswith("SELECT ") or sql == "SET TRANSACTION READ ONLY"):
                forbidden()
            if failure.split(".")[0] in sql and sql.startswith("SELECT "):
                raise RuntimeError(f"synthetic unavailable schema: {failure}")
    class BrokenEngine:
        def connect(self):
            if failure == "database":
                raise RuntimeError("synthetic missing database")
            return BrokenConnection()
    monkeypatch.setattr(db, "get_engine", lambda url: BrokenEngine())
    monkeypatch.setattr(db, "init_db", forbidden)
    monkeypatch.setattr(main, "get_active_database_info", forbidden)
    async def start():
        with pytest.raises(RuntimeError, match="database_schema_not_ready"):
            async with main.lifespan(main.app):
                forbidden()
    asyncio.run(start())


@pytest.mark.parametrize("service_name", ["worker", "worker-pdf"])
def test_compose_workers_wait_for_healthy_backend(service_name: str) -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service_block = _compose_service_block(compose_text, service_name)

    assert re.search(
        r"(?m)^      backend:\n        condition: service_healthy$",
        service_block,
    )

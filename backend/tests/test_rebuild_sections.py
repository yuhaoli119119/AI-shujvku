"""Exercise the real API/authentication and PostgreSQL transaction boundary.

All text/PDF fixtures are synthetic, in the isolated test DB and storage only.
"""
from __future__ import annotations

import copy
import hashlib
import zipfile
import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import AuditLog, Paper, PaperSection, RebuildDataRow, RebuildDataValue, RebuildPaperFile
from app.main import app
from app.security.htpasswd import hash_password, write_htpasswd
from app.security.session_auth import create_session
from app.services.paper_detail_readonly import PaperDetailReadonlyService
from app.services.rebuild_sections import EVENT, RECEIPT, list_sections


@pytest.fixture
def chapter_env(setup_test_db, tmp_path, monkeypatch):
    auth_file = tmp_path / "owner.htpasswd"
    write_htpasswd(auth_file, {"chapter_tester": hash_password("test-only-password")})
    for key, value in {"LITAI_AUTH_ENABLED": "true", "LITAI_AUTH_HTPASSWD_FILE": str(auth_file),
                       "LITAI_AUTH_SESSION_SECRET": "isolated-chapter-test-secret",
                       "LITAI_AUTH_REDIS_URL": "", "LITAI_AUTH_COOKIE_SECURE": "false"}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    settings = get_settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    factory = sessionmaker(bind=setup_test_db, autoflush=False)
    with factory() as session:
        paper = Paper(title="Synthetic chapter test", library_name="默认文献库", paper_code="T901", pdf_path="main.pdf")
        alien = Paper(title="Another synthetic paper", library_name="默认文献库", paper_code="T902", pdf_path="alien.pdf")
        session.add_all([paper, alien]); session.flush()
        files = []
        for owner, role, name in [(paper, "main", "main.pdf"), (paper, "si", "si.pdf"), (alien, "main", "alien.pdf")]:
            path = settings.storage_root / name
            with fitz.open() as doc:
                for number in range(1, 3):
                    doc.new_page().insert_text((30, 30), f"Synthetic {name} page {number}. Original paragraph.")
                doc.save(path)
            file = RebuildPaperFile(paper_id=owner.id, role=role, storage_path=name, original_filename=name,
                                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(), file_size=path.stat().st_size)
            session.add(file); session.flush(); files.append(file)
        legacy = PaperSection(paper_id=paper.id, section_title="Legacy chapter", text="Legacy bytes\n unchanged", page_start=1, page_end=1)
        row = RebuildDataRow(paper_id=paper.id, row_key="old", reaction="SRR", material="Synthetic", active_site_type="single_atom", data_type="dft")
        session.add_all([legacy, row]); session.flush()
        value = RebuildDataValue(paper_id=paper.id, row_id=row.id, field_name="synthetic_value", raw_value="1.2300", numeric_value=1.23, unit="eV")
        session.add(value); session.commit()
        ids = {"paper_id": paper.id, "legacy_id": legacy.id, "value_id": value.id,
               "main": files[0].id, "si": files[1].id, "alien": files[2].id,
               "hashes": {str(f.id): f.sha256 for f in files}}
    issued = create_session("chapter_tester", settings=settings)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("litai_session", issued.token)
    client.cookies.set("litai_csrf", issued.csrf)
    client.headers.update({"X-CSRF-Token": issued.csrf, "Origin": "http://testserver"})
    ids.update(client=client, factory=factory, settings=settings, tmp_path=tmp_path)
    yield ids
    client.close()


def body(env, *, file="main", key="intro", request="chapter-request-1"):
    fid = env[file]
    return {"request_id": request, "sections": [{"file_id": str(fid), "source_sha256": env["hashes"][str(fid)],
             "section_key": key, "page_basis": "source_pdf", "section_title": "Original heading",
             "text": " Original paragraph.\nSecond original line. ", "explanation_zh": "合成测试中文解读，待核验。",
             "page_start": 1, "page_end": 1,
             "sources": [{"page_number": 1, "quote": "Original paragraph.", "locator": "Original heading, first paragraph"}]}]}


def post(env, payload):
    return env["client"].post(f'/api/rebuild/papers/{env["paper_id"]}/sections/import', json=payload)


def counts(env):
    with env["factory"]() as session:
        return [session.scalar(select(func.count(PaperSection.id))),
                session.scalar(select(func.count(AuditLog.id)).where(
                    AuditLog.paper_id == env["paper_id"], AuditLog.action.in_([EVENT, RECEIPT])))]


def test_real_session_csrf_and_work_package_auth(chapter_env):
    env = chapter_env
    path = f'/api/rebuild/papers/{env["paper_id"]}'
    anonymous = TestClient(app)
    assert anonymous.get(path + "/ai-work-package").status_code == 401
    assert anonymous.get(path + "/sections").status_code == 401
    assert anonymous.post(path + "/sections/import", json=body(env)).status_code == 401
    anonymous.headers["X-MCP-Key"] = "test-reader-key"
    assert anonymous.post(path + "/sections/import", json=body(env)).status_code == 401
    anonymous.close()
    env["client"].headers.pop("X-CSRF-Token")
    assert post(env, body(env)).status_code == 403
    env["client"].headers["X-CSRF-Token"] = "wrong"
    assert post(env, body(env)).status_code == 403
    env["client"].headers["X-CSRF-Token"] = env["client"].cookies.get("litai_csrf")
    env["client"].headers["Origin"] = "https://foreign.invalid"
    assert post(env, body(env)).status_code == 403
    assert counts(env) == [1, 0]


def test_append_replay_audit_and_mature_readonly_compatibility(chapter_env):
    env = chapter_env; payload = body(env)
    response = post(env, payload)
    assert response.status_code == 200, response.text
    section_id = response.json()["items"][0]["section_id"]
    assert response.json()["created"] == 1
    assert counts(env) == [2, 2]
    retry = post(env, copy.deepcopy(payload))
    assert retry.json()["replayed"] and retry.json()["created"] == 0
    assert counts(env) == [2, 2]
    payload["request_id"] = "different-request-same-chapter"
    assert post(env, payload).json()["created"] == 0
    assert counts(env) == [2, 3]
    with env["factory"]() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        detail = PaperDetailReadonlyService(session).get_paper_detail(env["paper_id"])
        chapters = {str(s.id): s for s in detail.sections}
        assert chapters[str(env["legacy_id"])].text == "Legacy bytes\n unchanged"
        assert chapters[str(env["legacy_id"])].provenance is None
        new = chapters[section_id]
        assert new.text == payload["sections"][0]["text"]
        assert new.explanation_zh == payload["sections"][0]["explanation_zh"]
        assert new.provenance["file_id"] == str(env["main"])
        assert new.provenance["sources"] == payload["sections"][0]["sources"]
        assert new.provenance["actor"] == "chapter_tester"
        assert new.scientific_acceptance == "not_implied"
        value = session.get(RebuildDataValue, env["value_id"])
        assert (value.raw_value, value.numeric_value, value.unit) == ("1.2300", 1.23, "eV")
        event_row = session.scalar(select(AuditLog).where(AuditLog.action == EVENT))
        assert event_row.payload["section"]["text"] == new.text
        assert "sid" not in event_row.payload and "csrf" not in event_row.payload
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        session.rollback()


@pytest.mark.parametrize("change,expected", [
    ("cross_paper", 400), ("wrong_claim_hash", 409), ("changed_bytes", 409),
    ("wrong_file_size", 400), ("physical_page", 400), ("wrong_basis", 400),
    ("blank_quote", 422), ("blank_locator", 422), ("missing_page_evidence", 422),
    ("boolean_page", 422), ("no_page", 422), ("unknown_field", 422),
])
def test_reject_invalid_source_without_any_partial_write(chapter_env, change, expected):
    env = chapter_env; payload = body(env); section = payload["sections"][0]
    if change == "cross_paper": payload = body(env, file="alien")
    elif change == "wrong_claim_hash": section["source_sha256"] = "a" * 64
    elif change == "changed_bytes": (env["settings"].storage_root / "main.pdf").write_bytes(b"modified test PDF")
    elif change == "wrong_file_size":
        with env["factory"]() as session:
            session.get(RebuildPaperFile, env["main"]).file_size += 1; session.commit()
    elif change == "physical_page":
        section.update(page_start=3, page_end=3); section["sources"][0]["page_number"] = 3
    elif change == "wrong_basis": section.update(page_basis="derived_pdf", reading_pdf_sha256=section["source_sha256"])
    elif change == "blank_quote": section["sources"][0]["quote"] = "  "
    elif change == "blank_locator": section["sources"][0]["locator"] = "  "
    elif change == "missing_page_evidence": section["page_end"] = 2
    elif change == "boolean_page": section["page_start"] = True
    elif change == "no_page": section.pop("page_start")
    elif change == "unknown_field": section["scientifically_accepted"] = True
    result = post(env, payload)
    assert result.status_code == expected, result.text
    assert counts(env) == [1, 0]


def test_request_collision_key_collision_and_explicit_retained_alternative(chapter_env):
    env = chapter_env; original = body(env)
    first = post(env, original).json(); first_id = first["items"][0]["section_id"]
    changed = copy.deepcopy(original); changed["sections"][0]["explanation_zh"] = "另一解释，须保留冲突。"
    assert post(env, changed).json()["detail"] == "section_request_id_reused"
    changed["request_id"] = "request-2"
    assert post(env, changed).json()["detail"] == "section_key_conflict"
    assert counts(env) == [2, 2]
    changed["sections"][0].update(section_key="intro-alternative", conflicts_with_section_ids=[first_id])
    assert post(env, changed).status_code == 200
    with env["factory"]() as session:
        items = list_sections(session, env["paper_id"])
        assert len(items) == 3
        assert next(s for s in items if s["id"] == first_id)["explanation_zh"] == original["sections"][0]["explanation_zh"]
        assert next(s for s in items if s["section_key"] == "intro-alternative")["conflicts_with_section_ids"] == [first_id]


def test_new_si_coverage_does_not_replace_main_or_infer_legacy_file(chapter_env):
    env = chapter_env
    assert post(env, body(env)).status_code == 200
    path = f'/api/rebuild/papers/{env["paper_id"]}/ai-work-package'
    package = env["client"].get(path).json()
    coverage = {f["file_id"]: f for f in package["section_coverage"]["files"]}
    assert len(coverage[str(env["main"])]["sections"]) == 1
    assert coverage[str(env["si"])]["sections"] == []
    assert package["section_coverage"]["legacy_section_ids_without_file"] == [str(env["legacy_id"])]
    assert package["section_write_contract"]["authentication"] == "workbench session + X-CSRF-Token + same-origin"
    assert post(env, body(env, file="si", request="si-request")).json()["created"] == 1
    assert counts(env) == [3, 4]
    assert post(env, body(env)).json()["created"] == 0


def test_batch_failure_rolls_back_first_chapter_and_audit(chapter_env):
    env = chapter_env; payload = body(env)
    payload["sections"].append(body(env, file="alien")["sections"][0])
    assert post(env, payload).status_code == 400
    assert counts(env) == [1, 0]


def test_audit_insert_failure_rolls_back_chapter(chapter_env):
    env = chapter_env
    def fail(mapper, connection, target):
        if target.action == EVENT:
            raise RuntimeError("synthetic audit storage failure")
    event.listen(AuditLog, "before_insert", fail)
    try:
        assert post(env, body(env)).status_code == 500
        assert counts(env) == [1, 0]
    finally:
        event.remove(AuditLog, "before_insert", fail)


def test_parallel_identical_requests_create_one_chapter_and_receipt(chapter_env):
    env = chapter_env; payload = body(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(env, payload), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert sum(r.json()["created"] for r in responses) == 1
    assert counts(env) == [2, 2]


def test_docx_requires_existing_derived_pdf_with_exact_hash(chapter_env):
    env = chapter_env; root = env["settings"].storage_root; path = root / "source.docx"
    with zipfile.ZipFile(path, "w") as docx:
        docx.writestr("[Content_Types].xml", "<Types/>")
        docx.writestr("word/document.xml", "<document>Synthetic paragraph</document>")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with env["factory"]() as session:
        record = RebuildPaperFile(paper_id=env["paper_id"], role="si", storage_path="source.docx",
                                  original_filename="source.docx", sha256=digest, file_size=path.stat().st_size)
        session.add(record); session.commit(); fid = record.id
    payload = body(env); section = payload["sections"][0]
    section.update(file_id=str(fid), source_sha256=digest, page_basis="derived_pdf", reading_pdf_sha256="a" * 64)
    assert post(env, payload).json()["detail"] == "derived_pdf_preview_unavailable"
    assert counts(env) == [1, 0]
    cache = root / "rebuild_previews" / f"{digest}.pdf"; cache.parent.mkdir()
    cache.write_bytes((root / "si.pdf").read_bytes())
    assert post(env, payload).json()["detail"] == "section_reading_pdf_sha256_mismatch"
    section["reading_pdf_sha256"] = hashlib.sha256(cache.read_bytes()).hexdigest()
    assert post(env, payload).status_code == 200
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    items = env["client"].get(f'/api/rebuild/papers/{env["paper_id"]}/sections').json()["items"]
    new = next(s for s in items if s["provenance"])
    assert new["provenance"]["page_basis"] == "derived_pdf"
    assert new["provenance"]["reading_pdf_sha256"] == section["reading_pdf_sha256"]


def test_actual_lifespan_established_postgres_only_reads(chapter_env, monkeypatch):
    env = chapter_env
    main = importlib.import_module("app.main")
    from app.db import session as db
    from app.utils import active_database
    registry = env["tmp_path"] / "test-registry.json"
    registry.write_text('{"active_library":"默认文献库","libraries":[]}', encoding="utf-8")
    monkeypatch.setattr(active_database, "canonical_registry_path", lambda: registry)
    def forbidden(*args, **kwargs):
        raise AssertionError("lifespan must not initialize/migrate")
    monkeypatch.setattr(db, "init_db", forbidden)
    monkeypatch.setattr(main, "apply_persisted_settings_to_runtime", lambda: None)
    engine = db.get_engine(env["settings"].database_url)
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        assert statement.startswith("SELECT ") or statement == "SET TRANSACTION READ ONLY", statement
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", observe)
    actual_info = main.get_active_database_info
    observed_info = []
    def capture_info():
        result = actual_info(); observed_info.append(result); return result
    monkeypatch.setattr(main, "get_active_database_info", capture_info)
    async def start():
        async with main.lifespan(main.app):
            assert observed_info[0]["configured_db_papers_total"] == 2
            assert observed_info[0]["active_library"] == "默认文献库"
            assert observed_info[0]["db_kind"] == "postgresql"
    try:
        asyncio.run(start())
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert counts(env) == [1, 0]

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
from uuid import UUID, uuid4

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
from app.services.rebuild_sections import (EVENT, RECEIPT, list_sections, CORRECTION_EVENT,
                                         CORRECTION_RECEIPT, CORRECTION_VERSION)


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
        assert all(s["display_status"] == "current" and not s["correction_history"] for s in items)


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


def correction_fixture(env):
    old = post(env, body(env)).json()['items'][0]['section_id']
    alternative = body(env, key='boundary-fixed', request='fixed-chapter')
    alternative['sections'][0].update(text='Original paragraph.', explanation_zh='AI边界纠正；本字符串不能自动历史化。',
                                      conflicts_with_section_ids=[old])
    new = post(env, alternative).json()['items'][0]['section_id']
    sections = {s['id']: s for s in env['client'].get(f'/api/rebuild/papers/{env["paper_id"]}/sections').json()['items']}
    return {'request_id': 'correction-request', 'corrections': [{
        'kind': 'extraction_boundary', 'file_id': str(env['main']),
        'source_sha256': env['hashes'][str(env['main'])],
        'old_section': {'section_id': old, 'content_sha256': sections[old]['provenance']['content_sha256']},
        'new_sections': [{'section_id': new, 'content_sha256': sections[new]['provenance']['content_sha256']}],
        'reason': 'The extraction included text beyond the printed heading; preserve the old bytes.',
        'sources': [{'page_number': 1, 'locator': 'Original heading, first paragraph', 'quote': 'Original paragraph.'}],
    }]}


def correction_post(env, payload):
    return env['client'].post(f'/api/rebuild/papers/{env["paper_id"]}/sections/corrections', json=payload)


def correction_counts(env):
    with env['factory']() as session:
        return tuple(session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == action))
                     for action in (CORRECTION_EVENT, CORRECTION_RECEIPT))


def immutable_snapshot(env):
    with env['factory']() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        return {model.__tablename__: [{c.name: getattr(row, c.name) for c in model.__table__.columns}
                for row in session.scalars(select(model).order_by(model.id))]
                for model in (Paper, PaperSection, RebuildPaperFile, RebuildDataRow, RebuildDataValue)} | {
            'imports': [{c.name: getattr(row, c.name) for c in AuditLog.__table__.columns}
                        for row in session.scalars(select(AuditLog).where(AuditLog.action.in_([EVENT, RECEIPT])).order_by(AuditLog.id))]}


def test_boundary_correction_authentication_csrf_and_origin(chapter_env):
    env = chapter_env; payload = correction_fixture(env)
    path = f'/api/rebuild/papers/{env["paper_id"]}/sections/corrections'
    with TestClient(app) as anonymous:
        assert anonymous.post(path, json=payload).status_code == 401
        anonymous.headers['X-MCP-Key'] = 'not-a-session'
        assert anonymous.post(path, json=payload).status_code == 401
    env['client'].headers.pop('X-CSRF-Token')
    assert correction_post(env, payload).status_code == 403
    env['client'].headers['X-CSRF-Token'] = 'wrong'
    assert correction_post(env, payload).status_code == 403
    env['client'].headers['X-CSRF-Token'] = env['client'].cookies.get('litai_csrf')
    env['client'].headers['Origin'] = 'https://foreign.invalid'
    assert correction_post(env, payload).status_code == 403
    assert correction_counts(env) == (0, 0)


def test_explicit_history_preservation_replay_and_readonly_projection(chapter_env):
    env = chapter_env; payload = correction_fixture(env); before = immutable_snapshot(env)
    with env['factory']() as session:
        assert all(s['display_status'] == 'current' for s in list_sections(session, env['paper_id']))
    response = correction_post(env, payload)
    assert response.status_code == 200, response.text
    assert response.json()['created'] == 1
    assert correction_counts(env) == (1, 1)
    replay = correction_post(env, copy.deepcopy(payload)).json()
    assert replay['replayed'] and replay['created'] == 0
    assert correction_counts(env) == (1, 1)
    changed = copy.deepcopy(payload); changed['corrections'][0]['reason'] += ' Changed.'
    assert correction_post(env, changed).status_code == 409
    changed['request_id'] = 'another-declaration'
    assert correction_post(env, changed).json()['detail'] == 'section_correction_relation_conflict'
    same = copy.deepcopy(payload); same['request_id'] = 'same-declaration-new-request'
    assert correction_post(env, same).json()['created'] == 0
    assert correction_counts(env) == (1, 2)
    assert immutable_snapshot(env) == before
    old = payload['corrections'][0]['old_section']['section_id']
    new = payload['corrections'][0]['new_sections'][0]['section_id']
    with env['factory']() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        detail = PaperDetailReadonlyService(session).get_paper_detail(env['paper_id'])
        sections = {str(s.id): s.model_dump(mode='json') for s in detail.sections}
        assert sections[old]['display_status'] == 'extraction_boundary_history'
        assert sections[new]['display_status'] == sections[str(env['legacy_id'])]['display_status'] == 'current'
        assert sections[new]['conflicts_with_section_ids'] == [old]
        assert sections[old]['correction_history'] == sections[new]['correction_history']
        relation = sections[old]['correction_history'][0]
        assert relation['reason'] == payload['corrections'][0]['reason']
        assert relation['sources'] == payload['corrections'][0]['sources']
        assert relation['actor'] == 'chapter_tester' and relation['version'] == CORRECTION_VERSION
        assert relation['old_section']['content_sha256'] == sections[old]['provenance']['content_sha256']
        assert sections[old]['scientific_acceptance'] == 'not_implied'
    package = env['client'].get(f'/api/rebuild/papers/{env["paper_id"]}/ai-work-package').json()
    assert package['section_write_contract']['boundary_correction']['version'] == CORRECTION_VERSION
    assert next(s for s in package['sections'] if s['id'] == old)['display_status'] == 'extraction_boundary_history'


@pytest.mark.parametrize('change,expected', [
    ('kind', 422), ('unknown', 422), ('blank_reason', 422), ('missing_source', 422),
    ('self', 422), ('hash', 409), ('file', 400), ('alien_file', 400), ('source_hash', 409),
    ('legacy', 400), ('alien_section', 400), ('source_page', 400), ('source_boolean', 422),
])
def test_correction_identity_and_source_contract(chapter_env, change, expected):
    env = chapter_env; payload = correction_fixture(env); c = payload['corrections'][0]
    if change == 'kind': c['kind'] = 'scientific_conflict'
    elif change == 'unknown': c['accepted'] = True
    elif change == 'blank_reason': c['reason'] = '  '
    elif change == 'missing_source': c['sources'] = []
    elif change == 'self': c['new_sections'] = [c['old_section']]
    elif change == 'hash': c['old_section']['content_sha256'] = 'f' * 64
    elif change == 'file': c['file_id'] = str(env['si']); c['source_sha256'] = env['hashes'][str(env['si'])]
    elif change == 'alien_file': c['file_id'] = str(env['alien']); c['source_sha256'] = env['hashes'][str(env['alien'])]
    elif change == 'source_hash': c['source_sha256'] = 'f' * 64
    elif change == 'legacy': c['old_section']['section_id'] = str(env['legacy_id'])
    elif change == 'alien_section': c['new_sections'][0]['section_id'] = str(uuid4())
    elif change == 'source_page': c['sources'][0]['page_number'] = 2
    elif change == 'source_boolean': c['sources'][0]['page_number'] = True
    before = immutable_snapshot(env)
    response = correction_post(env, payload)
    assert response.status_code == expected, response.text
    assert correction_counts(env) == (0, 0)
    assert immutable_snapshot(env) == before


def test_correction_no_science_conflict_preference_and_legacy_inference(chapter_env):
    env = chapter_env; payload = correction_fixture(env)
    science = body(env, key='science-conflict', request='science-conflict-request')
    science['sections'][0].update(text='Different original scientific candidate.',
        explanation_zh='章节边界纠正；即使写了这个字样也不能推断 supersede。',
        conflicts_with_section_ids=[payload['corrections'][0]['new_sections'][0]['section_id'], str(env['legacy_id'])])
    science_id = post(env, science).json()['items'][0]['section_id']
    assert correction_post(env, payload).status_code == 200
    items = env['client'].get(f'/api/rebuild/papers/{env["paper_id"]}/sections').json()['items']
    by_id = {s['id']: s for s in items}
    assert len(items) == 4
    assert by_id[science_id]['display_status'] == 'current' and by_id[science_id]['correction_history'] == []
    assert by_id[str(env['legacy_id'])]['display_status'] == 'current'
    assert by_id[science_id]['conflicts_with_section_ids'] == science['sections'][0]['conflicts_with_section_ids']
    assert by_id[payload['corrections'][0]['new_sections'][0]['section_id']]['display_status'] == 'current'


def test_correction_atomic_batch_rollback_and_receipt_failure(chapter_env):
    env = chapter_env; payload = correction_fixture(env); before = immutable_snapshot(env)
    bad = copy.deepcopy(payload); another = copy.deepcopy(bad['corrections'][0])
    another['old_section']['section_id'] = str(env['legacy_id'])
    bad['corrections'].append(another)
    assert correction_post(env, bad).status_code == 400
    assert correction_counts(env) == (0, 0)
    def fail(mapper, connection, target):
        if target.action == CORRECTION_RECEIPT:
            raise RuntimeError('Synthetic receipt failure')
    event.listen(AuditLog, 'before_insert', fail)
    try:
        assert correction_post(env, payload).status_code == 500
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    assert correction_counts(env) == (0, 0)
    assert immutable_snapshot(env) == before
    assert correction_post(env, payload).status_code == 200


def test_correction_concurrent_replay_and_cycle_rejection(chapter_env):
    env = chapter_env; payload = correction_fixture(env)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: correction_post(env, copy.deepcopy(payload)), range(2)))
    assert all(r.status_code == 200 for r in results)
    assert sorted(r.json()['created'] for r in results) == [0, 1]
    assert correction_counts(env) == (1, 1)
    reverse = copy.deepcopy(payload); reverse['request_id'] = 'reverse'
    c = reverse['corrections'][0]
    c['old_section'], c['new_sections'] = c['new_sections'][0], [c['old_section']]
    assert correction_post(env, reverse).json()['detail'] == 'section_correction_target_is_history'
    assert correction_counts(env) == (1, 1)


@pytest.mark.parametrize('change', ['bytes', 'size', 'audit_hash', 'stored_text', 'reading_hash'])
def test_correction_rejects_changed_source_or_import_snapshot(chapter_env, change):
    env = chapter_env; payload = correction_fixture(env)
    if change == 'bytes':
        (env['settings'].storage_root / 'main.pdf').write_bytes(b'%PDF-tampered-synthetic')
    else:
        with env['factory']() as session:
            if change == 'size':
                session.get(RebuildPaperFile, env['main']).file_size += 1
            elif change == 'stored_text':
                old = payload['corrections'][0]['old_section']['section_id']
                session.get(PaperSection, UUID(old)).text += ' Modified.'
            else:
                imported = session.scalar(select(AuditLog).where(AuditLog.action == EVENT))
                data = copy.deepcopy(imported.payload)
                data['content_sha256' if change == 'audit_hash' else 'reading_pdf_sha256'] = 'f' * 64
                imported.payload = data
            session.commit()
    before = immutable_snapshot(env)
    assert correction_post(env, payload).status_code in {400, 409}
    assert correction_counts(env) == (0, 0)
    assert immutable_snapshot(env) == before


def test_multi_old_shared_new_and_stale_declaration_fails_closed(chapter_env):
    env = chapter_env; payload = correction_fixture(env)
    another = body(env, key='another-overlapping-old', request='another-old-request')
    old2 = post(env, another).json()['items'][0]['section_id']
    sections = env['client'].get(f'/api/rebuild/papers/{env["paper_id"]}/sections').json()['items']
    c2 = copy.deepcopy(payload['corrections'][0])
    c2['old_section'] = {'section_id': old2, 'content_sha256': next(s for s in sections if s['id'] == old2)['provenance']['content_sha256']}
    payload['corrections'].append(c2)
    before = immutable_snapshot(env)
    assert correction_post(env, payload).json()['created'] == 2
    assert correction_counts(env) == (2, 1) and immutable_snapshot(env) == before
    # Simulate a corrupted explicit declaration in the isolated DB only. The
    # reader must keep the original chapter current rather than trust its kind.
    with env['factory']() as session:
        record = session.scalar(select(AuditLog).where(AuditLog.action == CORRECTION_EVENT,
                                                       AuditLog.target_id == old2))
        data = copy.deepcopy(record.payload); data['declaration']['kind'] = 'scientific_conflict'
        record.payload = data; session.commit()
    with env['factory']() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        items = {s['id']: s for s in list_sections(session, env['paper_id'])}
        assert items[old2]['display_status'] == 'current' and items[old2]['correction_history'] == []
        assert items[payload['corrections'][0]['old_section']['section_id']]['display_status'] == 'extraction_boundary_history'

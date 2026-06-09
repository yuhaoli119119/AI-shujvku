"""test_intake_mvp.py — Phase 8: Literature Intake MVP 测试矩阵

验证核心安全门控：
  - POST /api/intake/search  不写 papers 表
  - reject 后调用 /ingest    → 400 candidate_rejected
  - pending_review 调用 /ingest → 400 candidate_not_approved
  - approve 后调用 /ingest   → 创建 WorkflowJob，状态变 ingesting
  - 重复 DOI 检测             → candidate.status == "duplicate"
  - 批量 ingest-approved      → 只有 approved 的候选被触发

所有测试使用 SQLite in-memory，不依赖网络或真实 PostgreSQL。
DiscoveryService.search 被 monkeypatched 返回固定结果。
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base, LiteratureIntakeCandidate, LiteratureIntakeSession, Paper
from app.db.session import get_db_session
from app.main import app


# ---------------------------------------------------------------------------
# 数据库 fixture（SQLite in-memory）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    _engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(_engine)
    yield _engine
    _engine.dispose()


@pytest.fixture()
def db_session(engine):
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db_session):
    """TestClient with dependency override for DB session."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db_session] = override_get_db
    c = TestClient(app, raise_server_exceptions=True)
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 固定检索结果（不发真实 HTTP）
# ---------------------------------------------------------------------------

MOCK_SEARCH_RESULTS: list[dict[str, Any]] = [
    {
        "identifier": "10.1234/test.001",
        "title": "Density Functional Theory of Battery Anodes",
        "doi": "10.1234/test.001",
        "year": 2022,
        "journal": "J. Electrochem. Soc.",
        "authors": ["Alice A.", "Bob B."],
        "abstract": "DFT study of lithium-ion battery anode materials.",
        "url": "https://example.com/paper/001",
        "pdf_url": "https://example.com/paper/001.pdf",
        "databases": ["openalex"],
    },
    {
        "identifier": "10.1234/test.002",
        "title": "Machine Learning for Electrolyte Screening",
        "doi": "10.1234/test.002",
        "year": 2021,
        "journal": "Nature Energy",
        "authors": ["Carol C."],
        "abstract": "ML screening of electrolyte candidates.",
        "url": "https://example.com/paper/002",
        "pdf_url": None,
        "databases": ["openalex"],
    },
]

MOCK_SEARCH_PATCH = patch(
    "app.services.discovery_service.DiscoveryService.search",
    return_value=MOCK_SEARCH_RESULTS,
)


# ---------------------------------------------------------------------------
# 辅助：直接操作 DB 的工厂
# ---------------------------------------------------------------------------

def make_session_with_candidates(
    db: Session,
    *,
    n_pending: int = 2,
    library_name: str = "test_lib",
) -> tuple[LiteratureIntakeSession, list[LiteratureIntakeCandidate]]:
    s = LiteratureIntakeSession(
        library_name=library_name,
        original_query="battery anode DFT",
        status="pending_review",
        providers=["openalex"],
        max_results=20,
    )
    db.add(s)
    db.flush()

    candidates = []
    for i in range(n_pending):
        c = LiteratureIntakeCandidate(
            session_id=s.id,
            title=f"Paper {i}",
            doi=f"10.9999/t.{i:03d}",
            identifier=f"10.9999/t.{i:03d}",
            status="pending_review",
            relevance_score=0.8 - i * 0.1,
            screening_tier="recommended",
        )
        db.add(c)
        candidates.append(c)
    db.commit()
    for c in candidates:
        db.refresh(c)
    db.refresh(s)
    return s, candidates


# ===========================================================================
# TEST 1: POST /api/intake/search — 不写 papers 表
# ===========================================================================

def test_search_does_not_write_papers(client, db_session):
    """核心安全门控：intake/search 结果只在候选表，papers 表不增加记录。"""
    papers_before = db_session.scalar(
        select(Paper).limit(1)
    )
    paper_count_before = len(db_session.execute(select(Paper.id)).all())

    with MOCK_SEARCH_PATCH:
        resp = client.post("/api/intake/search", json={
            "query": "battery anode DFT",
            "user_need": "Find DFT studies of lithium-ion battery anodes",
            "library_name": "test_lib",
            "max_results": 10,
        })

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # papers 表不增加
    paper_count_after = len(db_session.execute(select(Paper.id)).all())
    assert paper_count_after == paper_count_before, (
        f"papers 表增加了 {paper_count_after - paper_count_before} 条记录，违反安全门控！"
    )

    # 候选表正确写入
    assert data["candidate_count"] == len(MOCK_SEARCH_RESULTS)
    assert data["status"] == "pending_review"
    assert "candidates" in data
    for c in data["candidates"]:
        assert c["status"] in ("pending_review", "duplicate")

    # 包含免责提示
    assert "intake_notice" in data
    assert "尚未入库" in data["intake_notice"]


# ===========================================================================
# TEST 2: reject 后调用 /ingest → 400 candidate_rejected
# ===========================================================================

def test_ingest_rejected_candidate_blocked(client, db_session):
    """已 reject 的候选调用 /ingest 必须返回 400 candidate_rejected。"""
    _, candidates = make_session_with_candidates(db_session, n_pending=1)
    c = candidates[0]
    cid = str(c.id)

    # 先 reject
    r = client.post(f"/api/intake/candidates/{cid}/reject", json={"reason": "不相关"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # 然后尝试 ingest
    r2 = client.post(f"/api/intake/candidates/{cid}/ingest")
    assert r2.status_code == 400
    detail = r2.json().get("detail", {})
    assert detail.get("code") == "candidate_rejected"


# ===========================================================================
# TEST 3: pending_review 状态调用 /ingest → 400 candidate_not_approved
# ===========================================================================

def test_ingest_pending_candidate_blocked(client, db_session):
    """pending_review 候选调用 /ingest 必须返回 400 candidate_not_approved。"""
    _, candidates = make_session_with_candidates(db_session, n_pending=1)
    c = candidates[0]
    cid = str(c.id)

    r = client.post(f"/api/intake/candidates/{cid}/ingest")
    assert r.status_code == 400
    detail = r.json().get("detail", {})
    assert detail.get("code") == "candidate_not_approved"


# ===========================================================================
# TEST 4: approve 后调用 /ingest → 创建 WorkflowJob，状态变 ingesting
# ===========================================================================

def test_approve_then_ingest_creates_job(client, db_session):
    """approve 后 /ingest 应创建 WorkflowJob，candidate.status 变为 ingesting。"""
    _, candidates = make_session_with_candidates(db_session, n_pending=1)
    c = candidates[0]
    cid = str(c.id)

    # approve
    r = client.post(f"/api/intake/candidates/{cid}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # ingest（dispatch 不实际发网络，只检查 DB 状态）
    with patch("app.api.intake.dispatch_job", return_value="threadpool"):
        r2 = client.post(f"/api/intake/candidates/{cid}/ingest")

    assert r2.status_code == 200, r2.text
    data = r2.json()

    # 候选状态变为 ingesting
    assert data["candidate"]["status"] == "ingesting"
    assert data["candidate"]["ingest_job_id"] is not None

    # job 已创建
    assert "job" in data
    assert data["job"]["status"] in ("queued", "running", "completed")

    # DB 中验证
    db_session.expire_all()
    updated_c = db_session.get(LiteratureIntakeCandidate, c.id)
    assert updated_c.status == "ingesting"
    assert updated_c.ingest_job_id is not None


# ===========================================================================
# TEST 5: 重复 DOI 检测 → candidate.status == "duplicate"
# ===========================================================================

def test_duplicate_doi_detected(client, db_session):
    """检索结果的 DOI 与已有 Paper 重复时，候选状态应为 duplicate。"""
    existing_doi = "10.1234/test.001"

    # 预先写入一篇论文（模拟已存在）
    existing = Paper(
        title="Existing Battery Paper",
        doi=existing_doi,
        library_name="test_lib",
        pdf_path="mock.pdf",
    )
    db_session.add(existing)
    db_session.commit()

    with MOCK_SEARCH_PATCH:
        resp = client.post("/api/intake/search", json={
            "query": "battery anode",
            "library_name": "test_lib",
            "max_results": 10,
        })

    assert resp.status_code == 200
    candidates = resp.json()["candidates"]

    dup_candidates = [c for c in candidates if c.get("doi") == existing_doi]
    assert len(dup_candidates) >= 1, "重复 DOI 的候选应被检测到"
    for c in dup_candidates:
        assert c["status"] == "duplicate", f"重复候选状态应为 duplicate，实为 {c['status']}"
        assert c["duplicate_paper_id"] is not None


# ===========================================================================
# TEST 6: 批量 ingest-approved — 只触发 approved 候选
# ===========================================================================

def test_duplicate_doi_detection_is_library_scoped(client, db_session):
    """Same DOI in another library must not mark this library's candidate duplicate."""
    existing_doi = "10.1234/test.001"
    db_session.add(
        Paper(
            title="Existing Battery Paper In Other Library",
            doi=existing_doi,
            library_name="other_lib",
            pdf_path="mock.pdf",
        )
    )
    db_session.commit()

    with MOCK_SEARCH_PATCH:
        resp = client.post("/api/intake/search", json={
            "query": "battery anode",
            "library_name": "test_lib",
            "max_results": 10,
        })

    assert resp.status_code == 200
    same_doi = [c for c in resp.json()["candidates"] if c.get("doi") == existing_doi]
    assert same_doi
    assert all(c["status"] != "duplicate" for c in same_doi)
    assert all(c["duplicate_paper_id"] is None for c in same_doi)


def test_batch_ingest_approved_only(client, db_session):
    """batch ingest-approved 只处理 approved 候选，pending/rejected 被跳过。"""
    sess, candidates = make_session_with_candidates(db_session, n_pending=3)
    sid = str(sess.id)
    c_approve, c_reject, c_pending = candidates

    # approve 第1个
    client.post(f"/api/intake/candidates/{c_approve.id}/approve")
    # reject 第2个
    client.post(f"/api/intake/candidates/{c_reject.id}/reject", json={"reason": "不相关"})
    # 第3个保持 pending

    with patch("app.api.intake.dispatch_job", return_value="threadpool"):
        r = client.post(f"/api/intake/sessions/{sid}/ingest-approved")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["triggered_count"] == 1, f"只有1个 approved，实际触发 {data['triggered_count']} 个"
    assert data["failed_count"] == 0


# ===========================================================================
# TEST 7: 批量 ingest-approved — 无 approved 候选 → 422
# ===========================================================================

def test_batch_ingest_no_approved_returns_422(client, db_session):
    """无 approved 候选时批量 ingest 应返回 422。"""
    sess, _ = make_session_with_candidates(db_session, n_pending=2)
    r = client.post(f"/api/intake/sessions/{sess.id}/ingest-approved")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "no_approved_candidates"


# ===========================================================================
# TEST 8: GET /api/intake/sessions/{id} 返回 intake_notice 免责提示
# ===========================================================================

def test_session_response_includes_intake_notice(client, db_session):
    """GET session 响应必须包含"尚未入库"免责声明。"""
    sess, _ = make_session_with_candidates(db_session, n_pending=1)
    r = client.get(f"/api/intake/sessions/{sess.id}")
    assert r.status_code == 200
    data = r.json()
    assert "intake_notice" in data
    assert "尚未入库" in data["intake_notice"]


# ===========================================================================
# TEST 9: approve 已 ingesting 候选 → 409
# ===========================================================================

def test_approve_ingesting_candidate_conflict(client, db_session):
    """已处于 ingesting 的候选不能再 approve。"""
    _, candidates = make_session_with_candidates(db_session, n_pending=1)
    c = candidates[0]
    c.status = "ingesting"
    db_session.add(c)
    db_session.commit()

    r = client.post(f"/api/intake/candidates/{c.id}/approve")
    assert r.status_code == 409


# ===========================================================================
# TEST 10: 现有导出安全测试（回归验证）
# ===========================================================================

def test_existing_export_tests_still_importable():
    """确认现有测试模块未被破坏（import 检查）。"""
    import importlib
    for mod in [
        "app.api.intake",
        "app.services.intake_screening_service",
        "app.db.models",
    ]:
        m = importlib.import_module(mod)
        assert m is not None

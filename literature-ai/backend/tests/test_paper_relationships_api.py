"""
Tests for POST /api/papers/{paper_id}/relationships
验证"添加关联文献"功能的后端接口是否正常工作。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.config import get_settings
from app.db.models import Base, Paper, PaperRelationship
from app.db.session import get_db_session


@pytest.fixture
def setup_test_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        db_path = tmp_root / "test_relationships.db"
        db_url = f"sqlite:///{db_path}"
        storage_root = tmp_root / "storage"

        monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
        monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage_root))
        get_settings.cache_clear()

        engine = create_engine(db_url, future=True)
        Base.metadata.create_all(engine)

        TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def override_get_db_session():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_session] = override_get_db_session

        yield engine

        app.dependency_overrides.clear()
        engine.dispose()

        from app.db.session import _engines, _session_factories
        for eng in list(_engines.values()):
            try:
                eng.dispose()
            except Exception:
                pass
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()


def test_create_relationship_success(setup_test_db):
    """正常流程：创建两篇文献并成功关联。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        source = Paper(title="主文献", pdf_path="main.pdf")
        target = Paper(title="补充材料 SI", pdf_path="si.pdf")
        session.add_all([source, target])
        session.commit()
        source_id = str(source.id)
        target_id = str(target.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{source_id}/relationships",
        json={
            "target_paper_id": target_id,
            "relationship_type": "supplementary",
            "note": "Manual frontend binding",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "created"
    assert "id" in data

    # 验证数据库里真的写进去了
    with Session() as session:
        rel = session.scalar(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == source.id,
                PaperRelationship.target_paper_id == target.id,
            )
        )
        assert rel is not None
        assert rel.relationship_type == "supplementary"
        assert rel.created_by == "user_manual"


def test_create_relationship_source_not_found(setup_test_db):
    """source paper 不存在时应返回 404。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        target = Paper(title="只有目标", pdf_path="target.pdf")
        session.add(target)
        session.commit()
        target_id = str(target.id)

    fake_source_id = str(uuid4())
    client = TestClient(app)
    response = client.post(
        f"/api/papers/{fake_source_id}/relationships",
        json={
            "target_paper_id": target_id,
            "relationship_type": "citation",
        },
    )
    assert response.status_code == 404


def test_create_relationship_target_not_found(setup_test_db):
    """target paper 不存在时应返回 404。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        source = Paper(title="只有来源", pdf_path="source.pdf")
        session.add(source)
        session.commit()
        source_id = str(source.id)

    fake_target_id = str(uuid4())
    client = TestClient(app)
    response = client.post(
        f"/api/papers/{source_id}/relationships",
        json={
            "target_paper_id": fake_target_id,
            "relationship_type": "citation",
        },
    )
    assert response.status_code == 404


def test_create_relationship_type_citation(setup_test_db):
    """支持 citation 类型关联。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        source = Paper(title="综述文章", pdf_path="review.pdf")
        target = Paper(title="被引原始文献", pdf_path="original.pdf")
        session.add_all([source, target])
        session.commit()
        source_id = str(source.id)
        target_id = str(target.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{source_id}/relationships",
        json={
            "target_paper_id": target_id,
            "relationship_type": "citation",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "created"

    with Session() as session:
        rel = session.scalar(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == source.id
            )
        )
        assert rel.relationship_type == "citation"


def test_create_relationship_without_note(setup_test_db):
    """note 字段是可选的，不传时应正常创建。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        source = Paper(title="A 文献", pdf_path="a.pdf")
        target = Paper(title="B 文献", pdf_path="b.pdf")
        session.add_all([source, target])
        session.commit()
        source_id = str(source.id)
        target_id = str(target.id)

    client = TestClient(app)
    response = client.post(
        f"/api/papers/{source_id}/relationships",
        json={
            "target_paper_id": target_id,
            "relationship_type": "supplementary",
            # 不传 note
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "created"

    with Session() as session:
        rel = session.scalar(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == source.id
            )
        )
        assert rel.note is None


def test_create_multiple_relationships(setup_test_db):
    """一篇主文献可以关联多篇其他文献。"""
    engine = setup_test_db
    Session = sessionmaker(bind=engine)

    with Session() as session:
        main = Paper(title="主论文", pdf_path="main.pdf")
        si1 = Paper(title="SI Part 1", pdf_path="si1.pdf")
        si2 = Paper(title="SI Part 2", pdf_path="si2.pdf")
        session.add_all([main, si1, si2])
        session.commit()
        main_id = str(main.id)
        si1_id = str(si1.id)
        si2_id = str(si2.id)

    client = TestClient(app)

    r1 = client.post(
        f"/api/papers/{main_id}/relationships",
        json={"target_paper_id": si1_id, "relationship_type": "supplementary"},
    )
    r2 = client.post(
        f"/api/papers/{main_id}/relationships",
        json={"target_paper_id": si2_id, "relationship_type": "supplementary"},
    )

    assert r1.status_code == 200
    assert r2.status_code == 200

    with Session() as session:
        rels = session.scalars(
            select(PaperRelationship).where(
                PaperRelationship.source_paper_id == main.id
            )
        ).all()
        assert len(rels) == 2
        target_ids = {str(r.target_paper_id) for r in rels}
        assert target_ids == {si1_id, si2_id}

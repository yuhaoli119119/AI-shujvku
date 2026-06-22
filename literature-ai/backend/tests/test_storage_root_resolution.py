from __future__ import annotations

import os

import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.config as config_module
from app.config import Settings, get_settings
from app.db.models import Base, Paper
from app.services.paper_workbench_service import PaperWorkbenchService


def _patch_project_roots(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "repo"
    backend_root = repo_root / "backend"
    backend_root.mkdir(parents=True)
    monkeypatch.setattr(config_module, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(config_module, "BACKEND_ROOT", backend_root)
    return repo_root, backend_root


def _load_settings(monkeypatch, storage_root_value: str, cwd: Path):
    monkeypatch.setenv("LITAI_STORAGE_ROOT", storage_root_value)
    monkeypatch.chdir(cwd)
    get_settings.cache_clear()
    try:
        return get_settings()
    finally:
        get_settings.cache_clear()


def test_storage_root_resolves_to_repo_data_from_repo_cwd(monkeypatch, tmp_path):
    repo_root, _backend_root = _patch_project_roots(monkeypatch, tmp_path)

    settings = _load_settings(monkeypatch, "./data/storage", repo_root)

    assert settings.storage_root == (repo_root / "data" / "storage").resolve()


def test_storage_root_resolves_to_repo_data_from_backend_cwd(monkeypatch, tmp_path):
    repo_root, backend_root = _patch_project_roots(monkeypatch, tmp_path)

    settings = _load_settings(monkeypatch, "./data/storage", backend_root)

    assert settings.storage_root == (repo_root / "data" / "storage").resolve()


def test_storage_root_resolves_to_repo_data_from_arbitrary_cwd(monkeypatch, tmp_path):
    repo_root, _backend_root = _patch_project_roots(monkeypatch, tmp_path)
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()

    settings = _load_settings(monkeypatch, "./data/storage", other_cwd)

    assert settings.storage_root == (repo_root / "data" / "storage").resolve()


def test_explicit_absolute_storage_root_is_preserved(monkeypatch, tmp_path):
    _repo_root, _backend_root = _patch_project_roots(monkeypatch, tmp_path)
    absolute_root = (tmp_path / "custom" / "storage").resolve()
    absolute_root.mkdir(parents=True)

    settings = _load_settings(monkeypatch, str(absolute_root), tmp_path)

    assert settings.storage_root == absolute_root


def test_container_style_storage_root_is_not_rewritten(monkeypatch, tmp_path):
    _repo_root, _backend_root = _patch_project_roots(monkeypatch, tmp_path)

    settings = _load_settings(monkeypatch, "/data/storage", tmp_path)

    assert str(settings.storage_root).replace("\\", "/") == "/data/storage"


def test_prepare_paper_workspace_uses_unified_storage_root(monkeypatch, tmp_path):
    repo_root, backend_root = _patch_project_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("LITAI_STORAGE_ROOT", "./data/storage")
    monkeypatch.setenv("LITAI_DATABASE_URL", os.environ["LITAI_TEST_DATABASE_URL"])
    monkeypatch.chdir(backend_root)
    get_settings.cache_clear()
    settings = get_settings()
    storage_root = settings.storage_root
    (storage_root / "pdf").mkdir(parents=True, exist_ok=True)
    pdf_path = storage_root / "pdf" / "paper.pdf"
    pdf_path.write_bytes(b"fake-pdf")

    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    paper_id = None
    try:
        with Session(engine) as session:
            paper = Paper(
                title="Workspace root target",
                pdf_path="storage/pdf/paper.pdf",
                pdf_quality_status="A_text_readable",
                pdf_quality_score=1.0,
                pdf_quality_report={
                    "quality_status": "A_text_readable",
                    "quality_score": 1.0,
                    "parse_allowed": False,
                    "needs_human_confirmation": False,
                    "metrics": {"path": str(pdf_path)},
                },
                workflow_status="Initial_Parsed",
            )
            session.add(paper)
            session.commit()
            paper_id = paper.id

        with Session(engine) as session:
            summary = PaperWorkbenchService(session, settings).prepare_paper_workspace(paper_id)

        expected_workspace = storage_root / "by_id" / str(paper_id)
        assert summary["workspace_abs_path"] == str(expected_workspace.resolve())
        assert (expected_workspace / "metadata.json").exists()
        assert not (repo_root / "backend" / "data" / "storage" / "by_id" / str(paper_id)).exists()
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_dual_storage_roots_emit_warning_with_active_root(monkeypatch, tmp_path, caplog):
    repo_root, backend_root = _patch_project_roots(monkeypatch, tmp_path)
    repo_storage = repo_root / "data" / "storage"
    backend_storage = backend_root / "data" / "storage"
    repo_storage.mkdir(parents=True)
    backend_storage.mkdir(parents=True)

    caplog.set_level(logging.WARNING, logger="app.config")
    settings = _load_settings(monkeypatch, "./data/storage", backend_root)

    assert settings.storage_root == repo_storage.resolve()
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(repo_storage.resolve()) in message for message in messages)
    assert any(str(backend_storage.resolve()) in message for message in messages)
    assert any(str(repo_storage.resolve()) in message and "using" in message for message in messages)


def test_settings_constructor_resolves_relative_storage_root_from_project_root(monkeypatch, tmp_path):
    repo_root, _backend_root = _patch_project_roots(monkeypatch, tmp_path)

    settings = Settings(storage_root=Path("./data/storage"))

    assert settings.storage_root == (repo_root / "data" / "storage").resolve()

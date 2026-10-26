from pathlib import Path
import pytest
from app.services.rebuild_task_authorization import review_evidence_root
from app.services.rebuild_workflow_service import RebuildWorkflowError
from app.utils import project_paths
pytestmark = pytest.mark.no_test_database

@pytest.mark.parametrize("relative", ["outputs/legacy", "data/reviewer-evidence/paper-1"])
def test_legacy_and_durable_namespaces(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(project_paths, "PROJECT_ROOT", tmp_path)
    root = tmp_path / relative
    root.mkdir(parents=True)
    assert review_evidence_root(root) == root

@pytest.mark.parametrize("relative", ["data", "data/reviewer-evidence", "data/reviewer-evidence-lookalike/paper", "other/paper"])
def test_outside_or_unscoped_namespace_refused(tmp_path, monkeypatch, relative):
    monkeypatch.setattr(project_paths, "PROJECT_ROOT", tmp_path)
    with pytest.raises(RebuildWorkflowError):
        review_evidence_root(tmp_path / relative)

@pytest.mark.parametrize("at_root", [False, True])
def test_symlink_escape_does_not_expand_namespace(tmp_path, monkeypatch, at_root):
    monkeypatch.setattr(project_paths, "PROJECT_ROOT", tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "data/reviewer-evidence"
    link = base if at_root else base / "paper"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RebuildWorkflowError):
        review_evidence_root(link / "child")

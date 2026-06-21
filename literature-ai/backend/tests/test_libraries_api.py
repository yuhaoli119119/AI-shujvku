from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import libraries as libraries_api
from app.main import app
from app.services.library_manager import LibraryManager


def test_create_library_permission_error_returns_403(monkeypatch):
    def fake_create_library(self, name: str, root_path: str = "", description: str = ""):
        raise PermissionError(13, "Permission denied", "/host/users")

    monkeypatch.setattr(LibraryManager, "create_library", fake_create_library)
    client = TestClient(app)

    response = client.post(
        "/api/libraries",
        json={"name": "test-lib", "root_path": "/host/users"},
    )

    assert response.status_code == 403
    assert "Permission denied" in response.json()["detail"]


def test_import_library_permission_error_returns_403(monkeypatch):
    def fake_import_library(self, root_path: str):
        raise PermissionError(13, "Permission denied", "/host/users/Default")

    monkeypatch.setattr(LibraryManager, "import_library", fake_import_library)
    client = TestClient(app)

    response = client.post(
        "/api/libraries/import",
        json={"root_path": "/host/users/Default"},
    )

    assert response.status_code == 403
    assert "Permission denied" in response.json()["detail"]


def test_activate_library_returns_structured_500_when_post_activate_sync_fails(monkeypatch):
    class _FakeLibrary:
        def __init__(self, name: str):
            self.name = name

        def model_dump(self):
            return {
                "name": self.name,
                "root_path": "/libraries/dual-atom",
                "description": self.name,
                "paper_count": 0,
                "is_active": True,
                "created_at": "2026-06-20T00:00:00",
            }

    class _FakeManager:
        def activate_library(self, name: str):
            return _FakeLibrary(name)

    def _raise_runtime_error():
        raise RuntimeError("mock db switch failed")

    monkeypatch.setattr(libraries_api, "_get_manager", lambda: _FakeManager())
    monkeypatch.setattr(libraries_api, "activate_active_library_database", _raise_runtime_error)
    client = TestClient(app)

    response = client.post("/api/libraries/双原子催化剂/activate")

    assert response.status_code == 500
    assert response.json()["detail"] == "激活文献库失败：mock db switch failed"


def test_activate_library_returns_structured_500_when_manager_hits_permission_error(monkeypatch):
    class _FakeManager:
        def activate_library(self, name: str):
            raise PermissionError(13, "Permission denied", f"/data/libraries/{name}/library.json")

    monkeypatch.setattr(libraries_api, "_get_manager", lambda: _FakeManager())
    client = TestClient(app)

    response = client.post("/api/libraries/双原子催化剂/activate")

    assert response.status_code == 500
    assert "激活文献库失败" in response.json()["detail"]
    assert "Permission denied" in response.json()["detail"]

from __future__ import annotations

from fastapi.testclient import TestClient

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

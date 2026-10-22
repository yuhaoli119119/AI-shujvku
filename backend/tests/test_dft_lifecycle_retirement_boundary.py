"""Additive retirement checks; these do not replace legacy lifecycle invariants."""

from importlib.util import find_spec

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.session import get_db_session
from app.main import app


pytestmark = pytest.mark.no_test_database


@pytest.fixture
def retirement_client(monkeypatch):
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "true")
    get_settings.cache_clear()

    def forbidden_database():
        raise AssertionError("Retired/denied requests must not acquire a database session")

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db_session] = forbidden_database
    try:
        # Do not enter lifespan: this test targets routing, not startup DB checks.
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        get_settings.cache_clear()


def test_registered_application_has_no_legacy_dft_or_mcp_entrypoints():
    legacy_modules = {
        "app.api.dft", "app.mcp.server", "app.mcp.tool_surface",
        "app.services.dft_audit_issue_lifecycle_service",
        "app.services.dft_audit_issue_service", "app.services.dft_audit_report_service",
    }
    for route in app.routes:
        assert getattr(getattr(route, "endpoint", None), "__module__", "") not in legacy_modules
        assert not getattr(route, "path", "").startswith(("/mcp", "/api/mcp"))
    for module in legacy_modules - {"app.api.dft"}:
        assert find_spec(module) is None, module


def test_registered_analysis_routes_are_readonly():
    routes = [route for route in app.routes if
              getattr(getattr(route, "endpoint", None), "__module__", "") == "app.api.analysis_readonly"]
    assert routes
    assert all(route.methods == {"GET"} for route in routes)


@pytest.mark.parametrize("path", ["audit-issues", "audit-report"])
def test_retired_issue_and_report_http_routes_are_unreachable(retirement_client, path):
    response = retirement_client.get(f"/api/dft/{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [
    "/api/papers/export/dft-dataset",
    "/api/dft/catalyst-dataset",
])
def test_analysis_successor_exposes_no_mutation_method(retirement_client, method, path):
    response = retirement_client.request(method, path, json={"status": "approved"})
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


@pytest.mark.parametrize("path,parameter", [
    ("/api/papers/compare", "status"),
    ("/api/papers/export/dft-dataset", "export_mode"),
    ("/api/papers/export/csv", "export_mode"),
    ("/api/dft/catalyst-dataset", "export_mode"),
    ("/api/dft/catalyst-dataset.csv", "export_mode"),
])
@pytest.mark.parametrize("mode", ["approved", "exportable"])
def test_approval_is_rejected_before_any_projection(retirement_client, monkeypatch, path, parameter, mode):
    # Dependency resolution obtains the session before the endpoint rejects mode.
    # A sentinel permits acquisition while prohibiting every actual DB operation.
    class ForbiddenSession:
        def __getattr__(self, name):
            raise AssertionError(f"Approval denial touched the database: {name}")

    monkeypatch.setitem(app.dependency_overrides, get_db_session, lambda: ForbiddenSession())
    response = retirement_client.get(path, params={parameter: mode})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "approval_not_evaluated"
    assert response.json()["detail"]["assessment_status"] == "not_evaluated"


@pytest.mark.parametrize("path", [
    "/api/papers/compare", "/api/papers/export/dft-dataset",
    "/api/papers/export/csv", "/api/dft/catalyst-dataset",
])
def test_legacy_source_is_gone_before_database_acquisition(retirement_client, path):
    response = retirement_client.get(path, params={"source": "legacy"})
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "legacy_extraction_retired"


def test_retirement_does_not_bypass_export_policy(retirement_client, monkeypatch):
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "retirement-synthetic-owner-only")
    get_settings.cache_clear()
    client = TestClient(app, client=("203.0.113.10", 50000))
    response = client.get("/api/papers/export/dft-dataset")
    assert response.status_code == 403
    assert response.json() == {"detail": "Exports are disabled by server policy"}

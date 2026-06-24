from __future__ import annotations

import os

import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.models import Base, Paper, PaperNote, ShareToken
from app.db.session import get_db_session
from app.main import app
from app.mcp.auth import authenticate_mcp_request, require_mcp_capability
from app.mcp.context import MCPAuthInfo, mcp_auth_context
from app.security.files import UnsafeLocalPDF, validate_local_ingest_pdf
from app.security.urls import UnsafeOutboundURL, get_public_url, validate_public_http_url


def _request(host: str, authorization: str = ""):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers={"Authorization": authorization} if authorization else {},
    )


@pytest.fixture
def share_client(tmp_path, monkeypatch):
    db_url = os.environ["LITAI_TEST_DATABASE_URL"]
    monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", " ")
    monkeypatch.setenv("LITAI_SHARE_MAX_PAGE_SIZE", "10")
    get_settings.cache_clear()
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    with factory() as session:
        paper = Paper(title="Shared paper", library_name="Shared Library", pdf_path="shared.pdf")
        other = Paper(title="Private paper", library_name="Other Library", pdf_path="private.pdf")
        session.add_all([paper, other])
        session.flush()
        session.add(ShareToken(token="read-only-token", scope="library:Shared Library"))
        session.add(ShareToken(token="invalid-scope-token", scope="unknown"))
        for index in range(15):
            session.add(PaperNote(paper_id=paper.id, source="test", content=f"note-{index}"))
        session.commit()
        paper_id = str(paper.id)
        other_id = str(other.id)
    yield TestClient(app, client=("192.168.1.40", 50000)), paper_id, other_id
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def export_clients(tmp_path, monkeypatch):
    db_url = os.environ["LITAI_TEST_DATABASE_URL"]
    monkeypatch.setenv("LITAI_DATABASE_URL", db_url)
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "owner-secret")
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    get_settings.cache_clear()
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_session
    yield {
        "local": TestClient(app),
        "remote": TestClient(app, client=("192.168.1.20", 50000)),
    }
    app.dependency_overrides.clear()
    engine.dispose()
    get_settings.cache_clear()


def test_asset_endpoint_rejects_absolute_traversal_and_symlink_escape(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    figure = storage / "figures" / "paper-1" / "safe.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"safe image")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("LITAI_STORAGE_ROOT", str(storage))
    get_settings.cache_clear()

    client = TestClient(app)
    assert client.get("/api/papers/assets/storage/figures/paper-1/safe.png").status_code == 200
    for attack in (
        "/api/papers/assets/%2Fetc%2Fhostname",
        "/api/papers/assets/..%2F..%2Frequirements.txt",
        "/api/papers/assets/%252e%252e%252frequirements.txt",
        "/api/papers/assets/C:%5CWindows%5Cwin.ini",
    ):
        assert client.get(attack).status_code in {400, 404}

    link = figure.parent / "escape.png"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windows may deny symlink creation without Developer Mode. Simulate
        # the only relevant filesystem behavior: resolve() escaping the root.
        from app.utils.artifact_paths import resolve_persisted_artifact_path

        link.write_bytes(b"placeholder")
        original_resolve = Path.resolve
        outside_resolved = original_resolve(outside)

        def escaped_resolve(self, *args, **kwargs):
            if str(self).lower() == str(link).lower():
                return outside_resolved
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped_resolve)
        assert resolve_persisted_artifact_path(
            "paper-1/escape.png",
            category="figures",
            settings=get_settings(),
        ) is None
    else:
        assert client.get("/api/papers/assets/paper-1/escape.png").status_code == 404
    get_settings.cache_clear()


def test_local_pdf_validation_enforces_root_regular_file_extension_and_magic(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    valid = allowed / "valid.pdf"
    valid.write_bytes(b"%PDF-1.4\n%%EOF")
    settings = Settings(storage_root=tmp_path / "storage", local_ingest_roots=str(allowed))
    assert validate_local_ingest_pdf(valid, settings) == valid.resolve()

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\n%%EOF")
    with pytest.raises(UnsafeLocalPDF, match="outside configured"):
        validate_local_ingest_pdf(outside, settings)

    fake = allowed / "fake.pdf"
    fake.write_bytes(b"not a PDF")
    with pytest.raises(UnsafeLocalPDF, match="signature"):
        validate_local_ingest_pdf(fake, settings)

    wrong_extension = allowed / "paper.txt"
    wrong_extension.write_bytes(b"%PDF-1.4\n%%EOF")
    with pytest.raises(UnsafeLocalPDF, match="extension"):
        validate_local_ingest_pdf(wrong_extension, settings)


def test_remote_owner_boundary_ignores_spoofable_headers_and_allows_token(monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "owner-secret")
    get_settings.cache_clear()
    remote = TestClient(app, client=("192.168.1.20", 50000))

    forged = remote.post(
        "/api/papers/ingest/path",
        headers={"Host": "localhost:8000", "Origin": "http://localhost:8000", "Referer": "http://localhost/"},
        json={"pdf_path": "C:/secret.pdf"},
    )
    assert forged.status_code == 403
    assert remote.get("/openapi.json").status_code == 403
    assert remote.get("/pages/ingestion/index.html").status_code == 403
    assert remote.get("/pages/share/index.html").status_code == 200
    assert remote.get("/openapi.json", headers={"X-LitAI-Owner-Token": "owner-secret"}).status_code == 200

    local = TestClient(app)
    assert local.get("/openapi.json").status_code == 200
    get_settings.cache_clear()


def test_http_mcp_requires_key_even_for_loopback_or_private_network(monkeypatch):
    monkeypatch.setenv("LITAI_MCP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("LITAI_MCP_API_KEYS", "reader|Reader|mcp-secret|read_papers")
    get_settings.cache_clear()

    for host in ("127.0.0.1", "192.168.1.30", "172.18.0.5"):
        with pytest.raises(HTTPException) as exc_info:
            authenticate_mcp_request(_request(host))
        assert exc_info.value.status_code == 401

    auth = authenticate_mcp_request(_request("192.168.1.30", "Bearer mcp-secret"))
    assert auth.capabilities == frozenset({"read_papers"})
    with mcp_auth_context(auth):
        assert require_mcp_capability("read_papers") is auth
    get_settings.cache_clear()


def test_exports_default_off_blocks_remote_non_owner_but_not_owner_http_access(monkeypatch, export_clients):
    remote = export_clients["remote"]
    local = export_clients["local"]
    response = remote.get("/api/papers/export/dft-dataset")
    assert response.status_code == 403
    assert response.json()["detail"] == "Exports are disabled by server policy"

    owner_response = remote.get(
        "/api/papers/export/dft-dataset",
        headers={"X-LitAI-Owner-Token": "owner-secret"},
    )
    assert owner_response.status_code == 200
    assert owner_response.json()["metadata"]["schema_version"] == "dft_results_ml_v2"

    local_response = local.get("/api/papers/export/dft-dataset")
    assert local_response.status_code == 200
    assert local_response.json()["metadata"]["schema_version"] == "dft_results_ml_v2"

    v3_path = "/api/dft/ml-dataset-v3?task=adsorption_energy"
    blocked_v3 = remote.get(v3_path)
    assert blocked_v3.status_code == 403
    assert blocked_v3.json()["detail"] == "Exports are disabled by server policy"
    owner_v3 = remote.get(v3_path, headers={"X-LitAI-Owner-Token": "owner-secret"})
    assert owner_v3.status_code == 200
    assert owner_v3.json()["manifest"]["schema_version"] == "dft_results_ml_v3"
    local_v3 = local.get(v3_path)
    assert local_v3.status_code == 200
    assert local_v3.json()["manifest"]["schema_version"] == "dft_results_ml_v3"

    for export_path in (
        "/api/dft/ml-dataset-v3.csv?task=adsorption_energy",
        "/api/dft/ml-dataset-v3/manifest?task=adsorption_energy",
        "/api/dft/project-library-ml-export?task=adsorption_energy",
        "/api/dft/project-library-ml-export.csv?task=adsorption_energy",
    ):
        blocked = remote.get(export_path)
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Exports are disabled by server policy"
        owner = remote.get(export_path, headers={"X-LitAI-Owner-Token": "owner-secret"})
        assert owner.status_code == 200
        local_export = local.get(export_path)
        assert local_export.status_code == 200


def test_exports_default_off_still_disables_mcp_export_capabilities(monkeypatch):
    monkeypatch.setenv("LITAI_EXPORTS_ENABLED", "false")
    get_settings.cache_clear()
    reader = MCPAuthInfo("reader", "Reader", frozenset({"read_papers"}), "key")
    with mcp_auth_context(reader):
        with pytest.raises(PermissionError, match="export_data"):
            require_mcp_capability("export_data")
        with pytest.raises(PermissionError, match="create_share_links"):
            require_mcp_capability("create_share_links")
    get_settings.cache_clear()


def test_share_api_is_remote_read_only_and_page_size_is_hard_capped(share_client):
    client, paper_id, other_id = share_client
    papers = client.get("/api/share/read-only-token/papers?limit=100")
    assert papers.status_code == 200
    assert papers.json()["limit"] == 10
    assert [item["id"] for item in papers.json()["items"]] == [paper_id]

    notes = client.get(f"/api/share/read-only-token/notes/{paper_id}?limit=100")
    assert notes.status_code == 200
    assert len(notes.json()["items"]) == 10
    assert client.get(f"/api/share/read-only-token/papers/{paper_id}").status_code == 200
    assert client.get(f"/api/share/read-only-token/papers/{other_id}").status_code == 403
    assert client.get("/api/share/invalid-scope-token/papers").status_code == 403
    assert client.post("/api/share/read-only-token/papers").status_code == 405
    assert client.get("/api/papers/").status_code == 401


def test_ssrf_guard_blocks_local_dns_and_revalidates_redirects(monkeypatch):
    with pytest.raises(UnsafeOutboundURL):
        validate_public_http_url("file:///etc/passwd")
    with pytest.raises(UnsafeOutboundURL):
        validate_public_http_url("http://127.0.0.1/admin")
    with pytest.raises(UnsafeOutboundURL):
        validate_public_http_url("http://169.254.169.254/latest/meta-data")

    def public_dns(host, port, *args, **kwargs):
        del port, args, kwargs
        if host == "public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://localhost/internal"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeOutboundURL):
            get_public_url(client, "https://public.example/paper.pdf")
    assert calls == ["https://public.example/paper.pdf"]

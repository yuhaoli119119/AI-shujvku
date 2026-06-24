from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.settings import _advertised_base_url, _enforce_settings_write_access, _is_local_request_host, _mcp_runner_command
from app.config import get_settings
from app.main import app


def _make_request(host: str, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=host),
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("172.17.0.1", False),
        ("192.168.1.10", False),
        ("10.0.0.8", False),
        ("169.254.0.2", False),
        ("8.8.8.8", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_local_request_host(host: str, expected: bool):
    assert _is_local_request_host(host) is expected


def test_settings_write_rejects_private_docker_bridge_when_no_token(monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", " ")
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("172.17.0.1"))

    assert exc_info.value.status_code == 401
    assert "Owner authentication" in str(exc_info.value.detail)


def test_settings_write_rejects_spoofed_localhost_target_when_no_token(monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", " ")
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("172.17.0.1", {"host": "localhost:8000"}))

    assert exc_info.value.status_code == 401


def test_ide_prompt_base_url_prefers_request_host_over_docker_ip():
    assert _advertised_base_url(_make_request("172.18.0.7", {"host": "localhost:8000"}), fallback_host="172.18.0.7") == "http://localhost:8000"


def test_ide_prompt_base_url_rewrites_container_bridge_host_to_localhost(monkeypatch):
    monkeypatch.delenv("LITAI_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LITAI_MCP_PUBLIC_BASE_URL", raising=False)

    assert _advertised_base_url(_make_request("172.18.0.7", {"host": "172.18.0.7:8000"}), fallback_host="172.18.0.7") == "http://localhost:8000"


def test_ide_prompt_base_url_can_be_overridden_for_lan_or_tunnel(monkeypatch):
    monkeypatch.setenv("LITAI_PUBLIC_BASE_URL", "http://192.168.1.50:8000/mcp/")

    assert _advertised_base_url(_make_request("172.18.0.7", {"host": "172.18.0.7:8000"}), fallback_host="172.18.0.7") == "http://192.168.1.50:8000"


def test_mcp_runner_uses_npx_cmd_for_windows_clients():
    request = _make_request(
        "127.0.0.1",
        {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )

    assert _mcp_runner_command(request) == "npx.cmd"


def test_settings_write_rejects_public_host_when_no_token(monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", " ")
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("8.8.8.8"))

    assert exc_info.value.status_code == 401
    assert "Owner authentication" in str(exc_info.value.detail)


def test_settings_write_requires_matching_token_when_configured(monkeypatch):
    monkeypatch.setenv("LITAI_OWNER_API_TOKEN", "secret-token")
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("172.17.0.1", {"X-Settings-Token": "wrong-token"}))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid Owner token"

    with pytest.raises(HTTPException) as local_exc_info:
        _enforce_settings_write_access(_make_request("172.17.0.1", {"host": "localhost:8000"}))

    assert local_exc_info.value.status_code == 403
    assert local_exc_info.value.detail == "Invalid Owner token"

    _enforce_settings_write_access(_make_request("8.8.8.8", {"X-Settings-Token": "secret-token"}))


def test_ide_prompts_never_return_real_mcp_key(monkeypatch):
    import asyncio

    from app.api import settings as settings_api

    monkeypatch.setenv("LITAI_MCP_API_KEYS", "admin|Admin|litmcp_real_secret|read_papers")
    get_settings.cache_clear()
    monkeypatch.setattr(settings_api, "_read_persisted_settings", lambda: {})

    payload = asyncio.run(settings_api.get_ide_prompts(_make_request("127.0.0.1", {"host": "localhost:8000"})))

    assert payload["sample_key"] == "litmcp_your_key"
    assert "litmcp_real_secret" not in payload["cursor_config_json"]
    assert "litmcp_real_secret" not in payload["suggested_prompt"]
    get_settings.cache_clear()


def test_ide_prompts_always_require_http_mcp_key(monkeypatch):
    import asyncio

    from app.api import settings as settings_api

    monkeypatch.setenv("LITAI_MCP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("LITAI_MCP_API_KEYS", "")
    get_settings.cache_clear()
    monkeypatch.setattr(settings_api, "_read_persisted_settings", lambda: {})

    payload = asyncio.run(settings_api.get_ide_prompts(_make_request("127.0.0.1", {"host": "localhost:8000"})))

    assert payload["auth_required"] is True
    assert payload["mcp_url"].endswith("/mcp")
    assert payload["cursor_config"]["mcpServers"]["literature-ai"]["command"] in {"npx", "npx.cmd"}
    assert "--header" in payload["cursor_config"]["mcpServers"]["literature-ai"]["args"]
    assert payload["prompt_schema_version"] == "ide_review_prompt_v7"
    assert "SRR_LiS" in payload["prompt_contract"]["reaction_profile_templates"]
    assert "li_s_sac_dac" in payload["prompt_contract"]["project_library_contexts"]
    assert "li_s_sac_dac" in payload["prompt_contract"]["topic_field_dictionaries"]
    assert "li_s_sac_dac" in payload["prompt_contract"]["project_library_prompt_templates"]
    assert "app.mcp.context.mcp_auth_context" in payload["suggested_prompt"]
    assert "禁止直接导入 service/session/model" in payload["suggested_prompt"]
    assert "后写入的 AI 结果允许覆盖先前 AI 结果" in payload["suggested_prompt"]
    get_settings.cache_clear()


def test_services_status_reports_web_ai_disabled(setup_test_db, monkeypatch):
    monkeypatch.setenv("LITAI_WRITER_API_BASE", "https://writer.example.test")
    monkeypatch.setenv("LITAI_WRITER_API_KEY", "secret")
    monkeypatch.setenv("LITAI_WRITER_MODEL", "legacy-model")
    get_settings.cache_clear()

    client = TestClient(app)
    payload = client.get("/api/settings/status").json()

    assert payload["writer"]["configured"] is False
    assert payload["writer"]["disabled"] is True
    assert payload["writer"]["backend"] == "disabled"
    assert payload["writer"]["has_api_key"] is False
    assert payload["internal_parser"]["disabled"] is True
    assert payload["internal_parser"]["uses"] == "ide_mcp_ai"
    get_settings.cache_clear()


def test_writer_settings_keys_are_cleanup_only(setup_test_db):
    client = TestClient(app)

    response = client.post(
        "/api/settings",
        json={
            "settings": [
                {"key": "writer_api_key", "value": "should-not-persist"},
                {"key": "writer_api_base", "value": "https://writer.example.test"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 2
    payload = client.get("/api/settings").json()
    assert "writer_api_key" not in payload
    assert "writer_api_base" not in payload


def test_settings_api_hides_residual_writer_keys_from_persisted_db(setup_test_db):
    engine = setup_test_db
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_settings ("
                "  key   VARCHAR(255) PRIMARY KEY,"
                "  value TEXT"
                ")"
            )
        )
        connection.execute(
            text("INSERT INTO app_settings (key, value) VALUES (:key, :value)"),
            [
                {"key": "writer_api_key", "value": "persisted-secret-key"},
                {"key": "writer_api_base", "value": "https://writer.example.test"},
                {"key": "writer_model", "value": "persisted-model"},
            ],
        )

    client = TestClient(app)
    payload = client.get("/api/settings").json()

    assert "writer_api_key" not in payload
    assert "writer_api_base" not in payload
    assert "writer_model" not in payload

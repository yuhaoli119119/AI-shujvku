from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.settings import _enforce_settings_write_access, _is_local_request_host
from app.config import get_settings


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
        ("172.17.0.1", True),
        ("192.168.1.10", True),
        ("10.0.0.8", True),
        ("169.254.0.2", True),
        ("8.8.8.8", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_local_request_host(host: str, expected: bool):
    assert _is_local_request_host(host) is expected


def test_settings_write_allows_private_docker_bridge_when_no_token(monkeypatch):
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    _enforce_settings_write_access(_make_request("172.17.0.1"))


def test_settings_write_rejects_public_host_when_no_token(monkeypatch):
    monkeypatch.delenv("LITAI_SETTINGS_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("8.8.8.8"))

    assert exc_info.value.status_code == 403
    assert "local requests" in str(exc_info.value.detail)


def test_settings_write_requires_matching_token_when_configured(monkeypatch):
    monkeypatch.setenv("LITAI_SETTINGS_ADMIN_TOKEN", "secret-token")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        _enforce_settings_write_access(_make_request("172.17.0.1", {"X-Settings-Token": "wrong-token"}))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Invalid settings admin token"

    _enforce_settings_write_access(_make_request("8.8.8.8", {"X-Settings-Token": "secret-token"}))

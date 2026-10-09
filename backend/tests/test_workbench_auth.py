"""Unit + API tests for the workbench web login (``/api/auth/*``)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.api.auth as auth_api
from app.config import get_settings
from app.main import app
from app.security import session_auth
from app.security.htpasswd import (
    HtpasswdStore,
    hash_password,
    hash_scheme,
    parse_htpasswd,
    verify_password,
    write_htpasswd,
)
from app.security.rate_limit import FixedWindowRateLimiter
from app.security.session_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    client_ip,
    create_session,
    csrf_token_valid,
    origin_allowed,
    parse_session_token,
)

pytestmark = pytest.mark.no_test_database

ALICE_PASSWORD = "correct-horse-battery-staple"
BOB_PASSWORD = "another-long-password-42"


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Isolated htpasswd store, session secret, no Redis (in-process fallbacks)."""
    htpasswd_path = tmp_path / "owner.htpasswd"
    write_htpasswd(
        htpasswd_path,
        {
            "alice": hash_password(ALICE_PASSWORD, "aliceSlt"),
            "bob": hash_password(BOB_PASSWORD, "bobslt01"),
        },
    )
    monkeypatch.setenv("LITAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("LITAI_AUTH_HTPASSWD_FILE", str(htpasswd_path))
    monkeypatch.setenv("LITAI_AUTH_SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("LITAI_AUTH_REDIS_URL", "")
    monkeypatch.setenv("LITAI_AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("LITAI_AUTH_LOGIN_MAX_FAILURES_PER_IP_USER", "3")
    monkeypatch.setenv("LITAI_AUTH_LOGIN_MAX_FAILURES_PER_IP", "50")
    get_settings.cache_clear()
    session_auth._stores.clear()
    session_auth._process_revoked.clear()
    auth_api._limiter._memory.clear()
    yield htpasswd_path
    get_settings.cache_clear()
    session_auth._stores.clear()
    session_auth._process_revoked.clear()
    auth_api._limiter._memory.clear()


# ---------------------------------------------------------------------------
# htpasswd store
# ---------------------------------------------------------------------------

def test_apr1_matches_openssl_reference_vector():
    # ``openssl passwd -apr1 -salt abcdefgh hunter2`` on the deployment host.
    assert hash_password("hunter2", "abcdefgh") == "$apr1$abcdefgh$ckT15POyCRlen.h6XtGAZ1"


def test_apr1_hash_roundtrip_and_rejection():
    hashed = hash_password(ALICE_PASSWORD)
    assert hash_scheme(hashed) == "apr1"
    assert verify_password(hashed, ALICE_PASSWORD) is True
    assert verify_password(hashed, ALICE_PASSWORD + "x") is False
    assert verify_password(hashed, "") is False


def test_legacy_and_unsupported_hash_fields_are_refused():
    assert hash_scheme("{SHA}W6ph5Mm5Pz8GgiULbPgzG37mj9g=") == "sha1"
    assert hash_scheme("$2y$05$abcdefghijklmnopqrstuv") == "bcrypt"
    assert verify_password("$2y$05$abcdefghijklmnopqrstuv", "whatever") is False
    assert verify_password("plain-text-password", "plain-text-password") is False
    assert verify_password("{SHA}W6ph5Mm5Pz8GgiULbPgzG37mj9g=", "password") is True


def test_write_and_parse_roundtrip_preserves_order():
    text = "# comment\ncarol:$apr1$aaaaaaa1$0123456789012345678901\n\ndave:$apr1$bbbbbbb2$0123456789012345678901\n"
    users = parse_htpasswd(text)
    assert list(users) == ["carol", "dave"]


def test_store_reloads_when_file_changes(tmp_path):
    path = tmp_path / "store.htpasswd"
    write_htpasswd(path, {"eve": hash_password("first-password-1", "eveSalt1")})
    store = HtpasswdStore(path)
    assert store.verify("eve", "first-password-1") is True
    write_htpasswd(path, {"eve": hash_password("second-password-2", "eveSalt2")})
    store.reload(force=True)
    assert store.verify("eve", "second-password-2") is True
    assert store.verify("eve", "first-password-1") is False


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

def test_session_token_roundtrip(auth_env):
    issued = create_session("alice", remember=False)
    state = parse_session_token(issued.token)
    assert state is not None
    assert state.username == "alice"
    assert state.remember is False
    assert state.expires_at > int(time.time())
    assert state.csrf == issued.csrf
    assert issued.max_age is None  # browser-session cookie when "remember" is off


def test_remember_me_extends_ttl_and_sets_max_age(auth_env):
    issued = create_session("alice", remember=True)
    assert issued.max_age == 30 * 24 * 3600
    assert issued.state.ttl == 30 * 24 * 3600


def test_session_rejects_tampering_and_expiry(auth_env):
    issued = create_session("alice")
    head, body, signature = issued.token.split(".")
    assert parse_session_token(f"{head}.{body}.{signature[:-2]}xx") is None
    assert parse_session_token(issued.token[:-1] + "a") is None
    far_future = issued.state.expires_at + 10
    assert parse_session_token(issued.token, now=far_future) is None


def test_password_change_invalidates_sessions(auth_env, tmp_path):
    issued = create_session("alice")
    assert parse_session_token(issued.token) is not None
    write_htpasswd(
        auth_env,
        {
            "alice": hash_password("rotated-password-99", "aliceSlt"),
            "bob": hash_password(BOB_PASSWORD, "bobslt01"),
        },
    )
    session_auth.htpasswd_store().reload(force=True)
    assert parse_session_token(issued.token) is None


def test_session_unknown_user_is_rejected(auth_env):
    issued = create_session("alice")
    write_htpasswd(auth_env, {"bob": hash_password(BOB_PASSWORD, "bobslt01")})
    session_auth.htpasswd_store().reload(force=True)
    assert parse_session_token(issued.token) is None


def test_csrf_double_submit_requires_all_three_values(auth_env):
    issued = create_session("alice")
    state = parse_session_token(issued.token)

    class _Request:
        def __init__(self, headers, cookies):
            self.headers = headers
            self.cookies = cookies

    good = _Request({CSRF_HEADER: issued.csrf}, {CSRF_COOKIE: issued.csrf})
    assert csrf_token_valid(good, state) is True
    assert csrf_token_valid(_Request({}, {CSRF_COOKIE: issued.csrf}), state) is False
    assert csrf_token_valid(_Request({CSRF_HEADER: issued.csrf}, {}), state) is False
    assert csrf_token_valid(
        _Request({CSRF_HEADER: "attacker"}, {CSRF_COOKIE: "attacker"}), state
    ) is False


def test_origin_guard(auth_env):
    class _Request:
        def __init__(self, origin, host="dft.researchlife.top"):
            self.headers = {"origin": origin, "host": host} if origin else {"host": host}

    assert origin_allowed(_Request("")) is True
    assert origin_allowed(_Request("https://dft.researchlife.top")) is True
    assert origin_allowed(_Request("https://evil.example")) is False
    assert origin_allowed(_Request("null")) is False


def test_client_ip_prefers_cloudflare_header(auth_env):
    class _Request:
        def __init__(self, headers, host="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert client_ip(_Request({"cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    assert client_ip(_Request({"x-forwarded-for": "198.51.100.7, 10.1.1.1"})) == "198.51.100.7"
    assert client_ip(_Request({})) == "10.0.0.1"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_blocks_after_limit(auth_env):
    limiter = FixedWindowRateLimiter("")
    decisions = [await limiter.hit("k", 3, 60) for _ in range(4)]
    assert [item.allowed for item in decisions] == [True, True, True, False]
    assert decisions[-1].retry_after >= 1
    await limiter.reset("k")
    assert (await limiter.hit("k", 3, 60)).allowed is True


@pytest.mark.asyncio
async def test_rate_limiter_peek_does_not_consume(auth_env):
    limiter = FixedWindowRateLimiter("")
    for _ in range(3):
        await limiter.hit("peek", 3, 60)
    assert (await limiter.peek("peek", 3, 60)).allowed is False
    assert (await limiter.peek("peek", 3, 60)).allowed is False


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

def test_login_me_logout_flow(auth_env):
    client = TestClient(app)
    anonymous_me = client.get("/api/auth/me")
    assert anonymous_me.status_code == 401
    assert client.get("/api/auth/verify").status_code == 401

    wrong = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "invalid_credentials"

    unknown = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert unknown.status_code == 401
    # Same message for unknown account and wrong password.
    assert unknown.json() == wrong.json()

    ok = client.post("/api/auth/login", json={"username": "alice", "password": ALICE_PASSWORD})
    assert ok.status_code == 200
    body = ok.json()
    assert body["username"] == "alice"
    assert body["csrf_token"]
    assert client.cookies.get(SESSION_COOKIE)

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"

    verify = client.get("/api/auth/verify")
    assert verify.status_code == 200
    assert verify.headers["X-LitAI-User"] == "alice"

    # State-changing request without the CSRF header is refused.
    blocked = client.post("/api/auth/logout")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "csrf_token_invalid"

    logged_out = client.post(
        "/api/auth/logout", headers={CSRF_HEADER: body["csrf_token"]}
    )
    assert logged_out.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/verify").status_code == 401


def test_cross_origin_login_is_blocked(auth_env):
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": ALICE_PASSWORD},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_origin_request_blocked"


def test_login_rate_limit_returns_429_with_retry_after(auth_env):
    client = TestClient(app)
    statuses = [
        client.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code
        for _ in range(4)
    ]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429
    limited = client.post("/api/auth/login", json={"username": "alice", "password": ALICE_PASSWORD})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_session_cookie_flags_are_httponly_and_secure(monkeypatch, auth_env):
    monkeypatch.setenv("LITAI_AUTH_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        "/api/auth/login", json={"username": "bob", "password": BOB_PASSWORD}
    )
    assert response.status_code == 200
    raw = "; ".join(value for key, value in response.headers.multi_items() if key.lower() == "set-cookie")
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=lax" in raw.replace("samesite", "SameSite")


def test_login_is_refused_without_configured_secret(monkeypatch, auth_env):
    monkeypatch.setenv("LITAI_AUTH_SESSION_SECRET", "")
    monkeypatch.setenv("LITAI_OAUTH_JWT_SECRET", "")
    get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": ALICE_PASSWORD}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "auth_not_configured"

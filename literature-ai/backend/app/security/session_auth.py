"""Workbench web session authentication: HttpOnly cookie + CSRF double submit.

Why a cookie and not a ``Bearer`` token
--------------------------------------
The owner gateway now validates *page* requests (``/pages/literature_library/…``)
through an nginx ``auth_request`` subrequest.  Only a credential the browser
attaches on its own can be checked on a top level navigation, and a Bearer token
kept in ``localStorage`` is never sent with a navigation, so it cannot gate a
page.  A cookie is therefore the only workable transport here, and it has the
pleasant side effect that the existing workbench front end keeps working
unchanged: same-origin ``fetch``/``XHR`` send the cookie automatically.

Because browsers attach cookies automatically, the following protections are
mandatory and are implemented here:

* the session cookie is ``HttpOnly`` (XSS cannot read it) and ``SameSite=Lax``;
* every state changing ``/api`` request authenticated by that cookie must also
  present the CSRF token issued at login (``litai_csrf`` cookie *and*
  ``X-CSRF-Token`` header -- double submit, no server side storage needed);
* cross-origin state changing ``/api`` requests are rejected outright
  (``Origin`` check), which also blocks login CSRF on ``/api/auth/login``;
* the token is an HMAC-SHA256 signed, self describing payload (same idea as the
  OAuth refresh token in ``app/oauth.py``) so a backend restart does not log
  people out, plus a small revocation list for explicit logout.

Sessions are bound to the current htpasswd hash (``pv`` claim), so changing or
removing a password invalidates that user's outstanding sessions immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.security.htpasswd import HtpasswdStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "litai_session"
CSRF_COOKIE = "litai_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_ISSUER = "litai-workbench"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_EXEMPT_PATHS = ("/api/auth/login",)
BOUNDARY_EXEMPT_PREFIXES = ("/mcp", "/oauth/", "/.well-known/", "/api/share/")
REVOCATION_NAMESPACE = "litai:auth:revoked"

_stores: dict[str, HtpasswdStore] = {}
_process_revoked: set[str] = set()
_revocation_client: Any = None
_revocation_retry_after = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def session_secret(settings: Settings | None = None) -> str:
    runtime = settings or get_settings()
    return (runtime.auth_session_secret or runtime.oauth_jwt_secret or "").strip()


def sessions_available(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not bool(getattr(settings, "auth_enabled", True)):
        return False
    if not session_secret(settings):
        return False
    return bool(str(getattr(settings, "auth_htpasswd_file", "") or "").strip())


def htpasswd_store(settings: Settings | None = None) -> HtpasswdStore:
    runtime = settings or get_settings()
    path = str(runtime.auth_htpasswd_file or "").strip()
    store = _stores.get(path)
    if store is None:
        store = HtpasswdStore(path)
        _stores[path] = store
    return store


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64url(
        hmac.new(secret.encode("utf-8"), f"{head}.{body}".encode("utf-8"), hashlib.sha256).digest()
    )
    return f"{head}.{body}.{signature}"


def _unsign(token: str, secret: str) -> dict[str, Any] | None:
    try:
        head, body, signature = token.split(".")
        expected = _b64url(
            hmac.new(secret.encode("utf-8"), f"{head}.{body}".encode("utf-8"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def password_fingerprint(username: str, hash_field: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"pv:{username}:{hash_field}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def client_ip(request: Request) -> str:
    """Best effort client IP behind cloudflared -> nginx -> backend."""
    forwarded = request.headers.get("cf-connecting-ip", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    chain = request.headers.get("x-forwarded-for", "")
    if chain:
        first = chain.split(",")[0].strip()
        if first:
            return first
    return ((request.client.host if request.client else "") or "unknown").strip() or "unknown"


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionState:
    username: str
    sid: str
    csrf: str
    remember: bool
    issued_at: int
    expires_at: int
    ttl: int

    @property
    def renew_after(self) -> float:
        return self.issued_at + (self.ttl / 2.0)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf: str
    state: SessionState
    max_age: int | None


def session_ttl_seconds(remember: bool, settings: Settings | None = None) -> int:
    runtime = settings or get_settings()
    if remember:
        days = max(1, int(getattr(runtime, "auth_session_remember_days", 30) or 30))
        return days * 24 * 3600
    hours = max(1, int(getattr(runtime, "auth_session_ttl_hours", 12) or 12))
    return hours * 3600


def create_session(
    username: str,
    *,
    remember: bool = False,
    settings: Settings | None = None,
    now: int | None = None,
) -> IssuedSession:
    runtime = settings or get_settings()
    secret = session_secret(runtime)
    if not secret:
        raise HTTPException(status_code=503, detail="auth_not_configured")
    store = htpasswd_store(runtime)
    hash_field = store.hash_for(username)
    if not hash_field:
        raise HTTPException(status_code=401, detail="invalid_credentials")
    issued_at = int(now if now is not None else time.time())
    ttl = session_ttl_seconds(remember, runtime)
    csrf = secrets.token_urlsafe(32)
    sid = secrets.token_urlsafe(16)
    payload = {
        "iss": SESSION_ISSUER,
        "sub": username,
        "sid": sid,
        "csrf": csrf,
        "pv": password_fingerprint(username, hash_field, secret),
        "rem": bool(remember),
        "iat": issued_at,
        "exp": issued_at + ttl,
    }
    state = SessionState(
        username=username,
        sid=sid,
        csrf=csrf,
        remember=bool(remember),
        issued_at=issued_at,
        expires_at=issued_at + ttl,
        ttl=ttl,
    )
    return IssuedSession(
        token=_sign(payload, secret),
        csrf=csrf,
        state=state,
        max_age=ttl if remember else None,
    )


def parse_session_token(
    token: str,
    *,
    settings: Settings | None = None,
    now: int | None = None,
) -> SessionState | None:
    """Validate signature, expiry, issuer and the bound htpasswd hash."""
    runtime = settings or get_settings()
    secret = session_secret(runtime)
    raw = str(token or "").strip()
    if not secret or not raw:
        return None
    payload = _unsign(raw, secret)
    if payload is None:
        return None
    if payload.get("iss") != SESSION_ISSUER:
        return None
    username = str(payload.get("sub") or "").strip()
    sid = str(payload.get("sid") or "").strip()
    csrf = str(payload.get("csrf") or "").strip()
    if not username or not sid or not csrf:
        return None
    current = int(now if now is not None else time.time())
    try:
        expires_at = int(payload.get("exp") or 0)
        issued_at = int(payload.get("iat") or 0)
    except (TypeError, ValueError):
        return None
    if expires_at <= current:
        return None
    remember = bool(payload.get("rem"))
    hash_field = htpasswd_store(runtime).hash_for(username)
    if not hash_field:
        return None
    if not hmac.compare_digest(
        str(payload.get("pv") or ""),
        password_fingerprint(username, hash_field, secret),
    ):
        return None
    ttl = max(1, expires_at - issued_at) if issued_at else session_ttl_seconds(remember, runtime)
    return SessionState(
        username=username,
        sid=sid,
        csrf=csrf,
        remember=remember,
        issued_at=issued_at or (expires_at - ttl),
        expires_at=expires_at,
        ttl=ttl,
    )


def needs_renewal(state: SessionState, *, now: int | None = None) -> bool:
    return (now if now is not None else int(time.time())) >= state.renew_after


# ---------------------------------------------------------------------------
# Revocation (explicit logout)
# ---------------------------------------------------------------------------

def _revoked_key(sid: str) -> str:
    return f"{REVOCATION_NAMESPACE}:{sid}"


async def _revocation_redis():
    global _revocation_client, _revocation_retry_after
    if not str(get_settings().auth_redis_url or "").strip():
        return None
    if time.monotonic() < _revocation_retry_after:
        return None
    if _revocation_client is not None:
        return _revocation_client
    try:
        from redis.asyncio import from_url as redis_from_url

        client = redis_from_url(
            get_settings().auth_redis_url,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            decode_responses=True,
        )
        await client.ping()
    except Exception:
        _revocation_retry_after = time.monotonic() + 30.0
        logger.warning("Auth revocation store (Redis) unavailable; using in-process list only")
        return None
    _revocation_client = client
    return client


async def revoke_session(state: SessionState) -> None:
    _process_revoked.add(state.sid)
    client = await _revocation_redis()
    if client is None:
        return
    try:
        ttl = max(60, state.expires_at - int(time.time()))
        await client.set(_revoked_key(state.sid), "1", ex=ttl)
    except Exception:
        global _revocation_client, _revocation_retry_after
        _revocation_client = None
        _revocation_retry_after = time.monotonic() + 30.0
        logger.warning("Auth revocation write failed; in-process list still revoked the session")


async def is_revoked(sid: str) -> bool:
    if not sid:
        return False
    if sid in _process_revoked:
        return True
    client = await _revocation_redis()
    if client is None:
        return False
    try:
        return bool(await client.exists(_revoked_key(sid)))
    except Exception:
        global _revocation_client, _revocation_retry_after
        _revocation_client = None
        _revocation_retry_after = time.monotonic() + 30.0
        return False


async def load_session(request: Request, *, settings: Settings | None = None) -> SessionState | None:
    raw = str((request.cookies or {}).get(SESSION_COOKIE) or "").strip()
    if not raw:
        return None
    state = parse_session_token(raw, settings=settings)
    if state is None:
        return None
    if await is_revoked(state.sid):
        return None
    return state


async def require_session(request: Request, *, settings: Settings | None = None) -> SessionState:
    state = await load_session(request, settings=settings)
    if state is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return state


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

def _cookie_flags(settings: Settings) -> dict[str, Any]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": bool(getattr(settings, "auth_cookie_secure", True)),
        "path": "/",
    }


def apply_session_cookies(response, issued: IssuedSession, settings: Settings | None = None):
    runtime = settings or get_settings()
    flags = _cookie_flags(runtime)
    response.set_cookie(SESSION_COOKIE, issued.token, max_age=issued.max_age, **flags)
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf,
        max_age=issued.max_age,
        httponly=False,
        samesite="lax",
        secure=flags["secure"],
        path="/",
    )
    return response


def clear_session_cookies(response, settings: Settings | None = None):
    runtime = settings or get_settings()
    flags = _cookie_flags(runtime)
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax", secure=flags["secure"])
    response.delete_cookie(
        CSRF_COOKIE, path="/", samesite="lax", secure=flags["secure"], httponly=False
    )
    return response


# ---------------------------------------------------------------------------
# Request boundary: cross-origin guard, CSRF double submit, sliding renewal
# ---------------------------------------------------------------------------

def _allowed_origins(request: Request, settings: Settings) -> set[str]:
    allowed: set[str] = set()
    host = str(request.headers.get("host") or "").split(":")[0].strip().lower()
    if host:
        allowed.add(host)
    issuer = str(getattr(settings, "oauth_issuer", "") or "")
    issuer_host = urllib.parse.urlsplit(issuer).hostname
    if issuer_host:
        allowed.add(issuer_host.lower())
    for extra in str(getattr(settings, "auth_allowed_origins", "") or "").split(","):
        candidate = extra.strip()
        if not candidate:
            continue
        hostname = urllib.parse.urlsplit(candidate).hostname or candidate
        allowed.add(hostname.lower())
    return allowed


def origin_allowed(request: Request, settings: Settings | None = None) -> bool:
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return True
    if origin == "null":
        return False
    hostname = urllib.parse.urlsplit(origin).hostname
    if not hostname:
        return False
    return hostname.lower() in _allowed_origins(request, settings or get_settings())


def csrf_token_valid(request: Request, state: SessionState) -> bool:
    header = str(request.headers.get(CSRF_HEADER) or "").strip()
    cookie = str((request.cookies or {}).get(CSRF_COOKIE) or "").strip()
    if not header or not cookie or not state.csrf:
        return False
    if not hmac.compare_digest(header, cookie):
        return False
    return hmac.compare_digest(header, state.csrf)


async def enforce_workbench_session(request: Request, call_next):
    """Cross-origin guard + CSRF double submit + sliding session renewal."""
    path = request.url.path
    if path.startswith(BOUNDARY_EXEMPT_PREFIXES):
        return await call_next(request)

    settings = get_settings()
    method = request.method.upper()
    is_api = path.startswith("/api/")

    if is_api and method not in SAFE_METHODS and not origin_allowed(request, settings):
        logger.warning("Blocked cross-origin %s %s", method, path)
        return JSONResponse({"detail": "cross_origin_request_blocked"}, status_code=403)

    state = await load_session(request, settings=settings)
    if (
        state is not None
        and is_api
        and method not in SAFE_METHODS
        and not path.startswith(CSRF_EXEMPT_PATHS)
        and not csrf_token_valid(request, state)
    ):
        return JSONResponse({"detail": "csrf_token_invalid"}, status_code=403)

    response = await call_next(request)

    if (
        state is not None
        and sessions_available(settings)
        and (is_api or path.startswith("/pages/"))
        and needs_renewal(state)
    ):
        try:
            issued = create_session(state.username, remember=state.remember, settings=settings)
            apply_session_cookies(response, issued, settings)
        except Exception:  # never break a working request because renewal failed
            logger.warning("Session renewal failed for %s", state.username, exc_info=False)
    return response

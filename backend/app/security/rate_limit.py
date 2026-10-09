"""Small fixed-window counters used to slow down credential brute force.

Two backends are supported:

* Redis (``LITAI_AUTH_REDIS_URL``, database 2 by default) so counters survive
  a backend restart and are shared if the backend is ever scaled out;
* an in-process fallback so login keeps working (and keeps being rate limited)
  when Redis is unreachable.

The limiter is intentionally simple: ``INCR`` + ``EXPIRE`` per fixed window,
which is atomic, cheap, and good enough for a single-owner workbench.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_REDIS_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after: int


class FixedWindowRateLimiter:
    def __init__(self, redis_url: str = "", *, namespace: str = "litai:auth:rl"):
        self.redis_url = str(redis_url or "").strip()
        self.namespace = namespace
        self._client = None
        self._client_url = ""
        self._client_unavailable_until = 0.0
        self._memory: dict[str, tuple[float, int]] = {}

    def resolved_url(self) -> str:
        """Explicit URL wins; otherwise read the live setting (never cached at import)."""
        if self.redis_url:
            return self.redis_url
        try:
            from app.config import get_settings

            return str(get_settings().auth_redis_url or "").strip()
        except Exception:  # pragma: no cover - settings must never break login
            return ""

    # ------------------------------------------------------------------ redis
    async def _get_client(self):
        url = self.resolved_url()
        if not url:
            return None
        if time.monotonic() < self._client_unavailable_until:
            return None
        if self._client is not None and self._client_url == url:
            return self._client
        self._client = None
        try:
            from redis.asyncio import from_url as redis_from_url

            client = redis_from_url(
                url,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
                decode_responses=True,
            )
            await client.ping()
        except Exception:
            self._client_unavailable_until = time.monotonic() + _REDIS_RETRY_SECONDS
            logger.warning("Auth rate-limit Redis unavailable; using in-process counters")
            return None
        self._client = client
        self._client_url = url
        return client

    # ------------------------------------------------------------------ public
    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        """Record one event for ``key``; report whether it stayed within ``limit``."""
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        client = await self._get_client()
        if client is not None:
            try:
                redis_key = f"{self.namespace}:{key}"
                count = await client.incr(redis_key)
                if count == 1:
                    await client.expire(redis_key, window_seconds)
                ttl = await client.ttl(redis_key)
                retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
                return RateLimitDecision(count <= limit, max(0, limit - count), retry_after)
            except Exception:
                self._client = None
                self._client_unavailable_until = time.monotonic() + _REDIS_RETRY_SECONDS
                logger.warning("Auth rate-limit Redis call failed; using in-process counters")

        now = time.monotonic()
        window_start, count = self._memory.get(key, (now, 0))
        if now - window_start >= window_seconds:
            window_start, count = now, 0
        count += 1
        self._memory[key] = (window_start, count)
        if len(self._memory) > 5000:
            for stale_key, (stale_start, _count) in list(self._memory.items()):
                if now - stale_start >= window_seconds:
                    self._memory.pop(stale_key, None)
        retry_after = max(1, int(window_seconds - (now - window_start)))
        return RateLimitDecision(count <= limit, max(0, limit - count), retry_after)

    async def peek(self, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        """Report the current counter state without recording an event."""
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        client = await self._get_client()
        if client is not None:
            try:
                count = int(await client.get(f"{self.namespace}:{key}") or 0)
                ttl = await client.ttl(f"{self.namespace}:{key}")
                retry_after = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
                return RateLimitDecision(count < limit, max(0, limit - count), retry_after)
            except Exception:
                self._client = None
                self._client_unavailable_until = time.monotonic() + _REDIS_RETRY_SECONDS
        now = time.monotonic()
        window_start, count = self._memory.get(key, (now, 0))
        if now - window_start >= window_seconds:
            return RateLimitDecision(True, limit, window_seconds)
        return RateLimitDecision(count < limit, max(0, limit - count), max(1, int(window_seconds - (now - window_start))))

    async def reset(self, key: str) -> None:
        client = await self._get_client()
        if client is not None:
            try:
                await client.delete(f"{self.namespace}:{key}")
            except Exception:
                self._client = None
                self._client_unavailable_until = time.monotonic() + _REDIS_RETRY_SECONDS
        self._memory.pop(key, None)

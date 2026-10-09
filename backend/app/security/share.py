from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings


_rate_lock = asyncio.Lock()
_active_lock = asyncio.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)
_active_requests = 0


async def enforce_share_protection(request: Request, call_next):
    global _active_requests
    if not request.url.path.startswith("/api/share/"):
        return await call_next(request)
    if request.method not in {"GET", "HEAD"}:
        return JSONResponse({"detail": "Share access is read-only"}, status_code=405)

    settings = get_settings()
    client = (request.client.host if request.client else "unknown") or "unknown"
    token = request.url.path.split("/", 4)[3] if request.url.path.count("/") >= 3 else "unknown"
    key = f"{client}:{token}"
    now = time.monotonic()
    async with _rate_lock:
        entries = _requests[key]
        while entries and entries[0] <= now - 60:
            entries.popleft()
        if len(entries) >= max(1, settings.share_rate_limit_per_minute):
            return JSONResponse({"detail": "Share rate limit exceeded"}, status_code=429)
        entries.append(now)

    async with _active_lock:
        if _active_requests >= max(1, settings.share_max_concurrency):
            return JSONResponse({"detail": "Share concurrency limit exceeded"}, status_code=429)
        _active_requests += 1
    try:
        response = await call_next(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    finally:
        async with _active_lock:
            _active_requests -= 1

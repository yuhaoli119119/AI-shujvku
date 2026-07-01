from __future__ import annotations

from typing import Any

UNTRUSTED_EXTERNAL_SOURCE_IDENTITY = "untrusted:external_analysis"
UNTRUSTED_HTTP_SOURCE_IDENTITY = "untrusted:http_external_analysis"
UNTRUSTED_LEGACY_SOURCE_IDENTITY = "untrusted:legacy_external_analysis"


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def normalize_external_source_identity(
    source_identity: str | None,
    source_identity_verified: Any,
    *,
    default_untrusted: str = UNTRUSTED_EXTERNAL_SOURCE_IDENTITY,
) -> tuple[str, bool]:
    identity = str(source_identity or "").strip()
    verified = _truthy(source_identity_verified) and bool(identity)
    if verified:
        return identity, True
    if identity.casefold().startswith("untrusted:"):
        return identity, False
    return default_untrusted, False


def review_source_identity(
    source_identity: str | None,
    source_identity_verified: Any,
    *,
    default_untrusted: str = UNTRUSTED_EXTERNAL_SOURCE_IDENTITY,
) -> str:
    identity, _verified = normalize_external_source_identity(
        source_identity,
        source_identity_verified,
        default_untrusted=default_untrusted,
    )
    return identity


def review_submission_identity(
    payload: dict[str, Any],
    *,
    default_untrusted: str = UNTRUSTED_EXTERNAL_SOURCE_IDENTITY,
) -> str:
    return review_source_identity(
        payload.get("source_identity"),
        payload.get("source_identity_verified"),
        default_untrusted=default_untrusted,
    ).casefold()

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
    identity = review_source_identity(
        payload.get("source_identity"),
        payload.get("source_identity_verified"),
        default_untrusted=default_untrusted,
    )
    verified = _truthy(payload.get("source_identity_verified"))
    normalized_identity = identity.casefold()
    if verified and not normalized_identity.startswith("untrusted:"):
        return normalized_identity

    # HTTP imports without MCP auth all share the same untrusted identity.
    # Fall back to caller-supplied review labels so independently imported AI
    # opinions still count as distinct submissions instead of collapsing into
    # one synthetic reviewer.
    source_label = str(payload.get("source_label") or "").strip().casefold()
    source = str(payload.get("source") or "").strip().casefold()
    agent_role = str(payload.get("agent_role") or "").strip().casefold()
    model_name = str(payload.get("model_name") or "").strip().casefold()
    if source_label:
        return "|".join(
            part
            for part in (
                normalized_identity,
                f"source_label:{source_label}",
                f"source:{source}" if source else "",
                f"agent_role:{agent_role}" if agent_role else "",
                f"model:{model_name}" if model_name else "",
            )
            if part
        )
    if source:
        return "|".join(
            part
            for part in (
                normalized_identity,
                f"source:{source}",
                f"agent_role:{agent_role}" if agent_role else "",
                f"model:{model_name}" if model_name else "",
            )
            if part
        )
    return normalized_identity

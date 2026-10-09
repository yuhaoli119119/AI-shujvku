from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ActiveSiteMetal,
    CatalystSample,
    DFTResult,
    DFTSetting,
    ElectrochemicalPerformance,
    ExtractionFieldReview,
    MechanismClaim,
)
from app.utils.configuration_index import extract_configuration_index


TARGET_TYPE_ALIASES = {
    "catalystsample": "catalyst_samples",
    "catalyst_samples": "catalyst_samples",
    "catalystsample_schema": "catalyst_samples",
    "dftsetting": "dft_settings",
    "dft_setting": "dft_settings",
    "dft_settings": "dft_settings",
    "dftresult": "dft_results",
    "dft_result": "dft_results",
    "dft_results": "dft_results",
    "figure": "figures",
    "figures": "figures",
    "table": "tables",
    "tables": "tables",
    "mechanismclaim": "mechanism_claims",
    "mechanism_claim": "mechanism_claims",
    "mechanism_claims": "mechanism_claims",
    "electrochemicalperformance": "electrochemical_performance",
    "electrochemical_performance": "electrochemical_performance",
}

TARGET_TYPE_MODELS = {
    "catalyst_samples": CatalystSample,
    "dft_settings": DFTSetting,
    "dft_results": DFTResult,
    "mechanism_claims": MechanismClaim,
    "electrochemical_performance": ElectrochemicalPerformance,
}

_CATALYST_IDENTITY_CACHE_KEY = "authoritative_dft_catalyst_identity"


def get_dft_catalyst_identity(
    session: Session,
    catalyst_id: Any,
) -> tuple[CatalystSample | None, list[ActiveSiteMetal]]:
    """Read catalyst/site identity through the request-scoped authority cache."""
    if catalyst_id is None:
        return None, []
    cache = session.info.get(_CATALYST_IDENTITY_CACHE_KEY)
    cache_key = str(catalyst_id)
    if isinstance(cache, dict) and cache_key in cache:
        catalyst, sites = cache[cache_key]
        return catalyst, list(sites)

    catalyst = session.get(CatalystSample, catalyst_id)
    sites = list(session.scalars(
        select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == catalyst_id)
    ).all())
    sites.sort(key=lambda site: (site.active_site_key, site.site_role, str(site.id)))
    if isinstance(cache, dict):
        cache[cache_key] = (catalyst, sites)
    return catalyst, sites


def preload_dft_catalyst_identity(session: Session, rows: list[DFTResult]) -> None:
    """Preload catalyst/site identity for one authority-gate scope only."""
    cache = session.info.get(_CATALYST_IDENTITY_CACHE_KEY)
    if not isinstance(cache, dict):
        return
    catalyst_ids = {row.catalyst_sample_id for row in rows if row.catalyst_sample_id is not None}
    if not catalyst_ids:
        return
    catalysts = session.scalars(select(CatalystSample).where(CatalystSample.id.in_(catalyst_ids))).all()
    sites_by_catalyst: dict[Any, list[ActiveSiteMetal]] = {}
    for site in session.scalars(
        select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id.in_(catalyst_ids))
    ).all():
        sites_by_catalyst.setdefault(site.catalyst_sample_id, []).append(site)
    for catalyst in catalysts:
        cache[str(catalyst.id)] = (catalyst, sorted(
            sites_by_catalyst.get(catalyst.id, []),
            key=lambda site: (site.active_site_key, site.site_role, str(site.id)),
        ))

FIELD_PATHS = {
    "catalyst_samples": {
        "name": "catalyst_samples.name.value",
        "catalyst_type": "catalyst_samples.catalyst_type.value",
        "metal_centers": "catalyst_samples.metal_centers.value",
        "coordination": "catalyst_samples.coordination.value",
        "support": "catalyst_samples.support.value",
        "synthesis_method": "catalyst_samples.synthesis_method.value",
    },
    "dft_settings": {
        "software": "dft_settings.software.value",
        "functional": "dft_settings.functional.value",
        "dispersion_correction": "dft_settings.dispersion_correction.value",
        "pseudopotential": "dft_settings.pseudopotential.value",
        "cutoff_energy": "dft_settings.cutoff_energy.value",
        "k_points": "dft_settings.k_points.value",
        "convergence_settings": "dft_settings.convergence_settings.value",
        "vacuum_thickness": "dft_settings.vacuum_thickness.value",
    },
    "dft_results": {
        "catalyst": "dft_results.catalyst.value",
        "adsorbate": "dft_results.adsorbate.value",
        "energy_type": "dft_results.energy_type.value",
        "value": "dft_results.value.value",
        "reaction_step": "dft_results.reaction_step.value",
    },
    "mechanism_claims": {
        "claim_type": "mechanism_claims.claim_type.value",
        "claim_text": "mechanism_claims.claim_text.value",
        "key_species": "mechanism_claims.key_species.value",
        "mechanism_direction": "mechanism_claims.mechanism_direction.value",
    },
    "electrochemical_performance": {
        "sulfur_loading": "electrochemical_performance.sulfur_loading.value",
        "sulfur_content": "electrochemical_performance.sulfur_content.value",
        "electrolyte_sulfur_ratio": "electrochemical_performance.electrolyte_sulfur_ratio.value",
        "capacity": "electrochemical_performance.capacity.value",
        "cycle_number": "electrochemical_performance.cycle_number.value",
        "rate": "electrochemical_performance.rate.value",
        "decay_per_cycle": "electrochemical_performance.decay_per_cycle.value",
    },
}

ACTIVE_REVIEW_STATUSES = {"active", "remapped"}


@dataclass
class ReviewTargetResolution:
    status: str
    target_id: str | None
    score: float
    reason: str


def canonical_target_type(value: str) -> str:
    key = (value or "").replace("-", "_").replace(" ", "_").lower()
    canonical = TARGET_TYPE_ALIASES.get(key)
    if canonical is None:
        raise ValueError(f"Unsupported target_type: {value}")
    return canonical


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _canonicalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonicalize(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    return _normalize_text(value)


def _hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(_canonicalize(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_summary(target_type: str, entity: Any) -> dict[str, Any]:
    if target_type == "dft_settings":
        raw = entity.raw_json or {}
        evidence_text = raw.get("supporting_text") or raw.get("extracted") or ""
        return {"evidence_text": evidence_text, "source_section": "computational_details", "page_span": None}
    if target_type == "dft_results":
        return {
            "evidence_text": entity.evidence_text or "",
            "source_section": entity.source_section or "",
            "page_span": None,
        }
    if target_type == "mechanism_claims":
        return {"evidence_text": entity.evidence_text or "", "source_section": entity.claim_type or "", "page_span": None}
    if target_type == "electrochemical_performance":
        return {"evidence_text": entity.evidence_text or "", "source_section": "electrochemical_performance", "page_span": None}
    return {
        "evidence_text": entity.evidence_strength or entity.synthesis_method or entity.name or "",
        "source_section": entity.coordination or entity.support or "",
        "page_span": None,
    }


class ReviewTargetResolver:
    def __init__(self, session: Session) -> None:
        self.session = session

    def build_target_fingerprint(self, target_type: str, entity: Any) -> str:
        canonical_type = canonical_target_type(target_type)
        payload = {
            "target_type": canonical_type,
            "semantic": self._fingerprint_payload(canonical_type, entity),
            "evidence": _evidence_summary(canonical_type, entity),
        }
        return _hash_payload(payload)

    def build_target_label(self, target_type: str, entity: Any) -> str:
        canonical_type = canonical_target_type(target_type)
        if canonical_type == "catalyst_samples":
            metals = "-".join(entity.metal_centers or [])
            return " / ".join(part for part in [metals or entity.name, entity.coordination, entity.support] if part) or "catalyst sample"
        if canonical_type == "dft_settings":
            return " / ".join(part for part in [entity.software, entity.functional, entity.pseudopotential] if part) or "dft setting"
        if canonical_type == "dft_results":
            value_text = "" if entity.value is None else f"{entity.value} {entity.unit or ''}".strip()
            return " / ".join(part for part in [entity.property_type, entity.adsorbate, value_text] if part) or "dft result"
        if canonical_type == "mechanism_claims":
            return _normalize_text(entity.claim_text)[:180] or "mechanism claim"
        value_text = "" if entity.capacity_value is None else f"{entity.capacity_value} mAh/g"
        cycle_text = "" if entity.cycle_number is None else f"{entity.cycle_number} cycles"
        return " / ".join(part for part in [value_text, entity.rate, cycle_text] if part) or "electrochemical performance"

    def build_field_path(self, target_type: str, entity: Any, field_name: str) -> str:
        canonical_type = canonical_target_type(target_type)
        try:
            return FIELD_PATHS[canonical_type][field_name]
        except KeyError as exc:
            raise ValueError(f"Unsupported field for {canonical_type}: {field_name}") from exc

    def resolve_review_target(
        self,
        review: ExtractionFieldReview,
        current_entities: dict[str, list[Any]] | None = None,
    ) -> ReviewTargetResolution:
        canonical_type = canonical_target_type(review.target_type)
        model = TARGET_TYPE_MODELS[canonical_type]
        existing = self.session.scalar(select(model).where(model.paper_id == review.paper_id, model.id == UUID(str(review.target_id))))
        if existing is not None:
            return ReviewTargetResolution("active", str(existing.id), 1.0, "target_id still exists")

        entities = current_entities or {canonical_type: self._load_entities(review.paper_id, canonical_type)}
        candidates = entities.get(canonical_type, [])
        if not candidates:
            return ReviewTargetResolution("unresolved", None, 0.0, "no current entities for target type")

        fingerprint_matches = [
            entity for entity in candidates if review.target_fingerprint and self.build_target_fingerprint(canonical_type, entity) == review.target_fingerprint
        ]
        if len(fingerprint_matches) == 1:
            return ReviewTargetResolution("remapped", str(fingerprint_matches[0].id), 1.0, "exact target fingerprint match")
        if len(fingerprint_matches) > 1:
            return ReviewTargetResolution("ambiguous", None, 0.95, "multiple entities share target fingerprint")

        ranked: list[tuple[float, Any]] = []
        for entity in candidates:
            score = self._fallback_match_score(review, canonical_type, entity)
            if score >= 0.75:
                ranked.append((score, entity))
        ranked.sort(key=lambda item: item[0], reverse=True)

        if not ranked:
            return ReviewTargetResolution("stale", None, 0.0, "no semantic match after re-extraction")
        if len(ranked) > 1 and abs(ranked[0][0] - ranked[1][0]) < 0.05:
            return ReviewTargetResolution("ambiguous", None, ranked[0][0], "multiple high-confidence semantic candidates")
        return ReviewTargetResolution("remapped", str(ranked[0][1].id), ranked[0][0], "semantic label/field/evidence match")

    def remap_reviews_for_paper(self, paper_id: UUID) -> dict[str, int]:
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)
        ).all()
        current_entities = {
            target_type: self._load_entities(paper_id, target_type) for target_type in TARGET_TYPE_MODELS
        }
        summary = {"active": 0, "remapped": 0, "stale": 0, "ambiguous": 0, "unresolved": 0}

        for review in reviews:
            canonical_type = canonical_target_type(review.target_type)
            resolution = self.resolve_review_target(review, current_entities=current_entities)
            summary[resolution.status] += 1
            if resolution.status == "active":
                review.target_resolution_status = "active"
                review.last_resolved_target_id = review.target_id
                target = self._get_entity_by_id(paper_id, canonical_type, review.target_id)
                if target is not None:
                    self._refresh_review_identity(review, canonical_type, target)
                continue
            if resolution.status == "remapped" and resolution.target_id:
                conflict = self.session.scalar(
                    select(ExtractionFieldReview).where(
                        ExtractionFieldReview.paper_id == paper_id,
                        ExtractionFieldReview.target_type == canonical_type,
                        ExtractionFieldReview.target_id == resolution.target_id,
                        ExtractionFieldReview.field_name == review.field_name,
                        ExtractionFieldReview.id != review.id,
                    )
                )
                if conflict is not None:
                    review.target_resolution_status = "ambiguous"
                    review.last_resolved_target_id = None
                    summary["remapped"] -= 1
                    summary["ambiguous"] += 1
                    continue
                old_target_id = review.target_id
                review.target_id = resolution.target_id
                review.target_resolution_status = "remapped"
                review.remapped_from_target_id = old_target_id
                review.last_resolved_target_id = resolution.target_id
                target = self._get_entity_by_id(paper_id, canonical_type, resolution.target_id)
                if target is not None:
                    self._refresh_review_identity(review, canonical_type, target)
                continue
            review.target_resolution_status = resolution.status
            review.last_resolved_target_id = None
        self.session.flush()
        return summary

    def backfill_review_targets(self, paper_id: UUID) -> int:
        reviews = self.session.scalars(
            select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == paper_id)
        ).all()
        updated = 0
        for review in reviews:
            target = self._get_entity_by_id(review.paper_id, review.target_type, review.target_id)
            if target is None:
                if not review.target_resolution_status:
                    review.target_resolution_status = "unresolved"
                continue
            self._refresh_review_identity(review, review.target_type, target)
            if not review.last_resolved_target_id:
                review.last_resolved_target_id = review.target_id
            if not review.target_resolution_status:
                review.target_resolution_status = "active"
            updated += 1
        self.session.flush()
        return updated

    def _refresh_review_identity(self, review: ExtractionFieldReview, target_type: str, entity: Any) -> None:
        canonical_type = canonical_target_type(target_type)
        review.target_fingerprint = self.build_target_fingerprint(canonical_type, entity)
        review.target_label = self.build_target_label(canonical_type, entity)
        review.field_path = self.build_field_path(canonical_type, entity, review.field_name)

    def _load_entities(self, paper_id: UUID, target_type: str) -> list[Any]:
        canonical_type = canonical_target_type(target_type)
        model = TARGET_TYPE_MODELS[canonical_type]
        return self.session.scalars(select(model).where(model.paper_id == paper_id)).all()

    def _get_entity_by_id(self, paper_id: UUID, target_type: str, target_id: str) -> Any | None:
        canonical_type = canonical_target_type(target_type)
        model = TARGET_TYPE_MODELS[canonical_type]
        return self.session.scalar(select(model).where(model.paper_id == paper_id, model.id == UUID(str(target_id))))

    def _fingerprint_payload(self, target_type: str, entity: Any) -> dict[str, Any]:
        if target_type == "catalyst_samples":
            return {
                "name": entity.name,
                "catalyst_type": entity.catalyst_type,
                "metal_centers": entity.metal_centers or [],
                "coordination": entity.coordination,
                "support": entity.support,
                "synthesis_method": entity.synthesis_method,
            }
        if target_type == "dft_settings":
            return {
                "software": entity.software,
                "functional": entity.functional,
                "dispersion_correction": entity.dispersion_correction,
                "pseudopotential": entity.pseudopotential,
                "cutoff_energy_ev": entity.cutoff_energy_ev,
                "k_points": entity.k_points,
                "convergence_settings": entity.convergence_settings or {},
                "vacuum_thickness_a": entity.vacuum_thickness_a,
            }
        if target_type == "dft_results":
            payload_dict = entity.evidence_payload if hasattr(entity, "evidence_payload") else (entity.get("evidence_payload") if isinstance(entity, dict) else None)
            cfg_idx = extract_configuration_index(payload_dict)
            payload_dict = payload_dict if isinstance(payload_dict, dict) else {}
            catalyst_id = entity.catalyst_sample_id if not isinstance(entity, dict) else entity.get("catalyst_sample_id")
            catalyst = None if not isinstance(entity, dict) else entity.get("catalyst_sample")
            active_sites: list[dict[str, Any]] = []
            if catalyst_id and not isinstance(entity, dict):
                from sqlalchemy.orm import object_session

                session = object_session(entity)
                if session is not None:
                    cached_identity = session.info.get(_CATALYST_IDENTITY_CACHE_KEY)
                    cached_pair = cached_identity.get(str(catalyst_id)) if isinstance(cached_identity, dict) else None
                    if cached_pair is not None:
                        catalyst, site_rows = cached_pair
                    else:
                        catalyst = session.get(CatalystSample, catalyst_id)
                        site_rows = session.scalars(
                            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == catalyst_id)
                        ).all()
                    active_sites = [
                        {
                            "id": str(site.id),
                            "active_site_key": site.active_site_key,
                            "site_type": site.site_type,
                            "site_role": site.site_role,
                            "element_symbol": site.element_symbol,
                            "element_order": site.element_order,
                            "normalized_pair_key": site.normalized_pair_key,
                        }
                        for site in site_rows
                    ]
                    active_sites.sort(key=lambda site: (
                        str(site["active_site_key"]), str(site["site_role"]), str(site["id"])
                    ))
            catalyst_identity = None
            if catalyst is not None:
                catalyst_identity = {
                    "id": str(catalyst.id),
                    "name": catalyst.name,
                    "catalyst_type": catalyst.catalyst_type,
                    "metal_centers": sorted(str(metal) for metal in (catalyst.metal_centers or [])),
                    "coordination": catalyst.coordination,
                    "support": catalyst.support,
                    "active_sites": active_sites,
                }
            return {
                "catalyst_sample_id": str(catalyst_id) if catalyst_id else None,
                "catalyst_identity": catalyst_identity,
                "adsorbate": entity.adsorbate if not isinstance(entity, dict) else entity.get("adsorbate"),
                "property_type": entity.property_type if not isinstance(entity, dict) else entity.get("property_type"),
                "value": entity.value if not isinstance(entity, dict) else entity.get("value"),
                "value_upper": entity.value_upper if not isinstance(entity, dict) else entity.get("value_upper"),
                "value_kind": entity.value_kind if not isinstance(entity, dict) else entity.get("value_kind"),
                "unit": entity.unit if not isinstance(entity, dict) else entity.get("unit"),
                "reaction_step": entity.reaction_step if not isinstance(entity, dict) else entity.get("reaction_step"),
                "configuration_index": cfg_idx,
                "source_section": entity.source_section if not isinstance(entity, dict) else entity.get("source_section"),
                "source_figure": entity.source_figure if not isinstance(entity, dict) else entity.get("source_figure"),
                "evidence_source_identity": {
                    key: payload_dict.get(key)
                    for key in ("source_paper_id", "evidence_paper_id", "source_document_id", "source_document_type")
                    if payload_dict.get(key) is not None
                },
            }
        if target_type == "mechanism_claims":
            return {
                "claim_type": entity.claim_type,
                "claim_text": entity.claim_text,
                "evidence_types": entity.evidence_types or [],
            }
        return {
            "sulfur_loading_mg_cm2": entity.sulfur_loading_mg_cm2,
            "sulfur_content_wt_percent": entity.sulfur_content_wt_percent,
            "electrolyte_sulfur_ratio": entity.electrolyte_sulfur_ratio,
            "capacity_value": entity.capacity_value,
            "cycle_number": entity.cycle_number,
            "rate": entity.rate,
            "decay_per_cycle": entity.decay_per_cycle,
        }

    def _fallback_match_score(self, review: ExtractionFieldReview, target_type: str, entity: Any) -> float:
        score = 0.0
        label = self.build_target_label(target_type, entity)
        field_path = self.build_field_path(target_type, entity, review.field_name)
        evidence = _evidence_summary(target_type, entity)["evidence_text"]

        if review.target_label and _normalize_text(review.target_label) == _normalize_text(label):
            score += 0.55
        if review.field_path and review.field_path == field_path:
            score += 0.2
        score += 0.25 * self._evidence_similarity(review.evidence_text or "", evidence or "")
        return min(score, 0.99)

    @staticmethod
    def _evidence_similarity(left: str, right: str) -> float:
        left_norm = _normalize_text(left)
        right_norm = _normalize_text(right)
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0
        if left_norm in right_norm or right_norm in left_norm:
            return 0.9
        left_tokens = set(left_norm.split())
        right_tokens = set(right_norm.split())
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = left_tokens & right_tokens
        union = left_tokens | right_tokens
        return len(overlap) / len(union)

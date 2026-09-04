from __future__ import annotations

from collections import Counter, defaultdict
import json
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, DFTResult, Paper, PaperRelationship
from app.services.dft_audit_issue_lifecycle_service import DFTAuditIssueLifecycleService
from app.services.evidence_review_bundle_service import EvidenceReviewBundleService
from app.services.supplementary_dft_lifecycle_service import SUPPLEMENTARY_RELATIONSHIP_TYPES
from app.utils.workbench_status import EXTRACTION_PROTOCOL_VERSION


class DFTCandidatePreviewService:
    """Read existing DFT candidate sources and cluster them without persistence."""

    def __init__(
        self,
        session: Session,
        *,
        review_state_provider: Callable[[UUID], dict[str, Any]] | None = None,
    ) -> None:
        self.session = session
        self.identity_service = DFTAuditIssueLifecycleService(session)
        self.review_state_provider = review_state_provider or (
            lambda paper_id: EvidenceReviewBundleService(session).get_review_task(paper_id)
        )

    def build_preview(self, paper_id: UUID) -> dict[str, Any]:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise LookupError("Paper not found")

        candidates = [
            self._candidate_from_result(row, identity_root_paper_id=paper_id, source_type="stage2")
            for row in self._stage2_results_for_paper(paper_id)
        ]
        candidates.extend(self._completed_figure_review_candidates(paper_id))

        supplementary_ids = list(
            self.session.scalars(
                select(PaperRelationship.target_paper_id).where(
                    PaperRelationship.source_paper_id == paper_id,
                    PaperRelationship.relationship_type.in_(SUPPLEMENTARY_RELATIONSHIP_TYPES),
                )
            ).all()
        )
        if supplementary_ids:
            supplementary_rows = self.session.scalars(
                select(DFTResult).where(DFTResult.paper_id.in_(supplementary_ids))
            ).all()
            candidates.extend(
                self._candidate_from_result(
                    row,
                    identity_root_paper_id=paper_id,
                    source_type="si",
                )
                for row in supplementary_rows
                if self._is_stage2_result(row)
            )

        return self.preview_candidates(
            paper_id=paper_id,
            paper_code=paper.paper_code,
            candidates=candidates,
        )

    def preview_candidates(
        self,
        *,
        paper_id: UUID,
        paper_code: str | None,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized = [self._with_identity(paper_id, candidate) for candidate in candidates]
        source_counts = Counter(str(item.get("source_type") or "unknown") for item in normalized)

        incomplete: list[dict[str, Any]] = []
        by_subject: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for candidate in normalized:
            subject_key = candidate.get("subject_key")
            observation_key = candidate.get("observation_key")
            if not candidate.get("identity_complete") or not subject_key or not observation_key:
                incomplete.append(candidate)
                continue
            by_subject[str(subject_key)][str(observation_key)].append(candidate)

        conflict_subjects = {
            subject_key for subject_key, observations in by_subject.items() if len(observations) > 1
        }
        clusters: list[dict[str, Any]] = []
        for subject_key in sorted(by_subject):
            observations = by_subject[subject_key]
            for observation_key in sorted(observations):
                members = observations[observation_key]
                sources = self._unique_sources(members)
                possible_conflict = subject_key in conflict_subjects
                cluster_type = (
                    "SAME_SUBJECT_DIFFERENT_VALUE"
                    if possible_conflict
                    else "EXACT_OBSERVATION"
                    if len(sources) > 1 or len(members) > 1
                    else "DISTINCT_SUBJECT"
                )
                clusters.append(
                    {
                        "cluster_type": cluster_type,
                        "subject_key": subject_key,
                        "observation_key": observation_key,
                        "scientific_fields": members[0]["scientific_fields"],
                        "candidate_count": len(members),
                        "evidence_count": len(sources),
                        "sources": sources,
                        "candidates": members,
                        "identity_warnings": sorted(
                            {
                                warning
                                for member in members
                                for warning in member.get("identity_warnings", [])
                            }
                        ),
                        "possible_conflict": possible_conflict,
                        "possible_rounding": False,
                    }
                )

        for index, candidate in enumerate(incomplete):
            clusters.append(
                {
                    "cluster_type": "INCOMPLETE_IDENTITY",
                    "subject_key": candidate.get("subject_key"),
                    "observation_key": None,
                    "scientific_fields": candidate["scientific_fields"],
                    "candidate_count": 1,
                    "evidence_count": len(candidate.get("evidence_sources") or []),
                    "sources": candidate.get("evidence_sources") or [],
                    "candidates": [candidate],
                    "identity_warnings": candidate.get("identity_warnings") or [],
                    "possible_conflict": False,
                    "possible_rounding": False,
                    "incomplete_index": index,
                }
            )

        complete_clusters = [
            cluster for cluster in clusters if cluster["cluster_type"] != "INCOMPLETE_IDENTITY"
        ]
        return {
            "schema_version": "unified_dft_candidate_preview_v1",
            "paper_id": str(paper_id),
            "paper_code": paper_code,
            "read_only": True,
            "source_counts": {
                "stage2": source_counts.get("stage2", 0),
                "figure_review": source_counts.get("figure_review", 0),
                "si": source_counts.get("si", 0),
            },
            "clusters": clusters,
            "summary": {
                "total_candidates": len(normalized),
                "scientific_observations": len(complete_clusters),
                "exact_clusters": sum(
                    1
                    for cluster in complete_clusters
                    if cluster["candidate_count"] > 1 or cluster["evidence_count"] > 1
                ),
                "multi_evidence_clusters": sum(
                    1 for cluster in complete_clusters if cluster["evidence_count"] > 1
                ),
                "possible_conflicts": len(conflict_subjects),
                "incomplete_identity": len(incomplete),
                "distinct_observations": len(complete_clusters),
            },
        }

    def _stage2_results_for_paper(self, paper_id: UUID) -> list[DFTResult]:
        rows = self.session.scalars(
            select(DFTResult).where(DFTResult.paper_id == paper_id)
        ).all()
        return [row for row in rows if self._is_stage2_result(row)]

    @staticmethod
    def _is_stage2_result(row: DFTResult) -> bool:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        return bool(
            row.extraction_protocol_version == EXTRACTION_PROTOCOL_VERSION
            or evidence.get("system_extractor_protocol")
            or str(row.candidate_status or "").strip().lower() == "system_candidate"
        )

    def _candidate_from_result(
        self,
        row: DFTResult,
        *,
        identity_root_paper_id: UUID,
        source_type: str,
    ) -> dict[str, Any]:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        authoritative = self.identity_service.authoritative_payload_for_result(row)
        corrected = (
            authoritative.get("corrected_value")
            if isinstance(authoritative.get("corrected_value"), dict)
            else {}
        )
        if source_type == "stage2" and row.paper_id == identity_root_paper_id:
            identity = self.identity_service.identity_for_result(row)
        else:
            identity = self.identity_service.build_identity(
                paper_id=identity_root_paper_id,
                payload=authoritative,
            )
        candidate = {
            "source_type": source_type,
            "source_paper_id": str(row.paper_id),
            "source_record_id": str(row.id),
            "evidence_id": None,
            "evidence_text": row.evidence_text,
            "page": evidence.get("page"),
            "figure_id": evidence.get("figure_id"),
            "figure_label": row.source_figure or evidence.get("figure_label"),
            "table_id": evidence.get("source_table_id"),
            "table_caption": evidence.get("source_table_caption") or evidence.get("table"),
            "row": evidence.get("source_row_index"),
            "column": evidence.get("source_column_index"),
            "material_identity": evidence.get("material_identity")
            or evidence.get("catalyst_name")
            or corrected.get("material_identity"),
            "adsorbate": row.adsorbate,
            "property_type": row.property_type,
            "property_subtype": evidence.get("property_subtype"),
            "raw_value": row.value,
            "raw_value_upper": row.value_upper,
            "value_kind": row.value_kind,
            "raw_unit": row.unit,
            "site_label": evidence.get("site_label")
            or evidence.get("active_site_context")
            or corrected.get("site_label"),
            "active_site_instance_key": evidence.get("active_site_instance_key"),
            "atom_pair": evidence.get("atom_pair"),
            "reaction_step": row.reaction_step,
            "state_context": evidence.get("state_context") or evidence.get("structure_context"),
            "calculation_context": evidence.get("property_context")
            or evidence.get("calculation_context"),
            "confidence": row.confidence,
            "evidence_sources": self._result_evidence_sources(row, source_type=source_type),
            "_identity": identity,
        }
        return candidate

    def _completed_figure_review_candidates(self, paper_id: UUID) -> list[dict[str, Any]]:
        state = self.review_state_provider(paper_id)
        completed_fingerprint = str(state.get("completed_snapshot_fingerprint") or "").strip()
        if (
            state.get("stage_status") != "completed"
            or not completed_fingerprint
            or completed_fingerprint != str(state.get("current_snapshot_fingerprint") or "").strip()
        ):
            return []
        audits = self.session.scalars(
            select(AuditLog)
            .where(
                AuditLog.paper_id == paper_id,
                AuditLog.action == "offline_evidence_review_applied",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        ).all()
        payload = self._select_current_completed_review_payload(
            audits,
            completed_snapshot_fingerprint=completed_fingerprint,
            latest_review_run_id=self._text(state.get("latest_review_run_id")),
        )
        if payload is None:
            return []
        review_run_id = payload.get("review_run_id")
        candidates: list[dict[str, Any]] = []
        for raw in payload.get("dft_evidence_candidates") or []:
            if not isinstance(raw, dict):
                continue
            source_paper_id = self._text(raw.get("source_paper_id")) or str(paper_id)
            candidates.append(
                {
                    "source_type": "figure_review",
                    "source_paper_id": source_paper_id,
                    "source_record_id": self._text(raw.get("source_record_id")),
                    "evidence_id": self._text(raw.get("evidence_id")),
                    "evidence_text": self._text(raw.get("raw_text")),
                    "page": raw.get("page"),
                    "figure_id": self._text(raw.get("figure_id")),
                    "figure_label": self._text(raw.get("figure_label")),
                    "table_id": self._text(raw.get("table_id") or raw.get("source_record_id"))
                    if raw.get("source_kind") == "table"
                    else None,
                    "table_caption": self._text(raw.get("table_caption")),
                    "row": self._coalesce(raw.get("row"), raw.get("source_row_index")),
                    "column": self._coalesce(raw.get("column"), raw.get("source_column_index")),
                    "material_identity": self._text(raw.get("material_identity")),
                    "adsorbate": self._text(raw.get("adsorbate")),
                    "property_type": self._text(raw.get("property_type")),
                    "property_subtype": self._text(raw.get("property_subtype")),
                    "raw_value": raw.get("value"),
                    "raw_value_upper": raw.get("value_upper"),
                    "value_kind": self._text(raw.get("value_kind")),
                    "raw_unit": self._text(raw.get("unit")),
                    "site_label": self._text(raw.get("site_label") or raw.get("adsorption_site")),
                    "active_site_instance_key": self._text(raw.get("active_site_instance_key")),
                    "atom_pair": raw.get("atom_pair"),
                    "reaction_step": self._text(raw.get("reaction_step")),
                    "state_context": self._text(raw.get("state_context")),
                    "calculation_context": raw.get("calculation_context")
                    or raw.get("property_context"),
                    "confidence": raw.get("confidence"),
                    "review_run_id": review_run_id,
                    "_identity_input": dict(raw),
                }
            )
        return candidates

    @staticmethod
    def _select_current_completed_review_payload(
        audits: list[AuditLog],
        *,
        completed_snapshot_fingerprint: str,
        latest_review_run_id: str | None = None,
    ) -> dict[str, Any] | None:
        for audit in audits:
            payload = audit.payload if isinstance(audit.payload, dict) else {}
            response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
            fingerprint = str(
                payload.get("completed_snapshot_fingerprint")
                or response.get("completed_snapshot_fingerprint")
                or ""
            ).strip()
            if (
                payload.get("stage_status") == "completed"
                and not payload.get("run_id")
                and (
                    str(audit.id) == latest_review_run_id
                    if latest_review_run_id
                    else fingerprint == completed_snapshot_fingerprint
                )
            ):
                return {**payload, "review_run_id": str(audit.id)}
        return None

    def _with_identity(self, paper_id: UUID, candidate: dict[str, Any]) -> dict[str, Any]:
        public = {key: value for key, value in candidate.items() if not key.startswith("_")}
        identity = candidate.get("_identity")
        if identity is None:
            identity = self.identity_service.build_identity(
                paper_id=paper_id,
                payload=self._identity_payload(
                    candidate,
                    evidence_payload=candidate.get("_identity_input"),
                ),
            )
        identity_payload = identity.identity_payload if isinstance(identity.identity_payload, dict) else {}
        subject = identity_payload.get("subject") if isinstance(identity_payload.get("subject"), dict) else {}
        observation = (
            identity_payload.get("observation")
            if isinstance(identity_payload.get("observation"), dict)
            else {}
        )
        evidence_sources = public.get("evidence_sources")
        if not isinstance(evidence_sources, list) or not evidence_sources:
            evidence_sources = [self._source_from_candidate(public)]
        public.update(
            {
                "subject_key": identity.subject_key,
                "observation_key": identity.observation_key,
                "identity_version": identity.identity_version,
                "identity_complete": bool(identity.dedupe_allowed and identity.observation_key),
                "identity_warnings": list(identity.error_codes),
                "scientific_fields": {
                    "material_identity": subject.get("material_key"),
                    "adsorbate": subject.get("adsorbate"),
                    "property_type": subject.get("property_type"),
                    "property_subtype": subject.get("property_subtype"),
                    "reaction_step": subject.get("reaction_step"),
                    "active_site_instance_key": subject.get("active_site_instance_key"),
                    "atom_pair": subject.get("canonical_atom_pair"),
                    "site_label": subject.get("site_label"),
                    "state_context": subject.get("state_context"),
                    "calculation_context": subject.get("property_context") or {},
                    "canonical_value": observation.get("value"),
                    "canonical_value_upper": observation.get("value_upper"),
                    "value_kind": observation.get("value_kind"),
                    "canonical_unit": observation.get("unit"),
                },
                "evidence_sources": evidence_sources,
            }
        )
        return public

    @staticmethod
    def _identity_payload(
        candidate: dict[str, Any],
        *,
        evidence_payload: Any = None,
    ) -> dict[str, Any]:
        corrected = {
            "material_identity": candidate.get("material_identity"),
            "adsorbate": candidate.get("adsorbate"),
            "property_type": candidate.get("property_type"),
            "property_subtype": candidate.get("property_subtype"),
            "value": candidate.get("raw_value"),
            "value_upper": candidate.get("raw_value_upper"),
            "value_kind": candidate.get("value_kind"),
            "unit": candidate.get("raw_unit"),
            "site_label": candidate.get("site_label"),
            "active_site_instance_key": candidate.get("active_site_instance_key"),
            "atom_pair": candidate.get("atom_pair"),
            "reaction_step": candidate.get("reaction_step"),
            "state_context": candidate.get("state_context"),
            "property_context": candidate.get("calculation_context"),
        }
        evidence = evidence_payload if isinstance(evidence_payload, dict) else dict(candidate)
        return {"corrected_value": corrected, "evidence_payload": evidence}

    @classmethod
    def _result_evidence_sources(cls, row: DFTResult, *, source_type: str) -> list[dict[str, Any]]:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        raw_sources = evidence.get("evidence_sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raw_sources = [evidence]
        sources: list[dict[str, Any]] = []
        for raw in raw_sources:
            source = raw if isinstance(raw, dict) else {}
            sources.append(
                {
                    "source_type": source_type,
                    "source_paper_id": str(row.paper_id),
                    "source_record_id": str(row.id),
                    "evidence_id": source.get("evidence_id"),
                    "evidence_text": source.get("evidence_text") or source.get("quoted_text") or row.evidence_text,
                    "page": cls._coalesce(source.get("page"), evidence.get("page")),
                    "section": source.get("section") or row.source_section,
                    "figure_id": source.get("figure_id"),
                    "figure_label": source.get("figure") or row.source_figure,
                    "table_id": source.get("source_table_id") or evidence.get("source_table_id"),
                    "table_caption": source.get("table") or evidence.get("source_table_caption"),
                    "row": cls._coalesce(
                        source.get("source_row_index"), evidence.get("source_row_index")
                    ),
                    "column": cls._coalesce(
                        source.get("source_column_index"), evidence.get("source_column_index")
                    ),
                    "confidence": source.get("confidence") if source.get("confidence") is not None else row.confidence,
                }
            )
        return cls._dedupe_dicts(sources)

    @staticmethod
    def _source_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            key: candidate.get(key)
            for key in (
                "source_type",
                "source_paper_id",
                "source_record_id",
                "evidence_id",
                "evidence_text",
                "page",
                "figure_id",
                "figure_label",
                "table_id",
                "table_caption",
                "row",
                "column",
                "confidence",
                "review_run_id",
            )
            if candidate.get(key) is not None
        }

    @classmethod
    def _unique_sources(cls, members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return cls._dedupe_dicts(
            [source for member in members for source in member.get("evidence_sources") or []]
        )

    @staticmethod
    def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(item)
        return unique

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _coalesce(*values: Any) -> Any:
        return next((value for value in values if value is not None), None)

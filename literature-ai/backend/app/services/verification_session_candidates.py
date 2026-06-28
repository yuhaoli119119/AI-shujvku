from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AuditLog,
    CatalystSample,
    DFTResult,
    EvidenceLocator,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
)
from app.services.dft_audit_issue_service import DFTAuditIssueService
from app.services.dft_rescan_policy import (
    is_dft_method_only_reaction_step,
    normalize_dft_reaction_step_for_identity,
    normalize_source_document_type,
)
from app.utils.evidence_anchors import has_evidence_anchor


class VerificationSessionDFTCandidateMixin:
    def _materialize_new_dft_candidates(self, *, paper_id: UUID, reviewer: str) -> dict[str, Any]:
        rows = self.session.execute(
            select(ExternalAnalysisCandidate, ExternalAnalysisRun)
            .join(ExternalAnalysisRun, ExternalAnalysisRun.id == ExternalAnalysisCandidate.run_id)
            .where(
                ExternalAnalysisCandidate.paper_id == paper_id,
                ExternalAnalysisCandidate.candidate_type == "object_review_audit",
                ExternalAnalysisCandidate.status.in_(("candidate", "pending", "requires_resolution")),
            )
            .order_by(ExternalAnalysisCandidate.created_at.asc())
        ).all()
        existing_by_signature = self._existing_new_dft_signatures(paper_id)
        existing_by_semantic_signature = self._existing_new_dft_semantic_signatures(paper_id)
        existing_by_method_step_signature = self._existing_new_dft_method_step_signatures(paper_id)
        materialized: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        issue_service = DFTAuditIssueService(self.session)
        for candidate, run in rows:
            payload = candidate.normalized_payload if isinstance(candidate.normalized_payload, dict) else {}
            target_type = self._normalize_object_review_target_type(payload.get("target_type"))
            decision = str(payload.get("decision") or "").strip().lower()
            target_id = str(payload.get("target_id") or "").strip().lower()
            if target_type != "dft_results" or (decision != "new_candidate" and target_id != "new"):
                continue
            issue = issue_service.create_or_update_missing_issue(
                paper_id=paper_id,
                candidate=candidate,
                run=run,
                payload=payload,
            )
            if bool(payload.get("borrowed_from_reference")):
                skipped.append({"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"})
                self._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")
                continue
            if self._is_supporting_reference_dft_payload(payload):
                skipped.append({"candidate_id": str(candidate.id), "reason": "borrowed_supporting_reference"})
                self._retire_skipped_new_dft_candidate(candidate, reason="borrowed_supporting_reference")
                continue
            candidate_item, reason = self._new_dft_candidate_item(payload, run=run)
            if candidate_item is None:
                skipped.append({"candidate_id": str(candidate.id), "reason": reason})
                self._retire_skipped_new_dft_candidate(candidate, reason=reason)
                continue
            signature = candidate_item["signature"]
            existing = existing_by_signature.get(signature)
            if existing is None:
                semantic_matches = existing_by_semantic_signature.get(
                    self._new_dft_semantic_signature(candidate_item),
                    [],
                )
                if len(semantic_matches) == 1:
                    existing = semantic_matches[0]
            if existing is None:
                method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
                if method_step_signature is not None:
                    existing = self._method_step_compatible_existing(
                        candidate_item,
                        existing_by_method_step_signature.get(method_step_signature, []),
                    )
            if existing is None:
                existing = self._insert_new_dft_candidate(
                    paper_id=paper_id,
                    candidate_item=candidate_item,
                    source_label=run.source_label or run.source or reviewer,
                )
                existing_by_signature[signature] = existing
                semantic_signature = self._new_dft_semantic_signature(candidate_item)
                existing_by_semantic_signature.setdefault(semantic_signature, []).append(existing)
                method_step_signature = self._new_dft_method_step_compatible_signature(candidate_item)
                if method_step_signature is not None:
                    existing_by_method_step_signature.setdefault(method_step_signature, []).append(existing)
                action = "created"
            else:
                self._maybe_upgrade_method_only_reaction_step(existing, candidate_item)
                action = "deduplicated"
            candidate.status = "materialized"
            candidate.materialized_target_type = "dft_results"
            candidate.materialized_target_id = str(existing.id)
            self.session.add(candidate)
            materialized.append(
                {
                    "candidate_id": str(candidate.id),
                    "action": action,
                    "dft_result_id": str(existing.id),
                    "issue_id": str(issue.id),
                    "property_type": existing.property_type,
                    "value": existing.value,
                    "unit": existing.unit,
                }
            )
        if materialized:
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="materialize_new_dft_candidates",
                    source=reviewer,
                    target_type="paper",
                    target_id=str(paper_id),
                    payload={
                        "created_or_linked_count": len(materialized),
                        "skipped_count": len(skipped),
                        "policy": "IDE AI new_candidate rows become unverified DFTResult candidates only; they are not exportable/RAG-ready until the existing DFT safety gate passes.",
                    },
                )
            )
        self.session.flush()
        return {
            "materialized_count": len(materialized),
            "materialized_items": materialized,
            "skipped_count": len(skipped),
            "skipped_items": skipped,
        }

    def _new_dft_candidate_item(
        self,
        payload: dict[str, Any],
        *,
        run: ExternalAnalysisRun,
    ) -> tuple[dict[str, Any] | None, str]:
        corrected = payload.get("corrected_value")
        if not isinstance(corrected, dict):
            return None, "missing_structured_corrected_value"
        material_identity = self._first_text(
            corrected.get("material_identity"),
            corrected.get("material"),
            corrected.get("catalyst"),
            payload.get("normalized_material"),
            payload.get("normalized_material_or_catalyst"),
        )
        property_type = self._normalize_dft_property(
            self._first_text(
                corrected.get("property_type"),
                corrected.get("property"),
                corrected.get("energy_type"),
                payload.get("normalized_energy_type"),
            )
        )
        value = self._float_or_none(corrected.get("value"))
        unit = self._first_text(corrected.get("unit"))
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        if not material_identity:
            return None, "missing_material_identity"
        if not property_type:
            return None, "missing_property_type"
        if value is None:
            return None, "missing_value"
        if not unit:
            return None, "missing_unit"
        if not has_evidence_anchor(evidence):
            return None, "missing_evidence_anchor"
        evidence_payload = evidence if isinstance(evidence, dict) else {"evidence": evidence}
        source_table = self._first_text(corrected.get("source_table"), evidence_payload.get("table"))
        source_section = self._first_text(
            evidence_payload.get("section"),
            evidence_payload.get("section_title"),
            f"Page {evidence_payload.get('page')}" if evidence_payload.get("page") not in (None, "") else None,
        )
        source_figure = self._first_text(corrected.get("source_figure"), source_table, evidence_payload.get("figure"))
        method = self._first_text(corrected.get("method"), corrected.get("calculation_method"))
        temperature = self._first_text(corrected.get("temperature"), corrected.get("temperature_label"))
        reaction_step = self._first_text(
            corrected.get("reaction_step"),
            " | ".join(part for part in [method, temperature] if part),
        )
        adsorbate = self._first_text(corrected.get("adsorbate"), payload.get("adsorbate"), "H2")
        evidence_text = self._first_text(
            evidence_payload.get("quoted_text"),
            evidence_payload.get("evidence_text"),
            payload.get("reason"),
        )
        merged_evidence_payload = {
            **evidence_payload,
            "material_identity": material_identity,
            "source_label": run.source_label,
            "source": run.source,
            "corrected_value": corrected,
            "dedupe_signature": payload.get("dedupe_signature"),
            "import_policy": "new_candidate_unverified_dft_result",
        }
        signature = self._new_dft_signature(
            material_identity=material_identity,
            property_type=property_type,
            adsorbate=adsorbate,
            value=value,
            unit=unit,
            reaction_step=reaction_step,
            source_figure=source_figure,
            page=evidence_payload.get("page"),
        )
        return (
            {
                "material_identity": material_identity,
                "property_type": property_type,
                "adsorbate": adsorbate,
                "value": value,
                "unit": unit,
                "reaction_step": reaction_step,
                "source_section": source_section,
                "source_figure": source_figure,
                "evidence_text": evidence_text,
                "confidence": payload.get("confidence"),
                "evidence_payload": merged_evidence_payload,
                "signature": signature,
            },
            "",
        )

    def _insert_new_dft_candidate(
        self,
        *,
        paper_id: UUID,
        candidate_item: dict[str, Any],
        source_label: str,
    ) -> DFTResult:
        identity = self._new_dft_identity(candidate_item["signature"])
        existing = self.session.scalar(
            select(DFTResult).where(
                DFTResult.paper_id == paper_id,
                DFTResult.candidate_identity == identity,
            )
        )
        if existing is not None:
            return existing
        row = DFTResult(
            paper_id=paper_id,
            adsorbate=candidate_item["adsorbate"],
            property_type=candidate_item["property_type"],
            value=candidate_item["value"],
            unit=candidate_item["unit"],
            reaction_step=candidate_item["reaction_step"],
            source_section=candidate_item["source_section"],
            source_figure=candidate_item["source_figure"],
            evidence_text=candidate_item["evidence_text"],
            confidence=candidate_item["confidence"],
            candidate_status="new_candidate",
            evidence_payload=candidate_item["evidence_payload"],
            extraction_protocol_version="ide_ai_new_candidate_v1",
            candidate_identity=identity,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            winner = self.session.scalar(
                select(DFTResult).where(
                    DFTResult.paper_id == paper_id,
                    DFTResult.candidate_identity == identity,
                )
            )
            if winner is None:
                raise
            return winner
        self._upsert_new_dft_locator(row, candidate_item["evidence_payload"], source_label=source_label)
        return row

    @staticmethod
    def _new_dft_identity(signature: tuple[str, ...]) -> str:
        canonical = json.dumps(list(signature), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _upsert_new_dft_locator(self, row: DFTResult, evidence_payload: dict[str, Any], *, source_label: str) -> None:
        page = self._int_or_none(evidence_payload.get("page"))
        if page is None:
            return
        locator = EvidenceLocator(
            paper_id=row.paper_id,
            source_type="table" if evidence_payload.get("table") else "pdf",
            target_type="dft_results",
            target_id=str(row.id),
            field_name="value",
            page=page,
            section=evidence_payload.get("section") or evidence_payload.get("section_title") or row.source_section,
            evidence_text=str(evidence_payload.get("quoted_text") or evidence_payload.get("evidence_text") or row.evidence_text or "PDF evidence"),
            locator_status="exact_page",
            locator_confidence=float(row.confidence or 0.8),
            parser_source=str(source_label or "external_ai_review")[:32],
        )
        self.session.add(locator)

    def _existing_new_dft_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], DFTResult]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], DFTResult] = {}
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            signature = self._new_dft_signature(
                material_identity=material_identity,
                property_type=row.property_type,
                adsorbate=row.adsorbate,
                value=row.value,
                unit=row.unit,
                reaction_step=row.reaction_step,
                source_figure=row.source_figure,
                page=evidence_payload.get("page"),
            )
            signatures.setdefault(signature, row)
        return signatures

    def _existing_new_dft_semantic_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], list[DFTResult]]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], list[DFTResult]] = defaultdict(list)
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                if sample is not None and str(sample.name or "").strip():
                    material_identity = str(sample.name).strip()
            signature = self._new_dft_semantic_signature(
                {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                }
            )
            signatures[signature].append(row)
        return signatures

    def _existing_new_dft_method_step_signatures(self, paper_id: UUID) -> dict[tuple[str, ...], list[DFTResult]]:
        rows = self.session.scalars(select(DFTResult).where(DFTResult.paper_id == paper_id)).all()
        signatures: dict[tuple[str, ...], list[DFTResult]] = defaultdict(list)
        for row in rows:
            evidence_payload = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
            material_identity = self._first_text(evidence_payload.get("material_identity"))
            if row.catalyst_sample_id:
                sample = self.session.get(CatalystSample, row.catalyst_sample_id)
                if sample is not None and str(sample.name or "").strip():
                    material_identity = str(sample.name).strip()
            signature = self._new_dft_method_step_compatible_signature(
                {
                    "material_identity": material_identity,
                    "property_type": row.property_type,
                    "value": row.value,
                    "unit": row.unit,
                    "adsorbate": row.adsorbate,
                    "reaction_step": row.reaction_step,
                }
            )
            if signature is not None:
                signatures[signature].append(row)
        return signatures

    @staticmethod
    def _new_dft_semantic_signature(candidate_item: dict[str, Any]) -> tuple[str, ...]:
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                candidate_item.get("material_identity"),
                candidate_item.get("property_type"),
                value_part,
                candidate_item.get("unit"),
                candidate_item.get("adsorbate"),
                normalize_dft_reaction_step_for_identity(
                    candidate_item.get("reaction_step"),
                    property_type=candidate_item.get("property_type"),
                    adsorbate=candidate_item.get("adsorbate"),
                    material=candidate_item.get("material_identity"),
                ),
            )
        )

    @staticmethod
    def _new_dft_method_step_compatible_signature(candidate_item: dict[str, Any]) -> tuple[str, ...] | None:
        property_type = str(candidate_item.get("property_type") or "").strip().lower()
        if property_type != "adsorption_energy":
            return None
        value = candidate_item.get("value")
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                "method_step_compatible",
                candidate_item.get("material_identity"),
                candidate_item.get("property_type"),
                value_part,
                candidate_item.get("unit"),
                candidate_item.get("adsorbate"),
            )
        )

    @staticmethod
    def _method_step_compatible_existing(candidate_item: dict[str, Any], rows: list[DFTResult]) -> DFTResult | None:
        if not rows:
            return None
        candidate_method_only = is_dft_method_only_reaction_step(candidate_item.get("reaction_step"))
        if candidate_method_only:
            specific_rows = [row for row in rows if not is_dft_method_only_reaction_step(row.reaction_step)]
            candidates = specific_rows or rows
            return candidates[0] if len(candidates) == 1 else None

        method_only_rows = [row for row in rows if is_dft_method_only_reaction_step(row.reaction_step)]
        return method_only_rows[0] if len(rows) == 1 and len(method_only_rows) == 1 else None

    def _maybe_upgrade_method_only_reaction_step(self, row: DFTResult, candidate_item: dict[str, Any]) -> None:
        candidate_step = self._first_text(candidate_item.get("reaction_step"))
        if not candidate_step:
            return
        if is_dft_method_only_reaction_step(candidate_step):
            return
        if not is_dft_method_only_reaction_step(row.reaction_step):
            return
        if str(row.candidate_status or "").strip().lower() != "new_candidate":
            return
        row.reaction_step = candidate_step
        self.session.add(row)

    @staticmethod
    def _new_dft_signature(
        *,
        material_identity: Any,
        property_type: Any,
        value: Any,
        unit: Any,
        adsorbate: Any,
        reaction_step: Any,
        source_figure: Any,
        page: Any,
    ) -> tuple[str, ...]:
        value_part = "" if value is None else f"{float(value):.8g}"
        return tuple(
            str(part or "").strip().lower()
            for part in (
                material_identity,
                property_type,
                value_part,
                unit,
                adsorbate,
                normalize_dft_reaction_step_for_identity(
                    reaction_step,
                    property_type=property_type,
                    adsorbate=adsorbate,
                    material=material_identity,
                ),
                source_figure,
                page,
            )
        )

    @staticmethod
    def _is_supporting_reference_dft_payload(payload: dict[str, Any]) -> bool:
        evidence = payload.get("evidence_location") or payload.get("evidence_payload")
        evidence = evidence if isinstance(evidence, dict) else {}
        corrected = payload.get("corrected_value") if isinstance(payload.get("corrected_value"), dict) else {}
        source_type = normalize_source_document_type(
            payload.get("source_document_type")
            or payload.get("source_type")
            or evidence.get("source_document_type")
            or evidence.get("source_type")
            or corrected.get("source_document_type")
            or corrected.get("source_type")
        )
        return source_type == "supporting_reference"

    @staticmethod
    def _normalize_dft_property(value: Any) -> str | None:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "activation_energy": "activation_energy",
            "activation": "activation_energy",
            "permeance": "permeance",
            "permeability": "permeance",
            "adsorption_energy": "adsorption_energy",
            "reaction_barrier": "reaction_barrier",
            "permeation_barrier": "permeation_barrier",
        }
        return aliases.get(text, text or None)

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    ContentEvidenceItem,
    ContentWebReviewBundleV2,
    MechanismClaim,
    Paper,
    PaperRelationship,
    PaperSection,
    WritingCard,
)
from app.utils.artifact_paths import resolve_paper_pdf_path


POLICY_VERSION = "content_web_review_bundle_v2.1"
RESULT_SCHEMA = "content_web_review_proposal_v2"
DECISIONS = {"PASS", "REVISE", "REJECT", "NEEDS_HUMAN"}


class ContentWebReviewBundleV2Service:
    """Build and validate web-AI proposals without applying any review result.

    The persisted record is deliberately separate from ``ContentReviewBundle``.
    Its only state transition after validation is to await a local verifier.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def generate(self, *, paper_id: UUID, created_by: str = "user") -> dict[str, Any]:
        paper = self._paper(paper_id)
        manifest = self._build_manifest(paper)
        bundle = ContentWebReviewBundleV2(
            paper_id=paper.id,
            policy_version=POLICY_VERSION,
            snapshot_fingerprint=manifest["bundle_fingerprint"],
            manifest=manifest,
            status="generated",
            created_by=created_by,
        )
        self.session.add(bundle)
        self.session.flush()
        return self._bundle_response(bundle)

    def download(self, bundle_id: UUID) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        manifest = bundle.manifest or {}
        content = self._zip(manifest)
        paper = self._paper(bundle.paper_id)
        return {
            "content": content,
            "filename": f"{paper.paper_code or paper.id}_content_web_review_v2.zip",
            "fingerprint": bundle.snapshot_fingerprint,
        }

    def validate_web_proposal(self, bundle_id: UUID, proposal: dict[str, Any]) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        stale = self._stale_report(bundle)
        if stale["is_stale"]:
            bundle.status = "stale"
            bundle.manifest = {**(bundle.manifest or {}), "last_stale_report": stale}
            self.session.add(bundle)
            return {"valid": False, "bundle_id": str(bundle.id), "status": bundle.status, "errors": ["stale_bundle"], "stale": stale}

        errors = self._validate_proposal(bundle, proposal)
        if errors:
            bundle.status = "proposal_invalid"
            bundle.manifest = {**(bundle.manifest or {}), "last_validation_errors": errors}
            self.session.add(bundle)
            return {"valid": False, "bundle_id": str(bundle.id), "status": bundle.status, "errors": errors}

        # Canonicalize server-owned safety fields; client declarations cannot
        # turn this web proposal into an identity-verified or final truth write.
        bundle.proposal_payload = {
            **proposal,
            "proposal_status": "web_ai_proposal",
            "source_identity_verified": False,
            "writes_final_truth": False,
            "local_ai_verification": None,
        }
        bundle.status = "web_proposal_validated"
        self.session.add(bundle)
        plan = self._local_plan(bundle)
        bundle.status = "awaiting_human" if plan["local_required_target_count"] == 0 else "awaiting_local_verification"
        self.session.add(bundle)
        return {
            "valid": True,
            "bundle_id": str(bundle.id),
            "status": bundle.status,
            "web_reviewed_target_count": plan["web_reviewed_target_count"],
            "local_required_target_count": plan["local_required_target_count"],
            "local_skipped_target_count": plan["local_skipped_target_count"],
        }

    def local_verification_plan(self, bundle_id: UUID) -> dict[str, Any]:
        bundle = self._bundle(bundle_id)
        stale = self._stale_report(bundle)
        if stale["is_stale"]:
            bundle.status = "stale"
            bundle.manifest = {**(bundle.manifest or {}), "last_stale_report": stale}
            self.session.add(bundle)
            return {"bundle_id": str(bundle.id), "status": "stale", "stale": stale, "local_verification_plan": None}
        if bundle.status not in {"web_proposal_validated", "awaiting_local_verification", "awaiting_human"} or not bundle.proposal_payload:
            raise ValueError("content_web_review_v2_proposal_must_be_validated")
        return self._local_plan(bundle)

    def _build_manifest(self, paper: Paper) -> dict[str, Any]:
        pdf = self._pdf_descriptor(paper)
        source_pdfs = self._source_pdfs(paper, pdf)
        targets = self._targets(paper, pdf)
        evidence = [target["evidence"] for target in targets]
        coverage = [
            {key: target[key] for key in ("plan_item_id", "target_type", "target_id", "field_name", "object_snapshot_hash")}
            for target in targets
        ]
        base = {
            "schema_version": "content_web_review_bundle_v2",
            "policy_version": POLICY_VERSION,
            "paper_id": str(paper.id),
            "paper_code": paper.paper_code,
            "targets": targets,
            "required_target_ids": [target["target_id"] for target in targets],
            "required_field_coverage": coverage,
            "allowed_evidence_refs": evidence,
            "allowed_pages": sorted({item["page"] for item in evidence if item["page"] is not None}),
            # Full package freshness covers the main paper and linked SI PDFs,
            # even though v2's first target set only contains existing content.
            "source_pdfs": source_pdfs,
            "gate_blockers": sorted({blocker for target in targets for blocker in target["gate_blockers"]}),
        }
        fingerprint = self._hash(base)
        return {
            **base,
            "bundle_fingerprint": fingerprint,
            "return_schema": self._return_schema(),
            "return_template": self._return_template(paper, fingerprint, targets),
            "local_verification_requirements": self._local_requirements(),
            "format_examples": self._format_examples(paper, fingerprint, targets),
            "instructions": self._instructions(),
        }

    def _targets(self, paper: Paper, pdf: dict[str, Any]) -> list[dict[str, Any]]:
        raw: list[tuple[str, str, str, Any, str | None, int | None, str | None]] = []
        if paper.abstract:
            raw.append(("paper_abstract", str(paper.id), "abstract", paper.abstract, paper.abstract, None, "abstract"))
        for section in self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id).order_by(PaperSection.id)).all():
            raw.append(("paper_section", str(section.id), "text", section.text, section.text, section.page_start, section.section_title))
        for claim in self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper.id).order_by(MechanismClaim.id)).all():
            raw.append(("mechanism_claim", str(claim.id), "claim_text", claim.claim_text, claim.evidence_text or claim.claim_text, None, claim.claim_type))
        card_fields = ("research_gap", "proposed_solution", "core_hypothesis", "figure_logic", "abstract_logic", "introduction_logic", "discussion_logic")
        for card in self.session.scalars(select(WritingCard).where(WritingCard.paper_id == paper.id).order_by(WritingCard.id)).all():
            for field_name in card_fields:
                value = getattr(card, field_name)
                if value:
                    raw.append(("writing_card", str(card.id), field_name, value, value, None, field_name))
        targets: list[dict[str, Any]] = []
        for index, (target_type, target_id, field_name, value, excerpt, page, label) in enumerate(raw, start=1):
            object_hash = self._hash({"target_type": target_type, "target_id": target_id, "field_name": field_name, "value": value})
            page_ref = f"source_pdf:{pdf['sha256'] or 'missing'}#page={page if page is not None else 'unlocated'}"
            page_asset_sha = self._hash({"pdf": pdf["sha256"], "page": page})
            evidence_id = f"evidence:{index:04d}"
            blockers = list(pdf["gate_blockers"])
            if page is None:
                blockers.append("page_unlocated")
            if not str(excerpt or "").strip():
                blockers.append("evidence_excerpt_missing")
            qualification = self._existing_formal_qualification(paper.id, target_type, target_id)
            evidence = {
                "evidence_ref_id": evidence_id,
                "source_paper_id": str(paper.id),
                "source_pdf_sha256": pdf["sha256"],
                "page": page,
                "page_asset_sha256": page_asset_sha,
                "page_asset_ref": page_ref,
                "evidence_excerpt": str(excerpt or ""),
                "evidence_asset_sha256": self._hash({"excerpt": excerpt, "page_asset_sha256": page_asset_sha}),
            }
            targets.append({
                "plan_item_id": f"content-v2-{index:04d}", "target_type": target_type, "target_id": target_id,
                "field_name": field_name, "current_value": value, "object_snapshot_hash": object_hash,
                "target_label": label, "gate_blockers": sorted(set(blockers)),
                "existing_formal_qualification": qualification, "evidence": evidence,
            })
        return targets

    def _validate_proposal(self, bundle: ContentWebReviewBundleV2, proposal: dict[str, Any]) -> list[str]:
        manifest = bundle.manifest or {}
        errors: list[str] = []
        if proposal.get("schema_version") != RESULT_SCHEMA:
            errors.append("unsupported_schema_version")
        if str(proposal.get("bundle_fingerprint") or "") != bundle.snapshot_fingerprint:
            errors.append("stale_fingerprint")
        if str(proposal.get("paper_id") or "") != str(bundle.paper_id) or proposal.get("paper_code") != manifest.get("paper_code"):
            errors.append("wrong_paper")
        if proposal.get("proposal_status") != "web_ai_proposal":
            errors.append("invalid_proposal_status")
        if proposal.get("source_identity_verified") is not False:
            errors.append("forged_source_identity")
        if proposal.get("writes_final_truth") is not False:
            errors.append("forged_final_truth")
        if proposal.get("local_ai_verification") is not None:
            errors.append("forged_local_verification")
        actions = proposal.get("actions")
        if not isinstance(actions, list):
            return [*errors, "actions_must_be_list"]
        expected = {item["plan_item_id"]: item for item in manifest.get("targets", [])}
        seen: set[str] = set()
        for action in actions:
            if not isinstance(action, dict):
                errors.append("action_must_be_object")
                continue
            plan_item_id = str(action.get("plan_item_id") or "")
            if plan_item_id in seen:
                errors.append(f"duplicate_plan_item_id:{plan_item_id}")
                continue
            seen.add(plan_item_id)
            target = expected.get(plan_item_id)
            if target is None:
                errors.append(f"unknown_plan_item_id:{plan_item_id}")
                continue
            for field in ("target_type", "target_id", "field_name", "object_snapshot_hash"):
                if str(action.get(field) or "") != str(target[field]):
                    errors.append(f"wrong_{field}:{plan_item_id}")
            if action.get("decision") not in DECISIONS:
                errors.append(f"invalid_decision:{plan_item_id}")
            evidence = target["evidence"]
            refs = action.get("evidence_ref_ids")
            if refs != [evidence["evidence_ref_id"]]:
                errors.append(f"unknown_or_missing_evidence_ref:{plan_item_id}")
            if action.get("evidence_asset_sha256") != evidence["evidence_asset_sha256"]:
                errors.append(f"wrong_evidence_asset_hash:{plan_item_id}")
            if action.get("page") != evidence["page"]:
                errors.append(f"wrong_page:{plan_item_id}")
            quote = str(action.get("evidence_quote") or "")
            if not quote or quote not in evidence["evidence_excerpt"]:
                errors.append(f"forged_quote:{plan_item_id}")
        if set(expected) != seen:
            errors.append("incomplete_required_field_coverage")
        discoveries = proposal.get("discovery_proposals", [])
        if not isinstance(discoveries, list):
            errors.append("discovery_proposals_must_be_list")
        else:
            for proposal_item in discoveries:
                if not isinstance(proposal_item, dict) or proposal_item.get("target_id") not in (None, ""):
                    errors.append("discovery_proposal_must_not_target_existing_or_new_id")
        return sorted(set(errors))

    def _local_plan(self, bundle: ContentWebReviewBundleV2) -> dict[str, Any]:
        manifest = bundle.manifest or {}
        actions = {item["plan_item_id"]: item for item in (bundle.proposal_payload or {}).get("actions", [])}
        required: list[dict[str, Any]] = []
        skipped = {"ordinary_reject": 0, "needs_human": 0, "discovery_proposals": len((bundle.proposal_payload or {}).get("discovery_proposals", []))}
        for target in manifest.get("targets", []):
            action = actions[target["plan_item_id"]]
            decision = action["decision"]
            requires = decision in {"PASS", "REVISE"} or (decision == "REJECT" and target["existing_formal_qualification"])
            if not requires:
                skipped["ordinary_reject" if decision == "REJECT" else "needs_human"] += 1
                continue
            evidence = target["evidence"]
            required.append({
                "plan_item_id": target["plan_item_id"], "target_type": target["target_type"], "target_id": target["target_id"],
                "field_name": target["field_name"], "decision": decision, "object_snapshot_hash": target["object_snapshot_hash"],
                "source_paper_id": evidence["source_paper_id"], "source_pdf_sha256": evidence["source_pdf_sha256"],
                "page": evidence["page"], "page_asset_sha256": evidence["page_asset_sha256"],
                "evidence_asset_sha256": evidence["evidence_asset_sha256"], "evidence_excerpt": evidence["evidence_excerpt"],
                "page_asset_ref": evidence["page_asset_ref"], "requires_page_render": evidence["page"] is not None,
                "layout_consistency_status": "pending_local_page_check" if evidence["page"] is not None else "page_unlocated",
                "gate_blockers": target["gate_blockers"],
            })
        # Keep every target's evidence/page contract at the object layer; the
        # following evidence/page lists are the deduplicated execution view.
        object_checks = list(required)
        evidence_checks = self._dedupe(required, ("source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"))
        page_checks = [item for item in evidence_checks if item["page"] is not None]
        batches: dict[str, list[dict[str, Any]]] = {}
        for item in page_checks:
            batches.setdefault(item["source_pdf_sha256"] or "missing_pdf", []).append(item)
        web_count = len(manifest.get("targets", []))
        return {
            "bundle_id": str(bundle.id), "status": bundle.status, "proposal_only": True,
            "web_reviewed_target_count": web_count, "local_required_target_count": len(required),
            "local_skipped_target_count": sum(skipped.values()), "local_skipped_target_count_by_reason": skipped,
            "required_object_checks": object_checks, "required_evidence_checks": evidence_checks,
            "required_page_checks": page_checks, "unique_page_checks": page_checks,
            "page_batches": [{"source_pdf_sha256": key, "checks": value} for key, value in sorted(batches.items())],
            "metrics": {
                "logical_page_read_count": len(page_checks), "physical_page_read_attempt_count": 0,
                "page_read_retry_count": 0, "page_cache_hit_count": 0,
                "physical_counter_note": "Plan generation reports logical work only; no page reader was invoked.",
            },
            "writes_final_truth": False, "local_ai_verification": None,
        }

    def _stale_report(self, bundle: ContentWebReviewBundleV2) -> dict[str, Any]:
        paper = self._paper(bundle.paper_id)
        current = self._build_manifest(paper)
        previous = bundle.manifest or {}
        changed: list[str] = []
        previous_targets = {item["plan_item_id"]: item for item in previous.get("targets", [])}
        current_targets = {item["plan_item_id"]: item for item in current.get("targets", [])}
        affected = sorted(set(previous_targets) ^ set(current_targets))
        for key in sorted(set(previous_targets) & set(current_targets)):
            before, after = previous_targets[key], current_targets[key]
            if before["object_snapshot_hash"] != after["object_snapshot_hash"]:
                changed.append("target")
                affected.append(key)
            elif before["evidence"]["evidence_asset_sha256"] != after["evidence"]["evidence_asset_sha256"]:
                changed.append("evidence_ref")
                affected.append(key)
            elif before["evidence"]["page_asset_sha256"] != after["evidence"]["page_asset_sha256"]:
                changed.append("page_asset")
                affected.append(key)
        if previous.get("source_pdfs") != current.get("source_pdfs"):
            changed.append("source_pdf")
            # The evidence/page hashes derive from this source. Report the
            # root invalidator once while still propagating to every target.
            changed = [dependency for dependency in changed if dependency not in {"evidence_ref", "page_asset"}]
            affected = list(current_targets)
        if previous.get("policy_version") != current.get("policy_version"):
            changed.append("policy_version")
            affected = list(current_targets)
        return {"is_stale": bool(changed), "changed_dependencies": sorted(set(changed)), "affected_plan_item_ids": sorted(set(affected)), "current_fingerprint": current["bundle_fingerprint"], "dependency_graph": "target -> evidence_ref -> page_asset -> source_pdf -> policy_version"}

    def _existing_formal_qualification(self, paper_id: UUID, target_type: str, target_id: str) -> bool:
        source_types = {"mechanism_claim": "mechanism_claim", "writing_card": "writing_card"}
        source_type = source_types.get(target_type)
        if not source_type:
            return False
        row = self.session.scalar(select(ContentEvidenceItem).where(
            ContentEvidenceItem.paper_id == paper_id, ContentEvidenceItem.source_type == source_type,
            ContentEvidenceItem.source_id == target_id,
        ))
        return bool(row and row.citation_status == "citable" and row.review_status in {"validated", "approved", "safe_verified"})

    def _pdf_descriptor(self, paper: Paper) -> dict[str, Any]:
        path = resolve_paper_pdf_path(paper.pdf_path, self.settings.storage_root)
        if path is None:
            return {"paper_id": str(paper.id), "path": paper.pdf_path, "sha256": None, "gate_blockers": ["source_pdf_missing"]}
        return {"paper_id": str(paper.id), "path": str(path), "sha256": self._file_hash(path), "gate_blockers": []}

    def _source_pdfs(self, paper: Paper, main_pdf: dict[str, Any]) -> list[dict[str, Any]]:
        descriptors = [{**main_pdf, "source_document_type": "main"}]
        relationship_types = {"supplementary", "supplementary_information", "supporting_information", "si"}
        links = self.session.scalars(select(PaperRelationship).where(PaperRelationship.source_paper_id == paper.id)).all()
        linked_ids = sorted({link.target_paper_id for link in links if str(link.relationship_type or "").lower() in relationship_types}, key=str)
        for linked_id in linked_ids:
            linked = self.session.get(Paper, linked_id)
            if linked is not None:
                descriptors.append({**self._pdf_descriptor(linked), "source_document_type": "supplementary"})
        return descriptors

    def _bundle_response(self, bundle: ContentWebReviewBundleV2) -> dict[str, Any]:
        manifest = bundle.manifest or {}
        return {"bundle_id": str(bundle.id), "status": bundle.status, "bundle_fingerprint": bundle.snapshot_fingerprint, "manifest": manifest,
                "download_url": f"/api/content-knowledge/review-bundles/{bundle.id}/download", "proposal_only": True,
                "writes_final_truth": False, "source_identity_verified": False, "local_ai_verification": None}

    @staticmethod
    def _dedupe(items: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, ...]] = set(); result: list[dict[str, Any]] = []
        for item in items:
            key = tuple(item[field] for field in fields)
            if key not in seen:
                seen.add(key); result.append(item)
        return result

    def _zip(self, manifest: dict[str, Any]) -> bytes:
        files: dict[str, Any] = {
            "manifest.json": manifest,
            "return_schema.json": manifest["return_schema"],
            "return_template.json": manifest["return_template"],
            "instructions_for_web_ai.md": manifest["instructions"],
            "required_target_ids.json": manifest["required_target_ids"],
            "required_field_coverage.json": manifest["required_field_coverage"],
            "allowed_evidence_refs.json": manifest["allowed_evidence_refs"],
            "allowed_pages.json": manifest["allowed_pages"],
            "local_verification_requirements.json": manifest["local_verification_requirements"],
            "format_examples.json": manifest["format_examples"],
        }
        buffer = BytesIO()
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for name, payload in files.items():
                archive.writestr(name, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            for evidence in manifest["allowed_evidence_refs"]:
                archive.writestr(f"evidence/{evidence['evidence_ref_id']}.json", json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
        return buffer.getvalue()

    @staticmethod
    def _return_schema() -> dict[str, Any]:
        return {"schema_version": RESULT_SCHEMA, "required": ["bundle_fingerprint", "paper_id", "paper_code", "proposal_status", "source_identity_verified", "writes_final_truth", "local_ai_verification", "actions"], "action_decisions": sorted(DECISIONS), "discovery_proposals": "optional; no target_id and never applied"}

    @staticmethod
    def _return_template(paper: Paper, fingerprint: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id), "paper_code": paper.paper_code, "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": [], "discovery_proposals": []}

    @staticmethod
    def _format_examples(paper: Paper, fingerprint: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        if not targets:
            return {"actions": []}
        target = targets[0]; evidence = target["evidence"]
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id), "paper_code": paper.paper_code, "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": [{"plan_item_id": target["plan_item_id"], "target_type": target["target_type"], "target_id": target["target_id"], "field_name": target["field_name"], "object_snapshot_hash": target["object_snapshot_hash"], "decision": "NEEDS_HUMAN", "evidence_ref_ids": [evidence["evidence_ref_id"]], "evidence_quote": evidence["evidence_excerpt"], "evidence_asset_sha256": evidence["evidence_asset_sha256"], "page": evidence["page"]}]}

    @staticmethod
    def _local_requirements() -> dict[str, Any]:
        return {"proposal_only": True, "required_result_fields": ["required_object_checks", "required_evidence_checks", "required_page_checks"], "page_dedupe_key": ["source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"], "no_apply_endpoint": True}

    @staticmethod
    def _instructions() -> str:
        return "Return only the v2 proposal JSON. Cover every supplied plan item exactly once. Use only supplied evidence_ref_ids, quotes, hashes and page numbers. Do not claim an identity, local verification, final truth, or create a target_id in discovery proposals. This package cannot apply any review result."

    def _paper(self, paper_id: UUID) -> Paper:
        paper = self.session.get(Paper, paper_id)
        if paper is None:
            raise ValueError("content_web_review_v2_paper_not_found")
        return paper

    def _bundle(self, bundle_id: UUID) -> ContentWebReviewBundleV2:
        bundle = self.session.get(ContentWebReviewBundleV2, bundle_id)
        if bundle is None:
            raise ValueError("content_web_review_v2_bundle_not_found")
        return bundle

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import (
    ContentWebReviewBundleV2,
    ContentWebReviewLocalVerificationResult,
    EvidenceLocator,
    MechanismClaim,
    Paper,
    PaperRelationship,
    PaperSection,
    WritingCard,
)
from app.utils.artifact_paths import resolve_paper_pdf_path, resolve_persisted_artifact_path
from app.utils.review_safety import CONTENT_OBJECT_GATE_POLICY_VERSION, content_object_gate


POLICY_VERSION = "content_web_review_bundle_v2.1"
RESULT_SCHEMA = "content_web_review_proposal_v2"
DECISIONS = {"PASS", "REVISE", "REJECT", "NEEDS_HUMAN"}
MODULES = {"abstract", "sections", "mechanism_knowledge", "writing_cards"}
PLAN_ITEM_NAMESPACE = UUID("4fc7b45c-08f3-58e6-bdf7-8e8420cb1c16")
TRUSTED_LAYOUT_VERIFIER_SOURCES = {"layout_verifier", "content_layout_verifier"}
TRUSTED_LAYOUT_STATUSES = {"verified", "consistent", "verified_consistent"}
TRUSTED_LAYOUT_MIN_CONFIDENCE = 0.95
TARGET_TYPE_ALIASES = {
    "paper_abstract": {"paper_abstract", "paper", "papers", "Paper"},
    "paper_section": {"paper_section", "paper_sections", "PaperSection"},
    "mechanism_claim": {"mechanism_claim", "mechanism_claims", "MechanismClaim"},
    "writing_card": {"writing_card", "writing_cards", "WritingCard"},
}


class ContentWebReviewBundleV2Service:
    """Build and validate web-AI proposals without applying any review result.

    The persisted record is deliberately separate from ``ContentReviewBundle``.
    Its only state transition after validation is to await a local verifier.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._page_asset_bytes: dict[str, bytes] = {}

    def generate(
        self,
        *,
        paper_id: UUID,
        module: str | None = None,
        modules: list[str] | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        paper = self._paper(paper_id)
        selected_modules = self._selected_modules(module=module, modules=modules)
        manifest = self._build_manifest(paper, selected_modules=selected_modules)
        if not manifest["targets"]:
            raise ValueError("content_web_review_v2_no_targets_for_selected_modules")
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
        if self._stale_report(bundle)["is_stale"]:
            raise ValueError("content_web_review_v2_bundle_stale")
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
            # Never let an invalid retry downgrade a previously accepted
            # proposal.  It is still rejected, but its immutable audit chain
            # and lifecycle status remain intact.
            if bundle.proposal_payload:
                return {
                    "valid": False,
                    "bundle_id": str(bundle.id),
                    "status": bundle.status,
                    "errors": errors,
                }
            bundle.status = "proposal_invalid"
            bundle.manifest = {**(bundle.manifest or {}), "last_validation_errors": errors}
            self.session.add(bundle)
            return {"valid": False, "bundle_id": str(bundle.id), "status": bundle.status, "errors": errors}

        canonical_proposal = self._canonical_web_proposal(proposal)
        if bundle.proposal_payload:
            # A proposal establishes the exact local-verification plan.  It
            # must stay immutable so later web uploads cannot reinterpret an
            # already stored local result (or alter a pending object's scope).
            if canonical_proposal != bundle.proposal_payload:
                return {
                    "valid": False,
                    "bundle_id": str(bundle.id),
                    "status": bundle.status,
                    "errors": ["content_web_review_v2_proposal_immutable_conflict"],
                }
            plan = self._local_plan(bundle)
            return {
                "valid": True,
                "idempotent": True,
                "bundle_id": str(bundle.id),
                "status": bundle.status,
                "web_reviewed_target_count": plan["web_reviewed_target_count"],
                "local_required_target_count": plan["local_required_target_count"],
                "local_skipped_target_count": plan["local_skipped_target_count"],
            }

        # Canonicalize server-owned safety fields; client declarations cannot
        # turn this web proposal into an identity-verified or final truth write.
        bundle.proposal_payload = canonical_proposal
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

    @staticmethod
    def _canonical_web_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
        return {
            **proposal,
            "proposal_status": "web_ai_proposal",
            "source_identity_verified": False,
            "writes_final_truth": False,
            "local_ai_verification": None,
        }

    def local_verification_plan(
        self, bundle_id: UUID, *, persist_stale: bool = True
    ) -> dict[str, Any]:
        """Return the server-owned local verification plan.

        The normal API lifecycle records a stale observation on the bundle.
        MCP evidence reads must be genuinely read-only, so they set
        ``persist_stale=False`` and receive the same stale blocker without a
        database mutation.
        """
        bundle = self._bundle(bundle_id)
        stale = self._stale_report(bundle)
        if stale["is_stale"]:
            if persist_stale:
                bundle.status = "stale"
                bundle.manifest = {**(bundle.manifest or {}), "last_stale_report": stale}
                self.session.add(bundle)
            return {"bundle_id": str(bundle.id), "status": "stale", "stale": stale, "local_verification_plan": None}
        if bundle.status not in {"web_proposal_validated", "awaiting_local_verification", "awaiting_human"} or not bundle.proposal_payload:
            raise ValueError("content_web_review_v2_proposal_must_be_validated")
        return self._local_plan(bundle)

    def read_local_verification_page_asset(
        self,
        bundle_id: UUID,
        *,
        source_paper_id: str,
        source_pdf_sha256: str,
        page: int,
        page_asset_ref: str,
        page_asset_sha256: str,
    ) -> dict[str, Any]:
        """Read one page asset explicitly required by a fresh local plan.

        Caller-supplied values are selectors only.  The bundle's validated
        proposal and freshly rebuilt dependency graph remain authoritative.
        No path supplied by the MCP client is ever opened.
        """
        bundle = self._bundle(bundle_id)
        plan = self.local_verification_plan(bundle_id, persist_stale=False)
        if plan["status"] == "stale":
            raise ValueError("content_web_review_v2_bundle_stale")
        manifest = bundle.manifest or {}
        if bundle.policy_version != POLICY_VERSION or manifest.get("policy_version") != POLICY_VERSION:
            raise ValueError("content_web_review_v2_policy_mismatch")

        requested = {
            "source_paper_id": str(source_paper_id or ""),
            "source_pdf_sha256": str(source_pdf_sha256 or ""),
            "page": page,
            "page_asset_ref": str(page_asset_ref or ""),
            "page_asset_sha256": str(page_asset_sha256 or ""),
        }
        candidates = [
            item
            for item in plan["required_page_checks"]
            if all(item.get(key) == value for key, value in requested.items())
        ]
        if len(candidates) != 1:
            raise ValueError("content_web_review_v2_unknown_required_page_asset")
        check = candidates[0]
        evidence = next(
            (
                target.get("evidence", {})
                for target in manifest.get("targets", [])
                if target.get("plan_item_id") == check["plan_item_id"]
                and target.get("evidence", {}).get("evidence_ref_id") == check["evidence_ref_id"]
            ),
            None,
        )
        expected = {
            "source_paper_id": check["source_paper_id"],
            "source_pdf_sha256": check["source_pdf_sha256"],
            "page": check["page"],
            "page_asset_ref": check["page_asset_ref"],
            "page_asset_sha256": check["page_asset_sha256"],
        }
        if evidence is None or any(evidence.get(key) != value for key, value in expected.items()):
            raise ValueError("content_web_review_v2_page_asset_contract_mismatch")
        content = self._asset_bytes_for_evidence(evidence)
        if content is None or hashlib.sha256(content).hexdigest() != check["page_asset_sha256"]:
            raise ValueError("content_web_review_v2_page_asset_changed_or_unavailable")
        return {
            **expected,
            "mime_type": self._page_asset_mime_type(check["page_asset_ref"], content),
            "byte_count": len(content),
            "content": content,
        }

    @staticmethod
    def _page_asset_mime_type(page_asset_ref: str, content: bytes) -> str:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        suffix = Path(page_asset_ref).suffix.lower()
        return {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")

    def _build_manifest(self, paper: Paper, *, selected_modules: list[str]) -> dict[str, Any]:
        pdf = self._pdf_descriptor(paper)
        source_pdfs = self._source_pdfs(paper, pdf)
        targets = self._targets(paper, pdf, selected_modules=selected_modules)
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
            "selected_modules": selected_modules,
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

    def _targets(self, paper: Paper, pdf: dict[str, Any], *, selected_modules: list[str]) -> list[dict[str, Any]]:
        raw: list[tuple[str, str, str, Any, str | None, int | None, str | None]] = []
        if "abstract" in selected_modules and paper.abstract:
            raw.append(("paper_abstract", str(paper.id), "abstract", paper.abstract, paper.abstract, None, "abstract"))
        if "sections" in selected_modules:
            for section in self.session.scalars(select(PaperSection).where(PaperSection.paper_id == paper.id).order_by(PaperSection.id)).all():
                raw.append(("paper_section", str(section.id), "text", section.text, section.text, section.page_start, section.section_title))
        if "mechanism_knowledge" in selected_modules:
            for claim in self.session.scalars(select(MechanismClaim).where(MechanismClaim.paper_id == paper.id).order_by(MechanismClaim.id)).all():
                raw.append(("mechanism_claim", str(claim.id), "claim_text", claim.claim_text, claim.evidence_text or claim.claim_text, None, claim.claim_type))
        card_fields = ("research_gap", "proposed_solution", "core_hypothesis", "figure_logic", "abstract_logic", "introduction_logic", "discussion_logic")
        if "writing_cards" in selected_modules:
            for card in self.session.scalars(select(WritingCard).where(WritingCard.paper_id == paper.id).order_by(WritingCard.id)).all():
                for field_name in card_fields:
                    value = getattr(card, field_name)
                    if value:
                        raw.append(("writing_card", str(card.id), field_name, value, value, None, field_name))
        targets: list[dict[str, Any]] = []
        for target_type, target_id, field_name, value, fallback_excerpt, fallback_page, label in raw:
            locator = self._locator_for(paper.id, target_type, target_id, field_name)
            excerpt = locator.evidence_text if locator is not None else fallback_excerpt
            page = locator.page if locator is not None and locator.page is not None else fallback_page
            object_hash = self._hash({"target_type": target_type, "target_id": target_id, "field_name": field_name, "value": value})
            plan_item_id = str(uuid5(PLAN_ITEM_NAMESPACE, f"{paper.id}|{target_type}|{target_id}|{field_name}"))
            evidence_id = f"evidence:{plan_item_id}"
            page_asset = self._page_asset(pdf, page, locator)
            blockers = list(pdf["gate_blockers"])
            if page is None:
                blockers.append("page_unlocated")
            if not str(excerpt or "").strip():
                blockers.append("evidence_excerpt_missing")
            formal_gate = self._formal_gate_snapshot(paper.id, target_type, target_id)
            qualification = bool(formal_gate["can_use_for_writing"] or formal_gate["can_use_for_citation"])
            evidence = {
                "evidence_ref_id": evidence_id,
                "source_paper_id": str(paper.id),
                "source_pdf_sha256": pdf["sha256"],
                "page": page,
                "bbox": self._safe_bbox(locator.bbox) if locator is not None else None,
                "locator_id": str(locator.id) if locator is not None else None,
                "locator_status": locator.locator_status if locator is not None else "not_found",
                "evidence_source": "evidence_locator" if locator is not None else "object_field_fallback",
                **page_asset,
                "evidence_excerpt": str(excerpt or ""),
                "evidence_asset_sha256": self._hash({"excerpt": excerpt, "page_asset_sha256": page_asset["page_asset_sha256"]}),
            }
            targets.append({
                "plan_item_id": plan_item_id, "target_type": target_type, "target_id": target_id,
                "field_name": field_name, "current_value": value, "object_snapshot_hash": object_hash,
                "target_label": label, "gate_blockers": sorted(set(blockers)),
                "existing_formal_qualification": qualification, "formal_gate_snapshot": formal_gate,
                "evidence": evidence,
            })
        return targets

    def _validate_proposal(self, bundle: ContentWebReviewBundleV2, proposal: dict[str, Any]) -> list[str]:
        manifest = bundle.manifest or {}
        errors: list[str] = []
        allowed_top_level = {
            "schema_version", "bundle_fingerprint", "paper_id", "paper_code", "proposal_status",
            "source_identity_verified", "writes_final_truth", "local_ai_verification", "actions", "discovery_proposals",
        }
        if set(proposal) - allowed_top_level:
            errors.append("proposal_additional_properties_forbidden")
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
            allowed_action = {
                "plan_item_id", "target_type", "target_id", "field_name", "object_snapshot_hash", "decision",
                "evidence_ref_ids", "evidence_quote", "evidence_asset_sha256", "page", "proposed_value", "verification_note",
            }
            if set(action) - allowed_action:
                errors.append("action_additional_properties_forbidden")
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
            elif action["decision"] == "REVISE" and action.get("proposed_value") is None:
                errors.append(f"revise_requires_proposed_value:{plan_item_id}")
            elif action["decision"] != "REVISE" and action.get("proposed_value") is not None:
                errors.append(f"non_revise_proposed_value_must_be_null:{plan_item_id}")
            if "proposed_value" not in action:
                errors.append(f"proposed_value_required:{plan_item_id}")
            if "verification_note" in action and action["verification_note"] is not None and not isinstance(action["verification_note"], str):
                errors.append(f"invalid_verification_note:{plan_item_id}")
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
                if (
                    not isinstance(proposal_item, dict)
                    or set(proposal_item) != {"summary", "target_id"}
                    or not str(proposal_item.get("summary") or "").strip()
                    or proposal_item.get("target_id") is not None
                ):
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
                "formal_gate_snapshot": target["formal_gate_snapshot"],
                "source_paper_id": evidence["source_paper_id"], "source_pdf_sha256": evidence["source_pdf_sha256"],
                "evidence_ref_id": evidence["evidence_ref_id"],
                "page": evidence["page"], "page_asset_sha256": evidence["page_asset_sha256"],
                "evidence_asset_sha256": evidence["evidence_asset_sha256"], "evidence_excerpt": evidence["evidence_excerpt"],
                "page_asset_ref": evidence["page_asset_ref"], "page_asset_status": evidence["page_asset_status"],
                "page_asset_origin": evidence["page_asset_origin"],
                "bbox": evidence["bbox"], "locator_id": evidence["locator_id"], "locator_status": evidence["locator_status"],
                "requires_page_render": self._requires_page_render(target, evidence),
                "layout_consistency_status": evidence["layout_consistency_status"],
                "proposed_value": action.get("proposed_value"), "verification_note": action.get("verification_note"),
                "gate_blockers": target["gate_blockers"],
            })
        # Keep every target's evidence/page contract at the object layer; the
        # following evidence/page lists are the deduplicated execution view.
        object_checks = list(required)
        evidence_checks = self._dedupe(required, ("evidence_ref_id", "evidence_asset_sha256"))
        page_checks = self._dedupe(
            [item for item in evidence_checks if item["page"] is not None and item["requires_page_render"]],
            ("source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"),
        )
        optional_page_refs = self._dedupe(
            [item for item in evidence_checks if item["page"] is not None and not item["requires_page_render"]],
            ("source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"),
        )
        batches: dict[tuple[Any, Any, Any, Any], list[dict[str, Any]]] = {}
        for item in required:
            if item["page"] is None or not item["requires_page_render"]:
                continue
            batches.setdefault(
                (item["source_paper_id"], item["source_pdf_sha256"], item["page"], item["page_asset_sha256"]),
                [],
            ).append(item)
        web_count = len(manifest.get("targets", []))
        unresolved = [item for item in required if item["page"] is None and item["requires_page_render"]]
        page_batches = []
        for (source_paper_id, source_pdf_sha256, page, page_asset_sha256), checks in sorted(
            batches.items(), key=lambda item: tuple(str(value) for value in item[0])
        ):
            page_batches.append({
                "source_paper_id": source_paper_id, "source_pdf_sha256": source_pdf_sha256, "page": page,
                "page_asset_sha256": page_asset_sha256,
                "page_asset_ref": checks[0]["page_asset_ref"], "page_asset_status": checks[0]["page_asset_status"],
                "target_count": len(checks), "plan_item_ids": [check["plan_item_id"] for check in checks], "checks": checks,
            })
        return {
            "bundle_id": str(bundle.id), "status": bundle.status, "proposal_only": True,
            "web_reviewed_target_count": web_count, "local_required_target_count": len(required),
            "local_skipped_target_count": sum(skipped.values()), "local_skipped_target_count_by_reason": skipped,
            "required_object_checks": object_checks, "required_evidence_checks": evidence_checks,
            "required_page_checks": page_checks, "unique_page_checks": page_checks,
            "optional_page_refs": optional_page_refs,
            "unique_page_count": len(page_checks), "page_batches": page_batches,
            "unresolved_page_target_count": len(unresolved), "unresolved_page_blockers": unresolved,
            "local_ai_instruction": self._local_ai_instruction(bundle.id),
            "metrics": {
                "logical_page_read_count": len(page_checks), "physical_page_read_attempt_count": 0,
                "page_read_retry_count": 0, "page_cache_hit_count": 0,
                "unresolved_page_target_count": len(unresolved),
                "physical_counter_note": "Plan generation reports logical work only; no page reader was invoked.",
            },
            "writes_final_truth": False, "local_ai_verification": None,
        }

    @staticmethod
    def _requires_page_render(target: dict[str, Any], evidence: dict[str, Any]) -> bool:
        layout_status = str(evidence.get("layout_consistency_status") or "")
        asset_status = str(evidence.get("page_asset_status") or "")
        if evidence.get("page") is None or layout_status in {"page_unlocated", "page_not_materialized"}:
            return True
        if asset_status != "materialized" or layout_status not in {
            "verified",
            "consistent",
            "verified_consistent",
        }:
            return True
        if target["target_type"] in {"mechanism_claim", "writing_card"}:
            return True
        content = " ".join(str(value or "") for value in (target.get("current_value"), evidence.get("evidence_excerpt")))
        return bool(re.search(
            r"\d|(?:\beV\b|\bV\b|\bmA\b|\bA\b|%|\bcm\b|\bnm\b|μ|µ|\bmol\b|"
            r"\bTable\b|\bFig(?:ure)?\b|\bequation\b|\bformula\b)",
            content,
            re.IGNORECASE,
        ))

    def _stale_report(self, bundle: ContentWebReviewBundleV2) -> dict[str, Any]:
        paper = self._paper(bundle.paper_id)
        previous = bundle.manifest or {}
        current = self._build_manifest(paper, selected_modules=list(previous.get("selected_modules") or []))
        previous_targets = {item["plan_item_id"]: item for item in previous.get("targets", [])}
        current_targets = {item["plan_item_id"]: item for item in current.get("targets", [])}
        applied_results = list(self.session.scalars(
            select(ContentWebReviewLocalVerificationResult).where(
                ContentWebReviewLocalVerificationResult.bundle_id == bundle.id,
                ContentWebReviewLocalVerificationResult.status == "applied",
            ).order_by(ContentWebReviewLocalVerificationResult.applied_at)
        ).all())
        result_by_plan = {str(item.plan_item_id): item for item in applied_results}
        latest_gate_by_object: dict[tuple[str, str], dict[str, Any]] = {}
        for result in applied_results:
            if result.formal_gate_after:
                latest_gate_by_object[(result.target_type, result.target_id)] = result.formal_gate_after
        dependency_details: dict[str, set[str]] = {
            key: {"target"} for key in sorted(set(previous_targets) ^ set(current_targets))
        }
        for key in sorted(set(previous_targets) & set(current_targets)):
            before, after = previous_targets[key], current_targets[key]
            applied = result_by_plan.get(key)
            expected_object_hash = (
                applied.applied_object_snapshot_hash
                if applied is not None and applied.applied_object_snapshot_hash
                else before["object_snapshot_hash"]
            )
            expected_gate = latest_gate_by_object.get(
                (before["target_type"], before["target_id"]),
                before.get("formal_gate_snapshot"),
            )
            target_changed = expected_object_hash != after["object_snapshot_hash"]
            if target_changed:
                dependency_details.setdefault(key, set()).add("target")
            else:
                if before["evidence"]["evidence_asset_sha256"] != after["evidence"]["evidence_asset_sha256"]:
                    dependency_details.setdefault(key, set()).add("evidence_ref")
                if before["evidence"]["page_asset_sha256"] != after["evidence"]["page_asset_sha256"]:
                    dependency_details.setdefault(key, set()).add("page_asset")
                if expected_gate != after.get("formal_gate_snapshot"):
                    dependency_details.setdefault(key, set()).add("review_gate")
        previous_pdfs = {item.get("paper_id"): item for item in previous.get("source_pdfs", [])}
        current_pdfs = {item.get("paper_id"): item for item in current.get("source_pdfs", [])}
        changed_pdf_ids = {
            paper_id for paper_id in set(previous_pdfs) | set(current_pdfs)
            if previous_pdfs.get(paper_id) != current_pdfs.get(paper_id)
        }
        for key in sorted(set(previous_targets) | set(current_targets)):
            source_ids = {
                target.get("evidence", {}).get("source_paper_id")
                for target in (previous_targets.get(key), current_targets.get(key))
                if target is not None
            }
            if source_ids & changed_pdf_ids:
                details = dependency_details.setdefault(key, set())
                # Evidence and page hashes normally derive from the changed
                # source PDF. Suppress those child reasons only for this
                # dependent target, never for unrelated targets in the bundle.
                details.difference_update({"evidence_ref", "page_asset"})
                details.add("source_pdf")
        if previous.get("policy_version") != current.get("policy_version"):
            for key in sorted(set(previous_targets) | set(current_targets)):
                dependency_details.setdefault(key, set()).add("policy_version")
        dependency_details = {key: value for key, value in dependency_details.items() if value}
        changed = sorted({reason for reasons in dependency_details.values() for reason in reasons})
        return {
            "is_stale": bool(changed),
            "changed_dependencies": changed,
            "affected_plan_item_ids": sorted(dependency_details),
            "dependency_details": {key: sorted(value) for key, value in sorted(dependency_details.items())},
            "current_fingerprint": current["bundle_fingerprint"],
            "dependency_graph": "target -> evidence_ref -> page_asset -> source_pdf -> policy_version",
        }

    def _existing_formal_qualification(self, paper_id: UUID, target_type: str, target_id: str) -> bool:
        gate = self._formal_gate_snapshot(paper_id, target_type, target_id)
        return bool(gate["can_use_for_writing"] or gate["can_use_for_citation"])

    def _formal_gate_snapshot(self, paper_id: UUID, target_type: str, target_id: str) -> dict[str, Any]:
        canonical, target = self._resolve_formal_target(paper_id, target_type, target_id)
        if target is None:
            return {
                "policy_version": CONTENT_OBJECT_GATE_POLICY_VERSION,
                "can_use_for_writing": False,
                "can_use_for_citation": False,
                "review_gate_status": "blocked",
                "locator_status": "unmapped",
                "blocked_reasons": ["no_real_object_mapping"],
            }
        gate = content_object_gate(self.session, canonical, target)
        return {
            "policy_version": CONTENT_OBJECT_GATE_POLICY_VERSION,
            "can_use_for_writing": bool(gate.can_use_for_writing),
            "can_use_for_citation": bool(gate.can_use_for_citation),
            "review_gate_status": gate.review_gate_status,
            "locator_status": gate.locator_status,
            "blocked_reasons": list(gate.blocked_reasons),
        }

    def _resolve_formal_target(self, paper_id: UUID, target_type: str, target_id: str) -> tuple[str, Any | None]:
        if target_type == "paper_abstract":
            target = self.session.get(Paper, paper_id)
            return "abstract", target if target is not None and str(target.id) == str(target_id) else None
        mapping = {
            "paper_section": ("sections", PaperSection),
            "mechanism_claim": ("mechanism_claims", MechanismClaim),
            "writing_card": ("writing_cards", WritingCard),
        }
        spec = mapping.get(target_type)
        if spec is None:
            return target_type, None
        canonical, model = spec
        try:
            target = self.session.get(model, UUID(str(target_id)))
        except (TypeError, ValueError):
            target = None
        if target is None or getattr(target, "paper_id", None) != paper_id:
            return canonical, None
        return canonical, target

    @staticmethod
    def _selected_modules(*, module: str | None, modules: list[str] | None) -> list[str]:
        requested: list[str] = []
        if module is not None:
            requested.append(str(module))
        if modules is not None:
            if not isinstance(modules, list):
                raise ValueError("content_web_review_v2_modules_must_be_list")
            requested.extend(str(value) for value in modules)
        if not requested or any(not value.strip() for value in requested):
            raise ValueError("content_web_review_v2_module_required")
        invalid = sorted(set(requested) - MODULES)
        if invalid:
            raise ValueError("content_web_review_v2_invalid_module:" + ",".join(invalid))
        return sorted(set(requested))

    def _locator_for(
        self, paper_id: UUID, target_type: str, target_id: str, field_name: str
    ) -> EvidenceLocator | None:
        aliases = TARGET_TYPE_ALIASES.get(target_type, {target_type})
        rows = list(self.session.scalars(
            select(EvidenceLocator).where(
                EvidenceLocator.paper_id == paper_id,
                EvidenceLocator.target_id == target_id,
                EvidenceLocator.target_type.in_(aliases),
            )
        ).all())
        compatible = [row for row in rows if row.field_name in {field_name, None, ""}]
        if not compatible:
            return None
        return sorted(
            compatible,
            key=lambda row: (
                row.field_name != field_name,
                row.page is None,
                row.locator_status in {"missing", "invalid"},
                -float(row.locator_confidence or 0),
                str(row.id),
            ),
        )[0]

    def _page_asset(
        self, pdf: dict[str, Any], page: int | None, locator: EvidenceLocator | None
    ) -> dict[str, Any]:
        if page is None:
            return {
                "page_asset_sha256": self._hash({"render_source_pdf_sha256": pdf["sha256"], "page": None}),
                "page_asset_ref": f"render-source:{pdf['sha256'] or 'missing'}#page=unlocated",
                "page_asset_status": "not_materialized", "page_asset_origin": "page_unlocated",
                "layout_consistency_status": "page_unlocated",
            }
        bbox = locator.bbox if locator is not None and isinstance(locator.bbox, dict) else {}
        stored_path = next(
            (str(bbox[key]) for key in ("page_asset_path", "full_page_image_path", "page_image_path", "image_path") if bbox.get(key)),
            None,
        )
        asset = resolve_persisted_artifact_path(
            stored_path, category="figures", settings=self.settings, trusted_persisted_reference=True
        ) if stored_path else None
        if asset is not None and asset.is_file():
            content = asset.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            bundle_ref = f"evidence/pages/{digest}{asset.suffix.lower() or '.png'}"
            self._page_asset_bytes[bundle_ref] = content
            layout_status = self._trusted_layout_consistency_status(locator, bbox)
            return {
                "page_asset_sha256": digest, "page_asset_ref": bundle_ref,
                "page_asset_status": "materialized", "page_asset_origin": "existing_preview",
                "layout_consistency_status": layout_status,
            }
        rendered = self._render_selected_page(pdf, page)
        if rendered is not None:
            digest = hashlib.sha256(rendered).hexdigest()
            bundle_ref = f"evidence/pages/{digest}.png"
            self._page_asset_bytes[bundle_ref] = rendered
            return {
                "page_asset_sha256": digest, "page_asset_ref": bundle_ref,
                "page_asset_status": "rendered_for_bundle", "page_asset_origin": "selected_pdf_page_render",
                "layout_consistency_status": "rendered_page_unchecked",
            }
        return {
            "page_asset_sha256": self._hash({"render_source_pdf_sha256": pdf["sha256"], "page": page}),
            "page_asset_ref": f"render-source:{pdf['sha256'] or 'missing'}#page={page if page is not None else 'unlocated'}",
            "page_asset_status": "not_materialized", "page_asset_origin": "render_failed",
            "layout_consistency_status": "page_unlocated" if page is None else "page_not_materialized",
        }

    @staticmethod
    def _trusted_layout_consistency_status(locator: EvidenceLocator | None, bbox: dict[str, Any]) -> str:
        claimed = str(bbox.get("layout_consistency_status") or "").strip().lower()
        trusted = bool(
            locator is not None
            and str(locator.parser_source or "").strip().lower() in TRUSTED_LAYOUT_VERIFIER_SOURCES
            and str(locator.source_type or "").strip().lower() == "pdf"
            and str(locator.locator_status or "").strip().lower() in {"exact_page", "exact_bbox"}
            and float(locator.locator_confidence or 0.0) >= TRUSTED_LAYOUT_MIN_CONFIDENCE
            and claimed in TRUSTED_LAYOUT_STATUSES
        )
        return claimed if trusted else "asset_available_unchecked"

    def _render_selected_page(self, pdf: dict[str, Any], page: int) -> bytes | None:
        cache_key = f"render:{pdf.get('sha256')}:{page}"
        if cache_key in self._page_asset_bytes:
            return self._page_asset_bytes[cache_key]
        local_path = pdf.get("_local_path")
        if not local_path:
            return None
        try:
            import fitz

            document = fitz.open(str(local_path))
            try:
                if page < 1 or page > document.page_count:
                    return None
                image = document.load_page(page - 1).get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                content = image.tobytes("png")
            finally:
                document.close()
        except Exception:
            return None
        self._page_asset_bytes[cache_key] = content
        return content

    @staticmethod
    def _safe_bbox(bbox: dict | None) -> dict | None:
        if not isinstance(bbox, dict):
            return None
        path_keys = {"page_asset_path", "full_page_image_path", "page_image_path", "image_path", "path"}
        return {key: value for key, value in bbox.items() if key not in path_keys}

    def _pdf_descriptor(self, paper: Paper) -> dict[str, Any]:
        path = resolve_paper_pdf_path(paper.pdf_path, self.settings.storage_root)
        if path is None:
            return {"paper_id": str(paper.id), "source_pdf_ref": f"source/{paper.paper_code or paper.id}.pdf", "sha256": None, "gate_blockers": ["source_pdf_missing"]}
        return {"paper_id": str(paper.id), "source_pdf_ref": f"source/{paper.paper_code or paper.id}.pdf", "sha256": self._file_hash(path), "gate_blockers": [], "_local_path": str(path)}

    def _source_pdfs(self, paper: Paper, main_pdf: dict[str, Any]) -> list[dict[str, Any]]:
        descriptors = [{key: value for key, value in main_pdf.items() if key != "_local_path"} | {"source_document_type": "main"}]
        relationship_types = {"supplementary", "supplementary_information", "supporting_information", "si"}
        links = self.session.scalars(select(PaperRelationship).where(PaperRelationship.source_paper_id == paper.id)).all()
        linked_ids = sorted({link.target_paper_id for link in links if str(link.relationship_type or "").lower() in relationship_types}, key=str)
        for linked_id in linked_ids:
            linked = self.session.get(Paper, linked_id)
            if linked is not None:
                descriptor = self._pdf_descriptor(linked)
                descriptors.append({key: value for key, value in descriptor.items() if key != "_local_path"} | {"source_document_type": "supplementary"})
        return descriptors

    def _bundle_response(self, bundle: ContentWebReviewBundleV2) -> dict[str, Any]:
        manifest = bundle.manifest or {}
        return {"bundle_id": str(bundle.id), "status": bundle.status, "bundle_fingerprint": bundle.snapshot_fingerprint, "manifest": manifest,
                "download_url": f"/api/content-knowledge/review-bundles/{bundle.id}/download", "proposal_only": True,
                "object_count": len(manifest.get("targets", [])),
                "unique_evidence_page_count": len(manifest.get("allowed_pages", [])),
                "web_ai_instruction": manifest.get("instructions"),
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
            written_assets: set[str] = set()
            for evidence in manifest["allowed_evidence_refs"]:
                bundle_ref = str(evidence.get("page_asset_ref") or "")
                if not bundle_ref.startswith("evidence/pages/") or bundle_ref in written_assets:
                    continue
                content = self._asset_bytes_for_evidence(evidence)
                if content is None or hashlib.sha256(content).hexdigest() != evidence.get("page_asset_sha256"):
                    raise ValueError("content_web_review_v2_page_asset_changed_or_unavailable")
                archive.writestr(bundle_ref, content)
                written_assets.add(bundle_ref)
        return buffer.getvalue()

    def _asset_bytes_for_evidence(self, evidence: dict[str, Any]) -> bytes | None:
        bundle_ref = str(evidence.get("page_asset_ref") or "")
        cached = self._page_asset_bytes.get(bundle_ref)
        if cached is not None:
            return cached
        origin = evidence.get("page_asset_origin")
        if origin == "existing_preview":
            locator_id = evidence.get("locator_id")
            locator = self.session.get(EvidenceLocator, UUID(str(locator_id))) if locator_id else None
            bbox = locator.bbox if locator is not None and isinstance(locator.bbox, dict) else {}
            stored_path = next(
                (str(bbox[key]) for key in ("page_asset_path", "full_page_image_path", "page_image_path", "image_path") if bbox.get(key)),
                None,
            )
            asset = resolve_persisted_artifact_path(
                stored_path, category="figures", settings=self.settings, trusted_persisted_reference=True
            ) if stored_path else None
            content = asset.read_bytes() if asset is not None and asset.is_file() else None
        elif origin == "selected_pdf_page_render":
            paper = self._paper(UUID(str(evidence["source_paper_id"])))
            content = self._render_selected_page(self._pdf_descriptor(paper), int(evidence["page"]))
        else:
            content = None
        if content is not None:
            self._page_asset_bytes[bundle_ref] = content
        return content

    @staticmethod
    def _return_schema() -> dict[str, Any]:
        action_properties = {
            "plan_item_id": {"type": "string", "format": "uuid"},
            "target_type": {"type": "string"}, "target_id": {"type": "string"},
            "field_name": {"type": "string"}, "object_snapshot_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "decision": {"enum": sorted(DECISIONS)}, "evidence_ref_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "evidence_quote": {"type": "string", "minLength": 1}, "evidence_asset_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "page": {"type": ["integer", "null"]}, "proposed_value": {}, "verification_note": {"type": ["string", "null"]},
        }
        action = {
            "type": "object", "additionalProperties": False, "properties": action_properties,
            "required": list(action_properties),
            "allOf": [
                {
                    "if": {"properties": {"decision": {"const": "REVISE"}}},
                    "then": {"properties": {"proposed_value": {"not": {"type": "null"}}}},
                },
                {
                    "if": {"properties": {"decision": {"not": {"const": "REVISE"}}}},
                    "then": {"properties": {"proposed_value": {"type": "null"}}},
                },
            ],
        }
        discovery = {"type": "object", "additionalProperties": False, "properties": {"summary": {"type": "string", "minLength": 1}, "target_id": {"type": "null"}}, "required": ["summary", "target_id"]}
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": RESULT_SCHEMA,
            "type": "object", "additionalProperties": False,
            "properties": {
                "schema_version": {"const": RESULT_SCHEMA}, "bundle_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "paper_id": {"type": "string", "format": "uuid"}, "paper_code": {"type": ["string", "null"]},
                "proposal_status": {"const": "web_ai_proposal"}, "source_identity_verified": {"const": False},
                "writes_final_truth": {"const": False}, "local_ai_verification": {"type": "null"},
                "actions": {"type": "array", "minItems": 1, "items": action}, "discovery_proposals": {"type": "array", "items": discovery},
            },
            "required": ["schema_version", "bundle_fingerprint", "paper_id", "paper_code", "proposal_status", "source_identity_verified", "writes_final_truth", "local_ai_verification", "actions", "discovery_proposals"],
        }

    @staticmethod
    def _return_template(paper: Paper, fingerprint: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id), "paper_code": paper.paper_code, "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": [], "discovery_proposals": []}

    @staticmethod
    def _format_examples(paper: Paper, fingerprint: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        if not targets:
            return {"actions": []}
        target = targets[0]; evidence = target["evidence"]
        return {"schema_version": RESULT_SCHEMA, "bundle_fingerprint": fingerprint, "paper_id": str(paper.id), "paper_code": paper.paper_code, "proposal_status": "web_ai_proposal", "source_identity_verified": False, "writes_final_truth": False, "local_ai_verification": None, "actions": [{"plan_item_id": target["plan_item_id"], "target_type": target["target_type"], "target_id": target["target_id"], "field_name": target["field_name"], "object_snapshot_hash": target["object_snapshot_hash"], "decision": "NEEDS_HUMAN", "evidence_ref_ids": [evidence["evidence_ref_id"]], "evidence_quote": evidence["evidence_excerpt"], "evidence_asset_sha256": evidence["evidence_asset_sha256"], "page": evidence["page"], "proposed_value": None, "verification_note": None}], "discovery_proposals": []}

    @staticmethod
    def _local_requirements() -> dict[str, Any]:
        return {"proposal_only": True, "required_result_fields": ["required_object_checks", "required_evidence_checks", "required_page_checks"], "page_dedupe_key": ["source_paper_id", "source_pdf_sha256", "page", "page_asset_sha256"], "no_apply_endpoint": True}

    @staticmethod
    def _instructions() -> str:
        return "Return only the v2 proposal JSON. Cover every supplied plan item exactly once. Use only supplied evidence_ref_ids, quotes, hashes and page numbers. Do not claim an identity, local verification, final truth, or create a target_id in discovery proposals. This package cannot apply any review result."

    @staticmethod
    def _local_ai_instruction(bundle_id: UUID) -> str:
        return (
            f"For bundle_id={bundle_id}, first call get_content_web_review_local_verification_plan. Then call "
            "read_content_web_review_page_asset once for each required_page_check, using its complete page identity "
            "(source_paper_id, source_pdf_sha256, page, page_asset_sha256, page_asset_ref). Do not read the whole "
            "web package or full paper; deduplicate identical required pages and read each once. Return one result "
            "per required object through apply_content_web_review_local_verification only after evidence checks. "
            "Perform unlocked reads first and keep any write lock short. Unresolved page targets remain blockers "
            "and must not be applied."
        )

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

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, CatalystSample, DFTResult


class DFTMaterialBindingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_or_create_sample(
        self,
        *,
        paper_id: UUID,
        material_identity: Any,
    ) -> tuple[CatalystSample, bool]:
        name = self._first_text(material_identity)
        if not name:
            raise ValueError("material_identity is required.")
        samples = self.session.scalars(
            select(CatalystSample)
            .where(CatalystSample.paper_id == paper_id)
            .order_by(CatalystSample.id.asc())
        ).all()
        normalized_name = name.casefold()
        for sample in samples:
            if str(sample.name or "").strip().casefold() == normalized_name:
                return sample, False
        sample = CatalystSample(
            paper_id=paper_id,
            name=name,
            catalyst_type="unknown",
        )
        self.session.add(sample)
        self.session.flush()
        return sample, True

    def ensure_row_binding(
        self,
        *,
        row: DFTResult,
        material_identity: Any = None,
    ) -> dict[str, Any]:
        if row.catalyst_sample_id:
            return {
                "status": "already_bound",
                "catalyst_sample_id": str(row.catalyst_sample_id),
                "sample_created": False,
            }
        name = self._first_text(material_identity, self.material_identity_for_row(row))
        if not name:
            return {
                "status": "missing_material_identity",
                "catalyst_sample_id": None,
                "sample_created": False,
            }
        sample, created = self.resolve_or_create_sample(
            paper_id=row.paper_id,
            material_identity=name,
        )
        row.catalyst_sample_id = sample.id
        self.session.add(row)
        return {
            "status": "bound",
            "material_identity": name,
            "catalyst_sample_id": str(sample.id),
            "sample_created": created,
        }

    def backfill_paper(
        self,
        *,
        paper_id: UUID,
        actor: str,
    ) -> dict[str, Any]:
        rows = self.session.scalars(
            select(DFTResult)
            .where(
                DFTResult.paper_id == paper_id,
                DFTResult.catalyst_sample_id.is_(None),
            )
            .order_by(DFTResult.id.asc())
        ).all()
        bound_items: list[dict[str, Any]] = []
        skipped_items: list[dict[str, Any]] = []
        created_sample_ids: set[str] = set()
        for row in rows:
            result = self.ensure_row_binding(row=row)
            item = {"dft_result_id": str(row.id), **result}
            if result["status"] == "bound":
                bound_items.append(item)
                if result["sample_created"]:
                    created_sample_ids.add(str(result["catalyst_sample_id"]))
            else:
                skipped_items.append(item)
        if bound_items:
            self.session.add(
                AuditLog(
                    paper_id=paper_id,
                    action="backfill_dft_catalyst_bindings",
                    source=str(actor or "system")[:160],
                    target_type="paper",
                    target_id=str(paper_id),
                    payload={
                        "bound_count": len(bound_items),
                        "skipped_count": len(skipped_items),
                        "created_sample_count": len(created_sample_ids),
                        "policy": "Bind unbound DFT rows by explicit evidence material_identity within one paper.",
                    },
                )
            )
        self.session.flush()
        return {
            "paper_id": str(paper_id),
            "bound_count": len(bound_items),
            "skipped_count": len(skipped_items),
            "created_sample_count": len(created_sample_ids),
            "bound_items": bound_items,
            "skipped_items": skipped_items,
        }

    @classmethod
    def material_identity_for_row(cls, row: DFTResult) -> str | None:
        evidence = row.evidence_payload if isinstance(row.evidence_payload, dict) else {}
        corrected = evidence.get("corrected_value") if isinstance(evidence.get("corrected_value"), dict) else {}
        return cls._first_text(
            evidence.get("material_identity"),
            evidence.get("material"),
            evidence.get("catalyst"),
            corrected.get("material_identity"),
            corrected.get("material"),
            corrected.get("catalyst"),
        )

    @staticmethod
    def _first_text(*values: Any) -> str | None:
        for value in values:
            if value in (None, "", []):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ActiveSiteMetal, CatalystSample, Paper
from app.domain.catalyst_basic_info import normalize_catalyst_type, normalize_metal_centers
from app.domain.element_descriptors import element_descriptor


@dataclass(frozen=True)
class ActiveSiteCandidate:
    sample: CatalystSample
    catalyst_type: str
    metals: list[str]


class ActiveSiteEnrichmentService:
    """System enrichment for confirmed SAC/DAC active-site metal identities.

    This service intentionally refuses to create M1/M2 rows for screening sets
    or multi-metal collections. Those records need human/material-level cleanup
    before they can safely enter descriptor generation.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def refresh_sample(self, sample: CatalystSample, *, dry_run: bool = False) -> dict[str, Any]:
        """Rebuild structured active-site metal rows for one catalyst sample.

        A frontend/user edit is the authoritative catalyst identity update.
        Therefore stale M1/M2 rows must be removed first; if the edited sample
        is still not a confirmed SAC/DAC, the old rows stay deleted and the
        skip reason explains why.
        """
        existing = self.session.scalars(
            select(ActiveSiteMetal).where(ActiveSiteMetal.catalyst_sample_id == sample.id)
        ).all()
        deleted_count = len(existing)
        if not dry_run:
            for row in existing:
                self.session.delete(row)
            self.session.flush()

        candidate, reason = self._candidate(sample)
        if candidate is None:
            return {
                "sample_id": str(sample.id),
                "active_site_status": "skipped",
                "inserted_count": 0,
                "deleted_count": deleted_count,
                "skipped_reason": reason,
                "enrichment_status": "system_enriched",
                "does_not_mark_verified": True,
                "dry_run": dry_run,
            }

        inserted_count = self._insert_candidate(candidate, dry_run=dry_run)
        if not dry_run:
            self.session.flush()
        return {
            "sample_id": str(sample.id),
            "active_site_status": "refreshed",
            "inserted_count": inserted_count,
            "deleted_count": deleted_count,
            "skipped_reason": None,
            "site_type": candidate.catalyst_type,
            "metals": candidate.metals,
            "enrichment_status": "system_enriched",
            "does_not_mark_verified": True,
            "dry_run": dry_run,
        }

    def backfill_confirmed_sites(self, *, library_name: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        stmt = select(CatalystSample)
        if library_name:
            stmt = stmt.join(Paper, Paper.id == CatalystSample.paper_id).where(Paper.library_name == library_name)
        samples = list(self.session.scalars(stmt).all())
        inserted = 0
        updated = 0
        skipped: dict[str, int] = {}
        for sample in samples:
            candidate, reason = self._candidate(sample)
            if candidate is None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            result = self._upsert_candidate(candidate, dry_run=dry_run)
            inserted += result["inserted_count"]
            updated += result["updated_count"]
        if not dry_run:
            self.session.flush()
        return {
            "sample_count": len(samples),
            "inserted_count": inserted,
            "updated_count": updated,
            "skipped": dict(sorted(skipped.items())),
            "dry_run": dry_run,
        }

    def _candidate(self, sample: CatalystSample) -> tuple[ActiveSiteCandidate | None, str]:
        catalyst_type, _ = normalize_catalyst_type(sample.catalyst_type)
        metals, _ = normalize_metal_centers(sample.metal_centers or [])
        if len(metals) > 2:
            return None, "screening_set_not_active_site"
        if catalyst_type == "single_atom" and len(metals) == 1:
            return ActiveSiteCandidate(sample=sample, catalyst_type=catalyst_type, metals=metals), ""
        if catalyst_type == "dual_atom" and len(metals) == 2:
            return ActiveSiteCandidate(sample=sample, catalyst_type=catalyst_type, metals=metals), ""
        if catalyst_type == "single_atom":
            return None, "single_atom_requires_exactly_one_metal"
        if catalyst_type == "dual_atom":
            return None, "dual_atom_requires_exactly_two_metals"
        return None, "unconfirmed_active_site_identity"

    def _insert_candidate(self, candidate: ActiveSiteCandidate, *, dry_run: bool) -> int:
        inserted = 0
        for payload in self._candidate_payloads(candidate):
            inserted += 1
            if not dry_run:
                self.session.add(ActiveSiteMetal(**payload))
        return inserted

    def _upsert_candidate(self, candidate: ActiveSiteCandidate, *, dry_run: bool) -> dict[str, int]:
        active_site_key = f"catalyst:{candidate.sample.id}|site:confirmed_active_center"
        existing = {
            row.site_role: row
            for row in self.session.scalars(
                select(ActiveSiteMetal).where(
                    ActiveSiteMetal.catalyst_sample_id == candidate.sample.id,
                    ActiveSiteMetal.active_site_key == active_site_key,
                )
            ).all()
        }
        inserted = 0
        updated = 0
        for payload in self._candidate_payloads(candidate):
            row = existing.get(payload["site_role"])
            if row is None:
                inserted += 1
                if not dry_run:
                    self.session.add(ActiveSiteMetal(**payload))
                continue
            changed = False
            for key, value in payload.items():
                if getattr(row, key) != value:
                    changed = True
                    if not dry_run:
                        setattr(row, key, value)
            updated += 1 if changed else 0
        return {"inserted_count": inserted, "updated_count": updated}

    def _candidate_payloads(self, candidate: ActiveSiteCandidate) -> list[dict[str, Any]]:
        sample = candidate.sample
        active_site_key = f"catalyst:{sample.id}|site:confirmed_active_center"
        pair_key = _normalized_pair_key(candidate.metals) if candidate.catalyst_type == "dual_atom" else None
        payloads: list[dict[str, Any]] = []
        for index, symbol in enumerate(candidate.metals, start=1):
            payloads.append(
                {
                    "paper_id": sample.paper_id,
                    "catalyst_sample_id": sample.id,
                    "active_site_key": active_site_key,
                    "site_type": candidate.catalyst_type,
                    "site_role": f"M{index}",
                    "element_symbol": symbol,
                    "element_order": index,
                    "order_source": "catalyst_sample.metal_centers",
                    "normalized_pair_key": pair_key,
                    "confidence": None,
                    "evidence_payload": {
                        "source": "system_enriched",
                        "does_not_mark_verified": True,
                    },
                    "enrichment_status": "system_enriched",
                }
            )
        return payloads


def _normalized_pair_key(metals: list[str]) -> str | None:
    if len(metals) != 2:
        return None
    ordered = sorted(
        metals,
        key=lambda symbol: (
            element_descriptor(symbol).get("atomic_number") is None,
            element_descriptor(symbol).get("atomic_number") or 999,
            symbol,
        ),
    )
    return "-".join(ordered)

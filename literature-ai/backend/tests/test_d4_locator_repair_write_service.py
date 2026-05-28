from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, EvidenceLocator, ExtractionFieldReview, Paper
from app.services.locator_repair_write_service import (
    PILOT_PAPER_ID,
    ControlledLocatorRepairWriteService,
    LocatorRepairWriteError,
    approved_items_from_plan,
)


APPROVED_IDS = {
    "09f83676-8f13-4e82-a576-ab359b264933",
    "280f2d9e-3ebb-4107-9702-f6ea6d645465",
    "56f72584-45b3-465b-9a40-97ec60a2fabf",
}


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'd4_3h1_write.db'}", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    return engine, SessionLocal


def _seed(session):
    pilot = Paper(id=UUID(PILOT_PAPER_ID), title="Pilot", pdf_path="pilot.pdf")
    other = Paper(title="Other", pdf_path="other.pdf")
    session.add_all([pilot, other])
    session.flush()
    rows = [
        ("e2c75b7f-2d9c-41ff-a6e1-e95e5d491896", "catalyst_samples", "sample-1", "name", "HAADF-STEM"),
        ("09f83676-8f13-4e82-a576-ab359b264933", "catalyst_samples", "sample-1", "catalyst_type", "HAADF-STEM"),
        ("280f2d9e-3ebb-4107-9702-f6ea6d645465", "catalyst_samples", "sample-1", "metal_centers", "HAADF-STEM"),
        ("56f72584-45b3-465b-9a40-97ec60a2fabf", "electrochemical_performance", "perf-1", "rate", "caption"),
        ("4ba0e490-5934-439c-8136-33a8ddf4e201", "dft_settings", "dft-1", "convergence_settings", "{}"),
    ]
    for review_id, target_type, target_id, field, evidence in rows:
        session.add(
            ExtractionFieldReview(
                id=UUID(review_id),
                paper_id=pilot.id,
                target_type=target_type,
                target_id=target_id,
                field_name=field,
                original_value=field,
                reviewed_value=None,
                evidence_text=evidence,
                reviewer_status="pending",
                target_resolution_status="active",
            )
        )
    session.add(
        ExtractionFieldReview(
            paper_id=other.id,
            target_type="catalyst_samples",
            target_id="other-sample",
            field_name="catalyst_type",
            original_value="single_atom",
            evidence_text="Other",
            reviewer_status="pending",
            target_resolution_status="active",
        )
    )
    session.commit()
    return pilot, other


def _plan():
    items = [
        {
            "review_id": "e2c75b7f-2d9c-41ff-a6e1-e95e5d491896",
            "paper_id": PILOT_PAPER_ID,
            "field": "name",
            "value": "Fe-Co-V",
            "proposed_page": 7,
            "proposed_bbox": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
            "confidence": 0.56,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
            "mark_verified": False,
            "verified": False,
        },
        {
            "review_id": "09f83676-8f13-4e82-a576-ab359b264933",
            "paper_id": PILOT_PAPER_ID,
            "field": "catalyst_type",
            "value": "single_atom",
            "proposed_page": 7,
            "proposed_bbox": {"x0": 53.858, "y0": 477.052, "x1": 287.155, "y1": 359.672},
            "confidence": 0.68,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
            "mark_verified": False,
            "verified": False,
        },
        {
            "review_id": "280f2d9e-3ebb-4107-9702-f6ea6d645465",
            "paper_id": PILOT_PAPER_ID,
            "field": "metal_centers",
            "value": ["Fe", "Co", "V"],
            "proposed_page": 7,
            "proposed_bbox": {"x0": 53.859, "y0": 356.594, "x1": 287.167, "y1": 70.085},
            "confidence": 0.68,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
            "mark_verified": False,
            "verified": False,
        },
        {
            "review_id": "56f72584-45b3-465b-9a40-97ec60a2fabf",
            "paper_id": PILOT_PAPER_ID,
            "field": "rate",
            "value": "0.2C",
            "proposed_page": 6,
            "proposed_bbox": {"x0": 53.858, "y0": 125.995, "x1": 541.43, "y1": 71.087},
            "confidence": 0.68,
            "safe_verified": False,
            "export_eligible": False,
            "writing_eligible": False,
            "mark_verified": False,
            "verified": False,
        },
    ]
    return {"items": items, "excluded_items": [{"field": "convergence_settings"}]}


def _manifest():
    return {
        "proposals": [
            {
                "review_id": "09f83676-8f13-4e82-a576-ab359b264933",
                "matched_text": "single-atom catalyst, SAC",
                "source_artifact": "Docling #/texts/79",
                "proposed_page": 7,
                "proposed_bbox": {"x0": 53.858, "y0": 477.052, "x1": 287.155, "y1": 359.672},
                "match_method": "substring_match",
                "confidence": 0.68,
            },
            {
                "review_id": "280f2d9e-3ebb-4107-9702-f6ea6d645465",
                "matched_text": "以铁(Fe)、钴(Co)、钒(V)为金属中心",
                "source_artifact": "Docling #/texts/80",
                "proposed_page": 7,
                "proposed_bbox": {"x0": 53.859, "y0": 356.594, "x1": 287.167, "y1": 70.085},
                "match_method": "substring_match",
                "confidence": 0.68,
            },
            {
                "review_id": "56f72584-45b3-465b-9a40-97ec60a2fabf",
                "matched_text": "Cycling performances at 0.2 C",
                "source_artifact": "Docling #/texts/74",
                "proposed_page": 6,
                "proposed_bbox": {"x0": 53.858, "y0": 125.995, "x1": 541.43, "y1": 71.087},
                "match_method": "substring_match",
                "confidence": 0.68,
            },
        ]
    }


def _approved_items():
    return approved_items_from_plan(
        disabled_plan=_plan(),
        proposal_manifest=_manifest(),
        approved_review_ids=APPROVED_IDS,
    )


def test_default_dry_run_does_not_write_db(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            result = ControlledLocatorRepairWriteService(session).apply(
                _approved_items(),
                approved_review_ids=APPROVED_IDS,
            )
            session.rollback()

            assert result.dry_run is True
            assert result.written_count == 0
            assert session.scalar(select(EvidenceLocator.id).limit(1)) is None
    finally:
        engine.dispose()


def test_missing_explicit_approval_rejects_write(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            with pytest.raises(LocatorRepairWriteError, match="requires allow_active_db_write"):
                ControlledLocatorRepairWriteService(session).apply(
                    _approved_items(),
                    approved_review_ids=APPROVED_IDS,
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=False,
                )
    finally:
        engine.dispose()


def test_only_approved_three_fields_are_written_and_pending_remains(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            result = ControlledLocatorRepairWriteService(session).apply(
                _approved_items(),
                approved_review_ids=APPROVED_IDS,
                dry_run=False,
                allow_active_db_write=True,
                confirmed_approval=True,
            )
            session.commit()

            locators = session.scalars(select(EvidenceLocator)).all()
            fields = {locator.field_name for locator in locators}
            reviews = session.scalars(select(ExtractionFieldReview).where(ExtractionFieldReview.paper_id == UUID(PILOT_PAPER_ID))).all()

            assert result.written_count == 3
            assert fields == {"catalyst_type", "metal_centers", "rate"}
            assert all(review.reviewer_status == "pending" for review in reviews)
            assert all(review.target_resolution_status == "active" for review in reviews)
            assert all(locator.locator_status == "exact_page" for locator in locators)
            assert all(locator.page in {6, 7} for locator in locators)
            assert all(locator.bbox is not None for locator in locators)
    finally:
        engine.dispose()


def test_name_and_convergence_settings_are_rejected(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            name_item = [item for item in _plan()["items"] if item["field"] == "name"]
            with pytest.raises(LocatorRepairWriteError, match="Rejected field"):
                ControlledLocatorRepairWriteService(session).apply(
                    name_item,
                    approved_review_ids={name_item[0]["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )

            convergence_item = {
                **_approved_items()[0],
                "review_id": "4ba0e490-5934-439c-8136-33a8ddf4e201",
                "field": "convergence_settings",
            }
            with pytest.raises(LocatorRepairWriteError, match="Rejected field"):
                ControlledLocatorRepairWriteService(session).apply(
                    [convergence_item],
                    approved_review_ids={convergence_item["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )
    finally:
        engine.dispose()


def test_non_pilot_paper_is_rejected(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            item = {**_approved_items()[0], "paper_id": "00000000000000000000000000000000"}
            with pytest.raises(LocatorRepairWriteError, match="Non-pilot"):
                ControlledLocatorRepairWriteService(session).apply(
                    [item],
                    approved_review_ids={item["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )
    finally:
        engine.dispose()


def test_write_does_not_unlock_verified_export_or_writing(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            result = ControlledLocatorRepairWriteService(session).apply(
                _approved_items(),
                approved_review_ids=APPROVED_IDS,
                dry_run=False,
                allow_active_db_write=True,
                confirmed_approval=True,
            )

            assert result.post_write is not None
            assert result.post_write["verified_rows"] == 0
            assert result.post_write["safe_verified_rows"] == 0
            assert result.post_write["export_eligible_count"] == 0
            assert result.post_write["writing_eligible_count"] == 0
            assert all("verified" not in locator.warning_reason for locator in session.scalars(select(EvidenceLocator)).all())
    finally:
        engine.dispose()


def test_rollback_snapshot_is_generated_before_write(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            result = ControlledLocatorRepairWriteService(session).apply(
                _approved_items(),
                approved_review_ids=APPROVED_IDS,
                dry_run=False,
                allow_active_db_write=True,
                confirmed_approval=True,
            )

            before = result.rollback_snapshot["pre_write"]
            assert before["pilot_locator_count"] == 0
            assert before["review_rows_total"] == 5
            assert before["pending_rows"] == 5
            assert before["verified_rows"] == 0
    finally:
        engine.dispose()


def test_missing_page_or_bbox_is_rejected_without_fabrication(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            missing_page = {**_approved_items()[0], "proposed_page": None}
            with pytest.raises(LocatorRepairWriteError, match="Missing proposed page"):
                ControlledLocatorRepairWriteService(session).apply(
                    [missing_page],
                    approved_review_ids={missing_page["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )
            missing_bbox = {**_approved_items()[0], "proposed_bbox": None}
            with pytest.raises(LocatorRepairWriteError, match="Missing proposed bbox"):
                ControlledLocatorRepairWriteService(session).apply(
                    [missing_bbox],
                    approved_review_ids={missing_bbox["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )
    finally:
        engine.dispose()


def test_integration_only_touches_approved_review_ids(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            result = ControlledLocatorRepairWriteService(session).apply(
                _approved_items(),
                approved_review_ids=APPROVED_IDS,
                dry_run=False,
                allow_active_db_write=True,
                confirmed_approval=True,
            )
            after_checksum = result.post_write["non_pilot_review_checksum"]
            before_checksum = result.rollback_snapshot["pre_write"]["non_pilot_review_checksum"]
            written_review_ids = {
                next(
                        review.id
                        for review in session.scalars(select(ExtractionFieldReview)).all()
                        if review.field_name == locator.field_name and review.paper_id.hex == PILOT_PAPER_ID
                    )
                for locator in session.scalars(select(EvidenceLocator)).all()
            }

            assert {str(item) for item in written_review_ids} == APPROVED_IDS
            assert after_checksum == before_checksum
            assert result.post_write["non_pilot_locator_count"] == result.rollback_snapshot["pre_write"]["non_pilot_locator_count"]
    finally:
        engine.dispose()


def test_verified_like_payload_is_rejected(tmp_path):
    engine, SessionLocal = _session(tmp_path)
    try:
        with SessionLocal() as session:
            _seed(session)
            item = {**_approved_items()[0], "verified": True}
            with pytest.raises(LocatorRepairWriteError, match="Verified-like"):
                ControlledLocatorRepairWriteService(session).apply(
                    [item],
                    approved_review_ids={item["review_id"]},
                    dry_run=False,
                    allow_active_db_write=True,
                    confirmed_approval=True,
                )
    finally:
        engine.dispose()

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier, Event

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Base,
    DFTResult,
    ExtractionFieldReview,
    ExternalAnalysisCandidate,
    ExternalAnalysisRun,
    Paper,
    PaperCorrection,
    PaperFigure,
    VerificationSessionPaperClaim,
    WritingCard,
)
from app.mcp import server as mcp_server
from app.services.extraction_review_service import ExtractionReviewService
from app.services.module_write_lock_service import ModuleWriteLockService
from app.services.paper_workbench_service import PaperWorkbenchService
from app.services.paper_reprocessing import PaperReprocessingService
from app.services.review_service import ReviewService
from app.services.verification_session_service import VerificationSessionService


def _database(tmp_path: Path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"timeout": 15},
        future=True,
    )
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("PRAGMA journal_mode=WAL"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine, factory


def _paper(factory, title: str = "Concurrent paper"):
    with factory() as session:
        paper = Paper(title=title, pdf_path="paper.pdf", authors=[])
        session.add(paper)
        session.commit()
        return paper.id


def test_overlapping_module_locks_are_atomic_but_different_papers_succeed(tmp_path):
    engine, factory = _database(tmp_path, "module-race.db")
    first_id = _paper(factory, "First")
    second_id = _paper(factory, "Second")
    gate = Barrier(2)

    def acquire(paper_id, module, owner):
        with factory() as session:
            gate.wait()
            try:
                lock = ModuleWriteLockService(session).acquire(
                    paper_id=paper_id, module_name=module, locked_by=owner
                )
                session.commit()
                return "ok", lock.lock_token
            except ValueError as exc:
                session.rollback()
                return "conflict", str(exc)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: acquire(*args), [(first_id, "content", "a"), (first_id, "sections", "b")]))
        assert sorted(item[0] for item in results) == ["conflict", "ok"]

        with factory() as session:
            other = ModuleWriteLockService(session).acquire(
                paper_id=second_id, module_name="content", locked_by="c"
            )
            session.commit()
            assert other.paper_id == second_id
    finally:
        engine.dispose()


def test_extraction_review_concurrent_create_has_one_winner(tmp_path):
    engine, factory = _database(tmp_path, "review-race.db")
    paper_id = _paper(factory)
    target_id = "00000000-0000-0000-0000-000000000001"
    gate = Barrier(2)

    def create(reviewer):
        with factory() as session:
            gate.wait()
            review = ExtractionReviewService(session)._get_or_create_review(
                paper_id, "dft_results", target_id, "value"
            )
            if not review.reviewer:
                review.reviewer = reviewer
            session.commit()
            return review.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(create, ["ai-1", "ai-2"]))
        assert ids[0] == ids[1]
        with factory() as session:
            assert session.scalar(select(func.count(ExtractionFieldReview.id))) == 1
    finally:
        engine.dispose()


def test_dft_candidate_identity_prevents_concurrent_duplicates(tmp_path):
    engine, factory = _database(tmp_path, "dft-race.db")
    paper_id = _paper(factory)
    gate = Barrier(2)
    item = {
        "adsorbate": "Li2S4",
        "property_type": "adsorption_energy",
        "value": -1.23,
        "unit": "eV",
        "reaction_step": "adsorption",
        "source_section": "Results",
        "source_figure": "Figure 2",
        "evidence_text": "Delta G is -1.23 eV",
        "confidence": 0.9,
        "evidence_payload": {"material_identity": "Fe-N4"},
        "signature": ("fe-n4", "adsorption_energy", "-1.23", "ev", "adsorption", "figure 2", ""),
    }

    def insert(_):
        with factory() as session:
            gate.wait()
            row = VerificationSessionService(session, Settings(storage_root=tmp_path))._insert_new_dft_candidate(
                paper_id=paper_id, candidate_item=item, source_label="test"
            )
            session.commit()
            return row.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(insert, range(2)))
        assert ids[0] == ids[1]
        with factory() as session:
            assert session.scalar(select(func.count(DFTResult.id))) == 1
    finally:
        engine.dispose()


def test_verification_session_claim_is_all_or_nothing_and_released_on_settle(tmp_path):
    engine, factory = _database(tmp_path, "session-claim.db")
    paper_id = _paper(factory)
    gate = Barrier(2)

    def create(reviewer):
        with factory() as session:
            gate.wait()
            try:
                result = VerificationSessionService(session, Settings(storage_root=tmp_path)).create_session(
                    paper_ids=[paper_id], scope="dft_only", refresh_materials=False, reviewer=reviewer
                )
                return "ok", result["session_id"]
            except ValueError as exc:
                return "conflict", str(exc)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["ai-1", "ai-2"]))
        assert sorted(item[0] for item in results) == ["conflict", "ok"]
        session_id = next(value for status, value in results if status == "ok")
        with factory() as session:
            VerificationSessionService(session, Settings(storage_root=tmp_path)).settle_session(
                session_id, reviewer="human"
            )
            assert session.scalar(
                select(func.count(VerificationSessionPaperClaim.id)).where(
                    VerificationSessionPaperClaim.status == "active"
                )
            ) == 0
    finally:
        engine.dispose()


def test_rebuild_operation_lock_rejects_same_paper_and_recovers(tmp_path):
    engine, factory = _database(tmp_path, "rebuild-race.db")
    paper_id = _paper(factory)
    entered = Event()
    release = Event()

    def hold_first():
        with factory() as session:
            service = PaperReprocessingService(session, Settings(storage_root=tmp_path))

            def callback(_paper_id):
                entered.set()
                assert release.wait(10)
                return {"status": "done"}

            return service._run_exclusive_rebuild(paper_id, "test", callback)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hold_first)
            assert entered.wait(10)
            with factory() as session:
                service = PaperReprocessingService(session, Settings(storage_root=tmp_path))
                try:
                    service._run_exclusive_rebuild(paper_id, "test", lambda _: {"status": "unexpected"})
                    raise AssertionError("same-paper rebuild should conflict")
                except ValueError as exc:
                    assert str(exc).startswith("paper_operation_conflict")
            release.set()
            assert future.result()["status"] == "done"

        with factory() as session:
            result = PaperReprocessingService(session, Settings(storage_root=tmp_path))._run_exclusive_rebuild(
                paper_id, "test", lambda _: {"status": "recovered"}
            )
            assert result["status"] == "recovered"
    finally:
        release.set()
        engine.dispose()


def test_direct_workspace_prepare_cannot_bypass_same_paper_operation_lock(tmp_path):
    engine, factory = _database(tmp_path, "prepare-lock.db")
    paper_id = _paper(factory, "Prepare locked")
    other_paper_id = _paper(factory, "Prepare other")

    try:
        with factory() as session:
            held = ModuleWriteLockService(session).acquire(
                paper_id=paper_id,
                module_name="all_non_dft",
                locked_by="paper_operation:prepare_workspace:test-holder",
            )
            session.commit()

        with factory() as session:
            try:
                PaperWorkbenchService(session, Settings(storage_root=tmp_path)).prepare_paper_workspace(paper_id)
                raise AssertionError("same-paper direct prepare should conflict")
            except ValueError as exc:
                assert str(exc).startswith("paper_operation_conflict:prepare_workspace")

        with factory() as session:
            result = PaperWorkbenchService(session, Settings(storage_root=tmp_path)).prepare_paper_workspace(other_paper_id)
            assert result["paper_id"] == str(other_paper_id)

        with factory() as session:
            ModuleWriteLockService(session).release(
                lock_token=held.lock_token,
                released_by="paper_operation:prepare_workspace:test-holder",
            )
            session.commit()

        with factory() as session:
            result = PaperWorkbenchService(session, Settings(storage_root=tmp_path)).prepare_paper_workspace(paper_id)
            assert result["paper_id"] == str(paper_id)
    finally:
        engine.dispose()


def test_new_dft_candidate_materialization_requires_dft_results_write_lock(tmp_path):
    engine, factory = _database(tmp_path, "dft-lock.db")
    paper_id = _paper(factory, "DFT lock")

    with factory() as session:
        run = ExternalAnalysisRun(
            paper_id=paper_id,
            source="ide",
            source_label="ai-1",
            raw_text="",
        )
        session.add(run)
        session.flush()
        session.add(
            ExternalAnalysisCandidate(
                run_id=run.id,
                paper_id=paper_id,
                candidate_type="object_review_audit",
                status="candidate",
                normalized_payload={
                    "target_type": "dft_results",
                    "target_id": "new",
                    "decision": "new_candidate",
                    "corrected_value": {
                        "material_identity": "Fe-N4",
                        "property_type": "adsorption_energy",
                        "value": -1.23,
                        "unit": "eV",
                        "adsorbate": "Li2S4",
                        "reaction_step": "adsorption",
                    },
                    "evidence_location": {
                        "page": 3,
                        "quoted_text": "The adsorption energy of Li2S4 is -1.23 eV on Fe-N4.",
                    },
                    "confidence": 0.9,
                },
            )
        )
        session.commit()

    try:
        with factory() as session:
            service = VerificationSessionService(session, Settings(storage_root=tmp_path))
            try:
                service.apply_import_rules_for_paper(paper_id=paper_id, reviewer="ai-1")
                raise AssertionError("DFT new candidate materialization should require dft_results lock")
            except ValueError as exc:
                assert str(exc) == "module_write_lock_required:dft_results"
            session.rollback()
            assert session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)) == 0

        with factory() as session:
            lock = ModuleWriteLockService(session).acquire(
                paper_id=paper_id,
                module_name="dft_results",
                locked_by="ai-1",
            )
            session.commit()
            summary = VerificationSessionService(session, Settings(storage_root=tmp_path)).apply_import_rules_for_paper(
                paper_id=paper_id,
                reviewer="ai-1",
                write_lock_tokens=[lock.lock_token],
            )
            session.commit()
            assert summary["new_dft_candidates"]["materialized_count"] == 1
            assert session.scalar(select(func.count(DFTResult.id)).where(DFTResult.paper_id == paper_id)) == 1
    finally:
        engine.dispose()


def test_recrop_stale_write_removes_rendered_orphan_file(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    engine, factory = _database(tmp_path, "recrop-stale.db")
    storage_root = tmp_path / "storage"
    pdf_path = storage_root / "pdf" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True)
    document = fitz.open()
    document.new_page(width=200, height=200)
    document.save(pdf_path)
    document.close()

    with factory() as session:
        paper = Paper(title="Recrop race", pdf_path=str(pdf_path), authors=[])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 1",
            image_path="old/figure.png",
            page=1,
            write_version=1,
        )
        session.add(figure)
        session.commit()
        figure_id = figure.id

    settings = Settings(storage_root=storage_root, database_url="sqlite://")
    scope_calls = 0

    @contextmanager
    def test_session_scope(_database_url):
        nonlocal scope_calls
        scope_calls += 1
        if scope_calls == 2:
            with factory() as competing_session:
                competing_session.execute(
                    update(PaperFigure).where(PaperFigure.id == figure_id).values(write_version=2)
                )
                competing_session.commit()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_server, "session_scope", test_session_scope)
    monkeypatch.setattr(
        mcp_server,
        "require_mcp_capability",
        lambda _capability: SimpleNamespace(source_prefix="test-ai"),
    )

    try:
        with pytest.raises(ValueError, match="write_conflict:figure_version_stale"):
            mcp_server.recrop_figure(str(figure_id), strategy="full_page")

        assert list((storage_root / "figures").rglob("*.png")) == []
        with factory() as session:
            stored = session.get(PaperFigure, figure_id)
            assert stored.image_path == "old/figure.png"
            assert stored.write_version == 2
    finally:
        engine.dispose()


def test_review_service_recrop_approval_updates_figure_and_evidence(tmp_path, monkeypatch):
    pytest.importorskip("fitz")
    engine, factory = _database(tmp_path, "review-recrop-success.db")
    storage_root = tmp_path / "storage"
    pdf_path = storage_root / "pdf" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.review_service.get_settings",
        lambda: Settings(storage_root=storage_root, database_url="sqlite://"),
    )

    import fitz

    document = fitz.open()
    document.new_page(width=200, height=200)
    document.save(pdf_path)
    document.close()

    with factory() as session:
        paper = Paper(title="Review recrop", pdf_path=str(pdf_path), authors=[])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 1",
            image_path="old/figure.png",
            page=1,
            write_version=1,
        )
        correction = PaperCorrection(
            paper_id=paper.id,
            source="dual_ai",
            field_name="figures",
            target_path=f"figures:{figure.id}:prov",
            operation="recrop_figure",
            proposed_value={"strategy": "full_page"},
            reason="Use a full-page recrop.",
            evidence_payload={"page": 1, "quoted_text": "Figure 1"},
            status="pending",
        )
        session.add_all([figure, correction])
        session.commit()
        correction_id = correction.id
        figure_id = figure.id

    try:
        with factory() as session:
            service = ReviewService(session)
            approved = service.approve_correction(correction_id, reviewer="dual_ai")
            session.commit()

            assert approved.status == "approved"
            assert approved.evidence_payload["recrop_result"]["figure_id"] == str(figure_id)

        with factory() as session:
            stored = session.get(PaperFigure, figure_id)
            correction = session.get(PaperCorrection, correction_id)
            assert stored is not None
            assert stored.image_path != "old/figure.png"
            assert stored.crop_status == "recropped"
            assert stored.crop_source == "recrop:full_page:review_service"
            assert stored.write_version == 2
            assert stored.prov[-1]["action"] == "recrop_figure"
            assert stored.prov[-1]["source_correction_id"] == str(correction_id)
            assert correction is not None
            assert correction.status == "approved"
            assert correction.evidence_payload["recrop_result"]["image_path"] == stored.image_path
            assert (storage_root / "figures" / str(stored.paper_id) / Path(stored.image_path).name).exists()
    finally:
        engine.dispose()


def test_review_service_recrop_stale_write_removes_rendered_orphan_file(tmp_path, monkeypatch):
    pytest.importorskip("fitz")
    engine, factory = _database(tmp_path, "review-recrop-stale.db")
    storage_root = tmp_path / "storage"
    pdf_path = storage_root / "pdf" / "paper.pdf"
    pdf_path.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.review_service.get_settings",
        lambda: Settings(storage_root=storage_root, database_url="sqlite://"),
    )

    import fitz

    document = fitz.open()
    document.new_page(width=200, height=200)
    document.save(pdf_path)
    document.close()

    with factory() as session:
        paper = Paper(title="Review recrop race", pdf_path=str(pdf_path), authors=[])
        session.add(paper)
        session.flush()
        figure = PaperFigure(
            paper_id=paper.id,
            caption="Figure 1",
            image_path="old/figure.png",
            page=1,
            write_version=1,
        )
        correction = PaperCorrection(
            paper_id=paper.id,
            source="dual_ai",
            field_name="figures",
            target_path=f"figures:{figure.id}:prov",
            operation="recrop_figure",
            proposed_value={"strategy": "full_page"},
            reason="Use a full-page recrop.",
            evidence_payload={"page": 1, "quoted_text": "Figure 1"},
            status="pending",
        )
        session.add_all([figure, correction])
        session.commit()
        correction_id = correction.id
        figure_id = figure.id

    original_render = ReviewService._render_figure_recrop_plan

    def competing_render(self, recrop_plan):
        rendered = original_render(self, recrop_plan)
        with factory() as competing_session:
            competing_session.execute(
                update(PaperFigure).where(PaperFigure.id == figure_id).values(write_version=2)
            )
            competing_session.commit()
        return rendered

    monkeypatch.setattr(ReviewService, "_render_figure_recrop_plan", competing_render)

    try:
        with factory() as session:
            with pytest.raises(ValueError, match="write_conflict:figure_version_stale"):
                ReviewService(session).approve_correction(correction_id, reviewer="dual_ai")

        assert list((storage_root / "figures").rglob("*.png")) == []
        with factory() as session:
            stored = session.get(PaperFigure, figure_id)
            correction = session.get(PaperCorrection, correction_id)
            assert stored.image_path == "old/figure.png"
            assert stored.write_version == 2
            assert correction.status == "pending"
    finally:
        engine.dispose()


def test_workspace_commit_failure_restores_existing_writing_card_files(tmp_path, monkeypatch):
    engine, factory = _database(tmp_path, "workspace-restore.db")
    storage_root = tmp_path / "storage"
    with factory() as session:
        paper = Paper(
            title="Workspace rollback",
            authors=[],
            pdf_path=str(storage_root / "missing.pdf"),
            pdf_quality_report={
                "quality_status": "Broken",
                "quality_score": 0.0,
                "parse_allowed": False,
                "reason": "test_without_pdf",
            },
        )
        session.add(paper)
        session.flush()
        session.add(WritingCard(paper_id=paper.id, paper_type="existing-card"))
        session.commit()
        paper_id = paper.id

    workspace_root = storage_root / "by_id" / str(paper_id)
    workspace_root.mkdir(parents=True)
    sentinel = workspace_root / "writing-card-before-commit.txt"
    sentinel.write_text("existing writing-card workspace", encoding="utf-8")

    session = factory()
    service = PaperWorkbenchService(session, Settings(storage_root=storage_root))
    monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("forced commit failure")))

    try:
        with pytest.raises(RuntimeError, match="forced commit failure"):
            service._prepare_paper_workspace_unlocked(paper_id)

        assert sentinel.read_text(encoding="utf-8") == "existing writing-card workspace"
        assert not list(workspace_root.parent.glob(f".{workspace_root.name}.staging-*"))
        assert not list(workspace_root.parent.glob(f".{workspace_root.name}.backup-*"))
        with factory() as verification_session:
            card = verification_session.scalar(select(WritingCard).where(WritingCard.paper_id == paper_id))
            assert card.paper_type == "existing-card"
    finally:
        session.close()
        engine.dispose()

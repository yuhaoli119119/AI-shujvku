from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

SYSTEM_ROOT = Path(__file__).resolve().parents[2]
ROOT = SYSTEM_ROOT.parent
BACKEND = SYSTEM_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

RUN_DIR = ROOT / "local" / "test-runs" / "pdf-regression" / f"new_real_{int(time.time())}"
STORAGE_DIR = RUN_DIR / "storage"
PDF_DIR = ROOT / "local" / "test-fixtures" / "pdf-regression" / "new_real_papers"

PAPERS = [
    {
        "path": PDF_DIR / "mdpi_nanomat_vs2_sac_lis_2024.pdf",
        "source": "https://pdfs.semanticscholar.org/b097/dfef9389db233b290bf10a3b182f36173183.pdf",
        "metadata": {
            "title": "Rational Design of Non-Noble Metal Single-Atom Catalysts in Lithium-Sulfur Batteries through First Principles Calculations",
            "doi": "10.3390/nano14080692",
            "year": 2024,
            "journal": "Nanomaterials",
        },
    },
    {
        "path": PDF_DIR / "mdpi_molecules_p_gcn_lis_2024.pdf",
        "source": "https://pdfs.semanticscholar.org/ad67/8c14ac217aa834d1224346fcd7a1232b47b0.pdf",
        "metadata": {
            "title": "First-Principles Investigation of Phosphorus-Doped Graphitic Carbon Nitride as Anchoring Material for the Lithium-Sulfur Battery",
            "doi": "10.3390/molecules29122746",
            "year": 2024,
            "journal": "Molecules",
        },
    },
]


def configure_environment() -> tuple[str, object]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base_url = os.getenv("LITAI_TEST_ROOT_DATABASE_URL") or os.getenv("LITAI_DATABASE_URL")
    if not base_url:
        raise RuntimeError("Set LITAI_TEST_ROOT_DATABASE_URL or LITAI_DATABASE_URL to PostgreSQL")
    parsed = make_url(base_url)
    if not parsed.drivername.startswith("postgresql"):
        raise RuntimeError("PDF regression requires PostgreSQL")

    admin_url = parsed.difference_update_query(["options"])
    schema = f"pdf_regression_{uuid4().hex}"
    admin_engine = create_engine(admin_url, future=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    query = dict(admin_url.query)
    query["options"] = f"-csearch_path={schema},public"
    database_url = admin_url.set(query=query).render_as_string(hide_password=False)
    os.environ["LITAI_DATABASE_URL"] = database_url
    os.environ["LITAI_STORAGE_ROOT"] = str(STORAGE_DIR)
    os.environ["LITAI_GROBID_URL"] = "http://127.0.0.1:9"
    os.environ["LITAI_WRITER_BACKEND"] = "rule"
    os.environ["LITAI_WRITER_API_BASE"] = ""
    os.environ["LITAI_WRITER_API_KEY"] = ""
    os.environ["LITAI_WRITER_TIMEOUT_SECONDS"] = "2"
    return schema, admin_engine


def counts_for(session, paper_id: UUID) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.db.models import (
        CatalystSample,
        DFTResult,
        DFTSetting,
        ElectrochemicalPerformance,
        EvidenceLocator,
        ExtractionFieldReview,
        MechanismClaim,
        PaperFigure,
        PaperSection,
        PaperTable,
        WritingCard,
    )

    models = {
        "sections": PaperSection,
        "tables": PaperTable,
        "figures": PaperFigure,
        "figures_with_images": PaperFigure,
        "dft_settings": DFTSetting,
        "dft_results": DFTResult,
        "field_reviews": ExtractionFieldReview,
        "evidence_locators": EvidenceLocator,
        "catalyst_samples": CatalystSample,
        "electrochemical_performance": ElectrochemicalPerformance,
        "mechanism_claims": MechanismClaim,
        "writing_cards": WritingCard,
    }
    out: dict[str, int] = {}
    for key, model in models.items():
        stmt = select(func.count()).select_from(model).where(model.paper_id == paper_id)
        if key == "figures_with_images":
            stmt = stmt.where(PaperFigure.image_path.is_not(None))
        out[key] = int(session.scalar(stmt) or 0)
    return out


def warning_counts(session, paper_id: UUID) -> dict[str, int]:
    from app.services.extraction_schema_service import ExtractionSchemaService

    payload = ExtractionSchemaService(session).results(paper_id)
    counts: dict[str, int] = {}
    for warning in payload.validation_warnings:
        counts[warning.code] = counts.get(warning.code, 0) + 1
    return counts


def mark_one_review_verified(session, paper_id: UUID, detail) -> dict:
    from app.schemas.extraction import ExtractionReviewMarkVerifiedRequest
    from app.services.extraction_review_service import ExtractionReviewService

    review_service = ExtractionReviewService(session)
    candidates = []
    if detail.dft_settings_items:
        candidates.append(("dft_settings", str(detail.dft_settings_items[0].id), ["software"]))
    if detail.dft_results_items:
        candidates.append(("dft_results", str(detail.dft_results_items[0].id), ["energy_type", "value"]))
    if detail.catalyst_samples_items:
        candidates.append(("catalyst_samples", str(detail.catalyst_samples_items[0].id), ["name"]))

    errors = []
    for target_type, target_id, fields in candidates:
        try:
            marked = review_service.mark_verified(
                paper_id,
                ExtractionReviewMarkVerifiedRequest(
                    target_type=target_type,
                    target_id=target_id,
                    field_names=fields,
                    reviewer="e2e_acceptance",
                    reviewer_note="controlled end-to-end acceptance check",
                ),
            )
            return {
                "status": "verified",
                "target_type": target_type,
                "target_id": target_id,
                "fields": fields,
                "review_ids": [str(item.id) for item in marked],
                "safe_verified": [item.verified for item in marked],
            }
        except Exception as exc:
            errors.append(f"{target_type}:{target_id}:{fields}:{exc}")
    return {"status": "not_verified", "errors": errors}


async def run() -> dict:
    if not PDF_DIR.exists():
        raise FileNotFoundError(f"Missing PDF regression fixture directory: {PDF_DIR}")

    schema, admin_engine = configure_environment()
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    from app.config import get_settings
    from app.db.session import init_db, session_scope
    from app.schemas.api import PaperTranslationPreviewRequest
    from app.api.papers.detail import preview_paper_translation
    from app.services.extraction_review_service import ExtractionReviewService
    from app.services.paper_ingestion import PaperIngestionService
    from app.services.paper_query import PaperQueryService
    from app.services.evidence_locator_service import EvidenceLocatorService
    from app.services.extraction_schema_service import ExtractionSchemaService

    get_settings.cache_clear()
    settings = get_settings()
    try:
        init_db(settings.database_url)

        summaries = []
        with session_scope(settings.database_url) as session:
            service = PaperIngestionService(session, settings)
            for item in PAPERS:
                paper = await service.ingest_pdf(
                    source_path=item["path"],
                    original_filename=item["path"].name,
                    external_metadata=item["metadata"],
                    source_reference=item["source"],
                    library_name="新真实文献验收库",
                    ingest_source="downloaded_semantic_scholar_pdf",
                )

                review_service = ExtractionReviewService(session)
                prepared = review_service.prepare_pending_reviews(paper.id)
                detail = PaperQueryService(session).get_paper_detail(paper.id)
                manual_verify = mark_one_review_verified(session, paper.id, detail)
                detail = PaperQueryService(session).get_paper_detail(paper.id)
                translation = await preview_paper_translation(
                    paper.id,
                    PaperTranslationPreviewRequest(include_abstract=True, max_sections=2, max_chars_per_item=1200),
                    session=session,
                    settings=settings,
                )
                locators = EvidenceLocatorService(session).list_locators_for_paper(paper.id)
                schema_payload = ExtractionSchemaService(session).results(paper.id)
                counts = counts_for(session, paper.id)
                dft_types: dict[str, int] = {}
                dft_examples = []
                for result in detail.dft_results_items:
                    dft_types[result.property_type or "unknown"] = dft_types.get(result.property_type or "unknown", 0) + 1
                    if len(dft_examples) < 8:
                        dft_examples.append(result.model_dump(mode="json"))

                summaries.append(
                    {
                        "filename": item["path"].name,
                        "paper_id": str(paper.id),
                        "title": paper.title,
                        "doi": paper.doi,
                        "year": paper.year,
                        "journal": paper.journal,
                        "paper_type": getattr(paper, "paper_type", None),
                        "type_confidence": getattr(paper, "type_confidence", None),
                        "classification_source": getattr(paper, "classification_source", None),
                        "counts": counts,
                        "detail_counts": detail.counts.model_dump(),
                        "prepared_reviews": prepared.created_count + prepared.existing_count,
                        "manual_verify": manual_verify,
                        "verified_reviews": sum(
                            1
                            for item in review_service.list_reviews(paper.id)
                            if item.reviewer_status == "verified"
                        ),
                        "safe_verified_reviews": sum(
                            1 for item in review_service.list_reviews(paper.id) if item.verified
                        ),
                        "translation_backend": translation.backend_used,
                        "translation_status": translation.llm_status,
                        "translation_items": len(translation.items),
                        "locator_count_from_api": len(locators),
                        "schema_result_counts": {
                            key: len(value) for key, value in schema_payload.results.items()
                        },
                        "validation_status": schema_payload.validation_status,
                        "warning_counts": warning_counts(session, paper.id),
                        "dft_types": dft_types,
                        "dft_examples": dft_examples,
                    }
                )

        result = {
            "run_dir": str(RUN_DIR),
            "database_schema": schema,
            "storage": str(STORAGE_DIR),
            "papers": summaries,
        }
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        from sqlalchemy import text

        from app.db.session import _engines, _session_factories

        for engine in list(_engines.values()):
            engine.dispose()
        _engines.clear()
        _session_factories.clear()
        get_settings.cache_clear()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


PAPERS = [
    {
        "arxiv_id": "1710.10084",
        "file": "1710.10084_single_vacancy_adsorption.pdf",
        "title": "Atomic adsorption on graphene with a single vacancy: systematic DFT study through the Periodic Table of Elements",
        "year": 2017,
        "authors": [
            "Igor A. Pasti",
            "Aleksandar Jovanovic",
            "Ana S. Dobrota",
            "Slavko V. Mentus",
            "Borje Johansson",
            "Natalia V. Skorodumova",
        ],
        "journal": "Phys. Chem. Chem. Phys.",
        "abstract": "Systematic DFT study of atomic adsorption on graphene with a single vacancy across rows 1 to 6 of the periodic table.",
    },
    {
        "arxiv_id": "2308.05425",
        "file": "2308.05425_stone_wales_reactivity.pdf",
        "title": "Reactivity of Stone-Wales defect in graphene lattice -- DFT study",
        "year": 2023,
        "authors": [
            "Aleksandar Z. Jovanovic",
            "Ana S. Dobrota",
            "Natalia V. Skorodumova",
            "Igor A. Pasti",
        ],
        "journal": "arXiv",
        "abstract": "Density functional theory study of atomic adsorption and mechanical deformation effects for Stone-Wales defects in graphene.",
    },
    {
        "arxiv_id": "1405.1928",
        "file": "1405.1928_twisted_bilayer_defects.pdf",
        "title": "Point Defects in Twisted Bilayer Graphene: A Density Functional Theory Study",
        "year": 2014,
        "authors": ["Kanchan Ulman", "Shobhana Narasimhan"],
        "journal": "Phys. Rev. B",
        "abstract": "Ab initio density functional theory study of Stone-Wales defects and monovacancies in twisted bilayer graphene.",
    },
    {
        "arxiv_id": "1112.5598",
        "file": "1112.5598_divacancies_irradiated_graphene.pdf",
        "title": "Electronic and structural characterization of divacancies in irradiated graphene",
        "year": 2011,
        "authors": [
            "Miguel M. Ugeda",
            "Ivan Brihuega",
            "Fanny Hiebel",
            "Pierre Mallet",
            "Jean-Yves Veuillen",
            "Jose M. Gomez-Rodriguez",
            "Felix Yndurain",
        ],
        "journal": "Physical Review B",
        "abstract": "First-principles calculations and STM/STS characterization of carbon divacancies in graphene.",
    },
    {
        "arxiv_id": "1207.3194",
        "file": "1207.3194_dft_dftb_graphene_defects.pdf",
        "title": "A comparative study of density functional and density functional tight binding calculations of defects in graphene",
        "year": 2012,
        "authors": [
            "Alberto Zobelli",
            "Viktoria V. Ivanovskaya",
            "Philipp Wagner",
            "Irene Suarez-Martinez",
            "Abu Yaya",
            "Chris P. Ewels",
        ],
        "journal": "Physica Status Solidi B",
        "abstract": "Comparative DFT and DFTB calculations of point and line defects in graphene, including vacancies and Stone-Wales defects.",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download five graphite/graphene defect DFT papers, ingest them, and export Codex context QA."
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=BACKEND_ROOT / "tests" / "data" / "codex_graphite_defects_e2e",
        help="Ignored artifact directory for PDFs, runtime DB/storage, and reports.",
    )
    parser.add_argument("--redownload", action="store_true", help="Download PDFs even when local copies exist.")
    parser.add_argument(
        "--grobid-url",
        default="http://127.0.0.1:1",
        help="GROBID URL. Default fails fast for local offline smoke runs.",
    )
    parser.add_argument("--docling-disabled", action="store_true", help="Force the pypdf fallback parser.")
    return parser.parse_args()


def download_pdf(arxiv_id: str, destination: Path, *, redownload: bool = False) -> None:
    if destination.exists() and destination.stat().st_size > 10_000 and not redownload:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        f"https://arxiv.org/pdf/{arxiv_id}",
        headers={"User-Agent": "Codex Literature AI e2e test"},
    )
    with urlopen(request, timeout=120) as response:
        destination.write_bytes(response.read())


def count(session, model, paper_id):
    from sqlalchemy import func, select

    return int(session.scalar(select(func.count()).select_from(model).where(model.paper_id == paper_id)) or 0)


def summarize_codex_context(context) -> dict:
    payload = context.model_dump(mode="json")
    ctx = payload.get("context", {})
    figures = ctx.get("content", {}).get("figures", [])
    figure_flags = {}
    for figure in figures:
        for flag in ((figure.get("image_review") or {}).get("flags") or []):
            figure_flags[flag] = figure_flags.get(flag, 0) + 1
    return {
        "warnings": ctx.get("warnings", []),
        "reliability_policy": ctx.get("reliability_policy", {}),
        "recommended_next_actions": ctx.get("recommended_next_actions", []),
        "markdown_chars": len(payload.get("markdown") or ""),
        "dft_export_readiness": ctx.get("dft_export_readiness", {}),
        "figure_review_flags": figure_flags,
    }


def summarize_codex_item_context(context) -> dict | None:
    if context is None:
        return None
    payload = context.model_dump(mode="json")
    ctx = payload.get("context", {})
    item = ctx.get("item") or {}
    locators = (ctx.get("evidence_locators") or {}).get("items") or []
    review = item.get("image_review") or {}
    export_safety = ctx.get("export_safety") or item.get("export_safety") or {}
    return {
        "item_type": ctx.get("item_type"),
        "item_id": str(payload.get("item_id") or item.get("id") or ""),
        "markdown_chars": len(payload.get("markdown") or ""),
        "locator_count": len(locators),
        "locator_status_counts": (ctx.get("evidence_locators") or {}).get("status_counts") or {},
        "related_sections": len((ctx.get("nearby_context") or {}).get("related_sections") or []),
        "export_safety": export_safety,
        "image_review": review,
        "recommended_next_actions": ctx.get("recommended_next_actions", []),
    }


async def run(args: argparse.Namespace) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.db.models import (
        DFTResult,
        DFTSetting,
        EvidenceLocator,
        MechanismClaim,
        Paper,
        PaperFigure,
        PaperSection,
        PaperTable,
        WritingCard,
    )
    from app.db.session import get_engine, init_db
    from app.services.codex_context_service import CodexContextService
    from app.services.paper_ingestion import PaperIngestionService

    workdir = args.workdir.resolve()
    pdf_dir = workdir / "pdfs"
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    runtime_root = workdir / "runtime" / run_id
    report_dir = workdir / "reports"
    db_path = runtime_root / "database.sqlite"
    storage_root = runtime_root / "storage"
    report_path = report_dir / f"{run_id}.json"

    for item in PAPERS:
        download_pdf(item["arxiv_id"], pdf_dir / item["file"], redownload=args.redownload)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    os.environ["LITAI_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["LITAI_STORAGE_ROOT"] = str(storage_root)
    os.environ["LITAI_GROBID_URL"] = args.grobid_url
    os.environ["LITAI_WRITER_API_KEY"] = ""
    os.environ["LITAI_WRITER_API_BASE"] = ""
    if args.docling_disabled:
        os.environ["LITAI_DOCLING_ENABLED"] = "false"
    get_settings.cache_clear()

    settings = get_settings()
    init_db(settings.database_url)
    engine = get_engine(settings.database_url)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    started = time.perf_counter()
    results = []
    with Session() as session:
        ingestion = PaperIngestionService(session=session, settings=settings)
        context_service = CodexContextService(session=session, settings=settings)
        for item in PAPERS:
            source_path = pdf_dir / item["file"]
            paper_started = time.perf_counter()
            record = {
                "arxiv_id": item["arxiv_id"],
                "source_pdf": str(source_path),
                "source_url": f"https://arxiv.org/abs/{item['arxiv_id']}",
                "title": item["title"],
            }
            try:
                paper = await ingestion.ingest_pdf(
                    source_path=source_path,
                    original_filename=source_path.name,
                    external_metadata={
                        "title": item["title"],
                        "authors": item["authors"],
                        "year": item["year"],
                        "journal": item["journal"],
                        "abstract": item["abstract"],
                        "url": f"https://arxiv.org/abs/{item['arxiv_id']}",
                        "identifier": item["arxiv_id"],
                    },
                    source_reference=f"https://arxiv.org/abs/{item['arxiv_id']}",
                    library_name="Codex Graphite Defects E2E",
                    ingest_source="arxiv_pdf",
                )
                record["status"] = getattr(paper, "_ingest_status", "completed")
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                paper = None

            if paper is not None:
                session.refresh(paper)
                context = context_service.build_context(paper.id)
                first_dft_result = session.scalars(
                    select(DFTResult).where(DFTResult.paper_id == paper.id).limit(1)
                ).first()
                first_figure = session.scalars(
                    select(PaperFigure).where(PaperFigure.paper_id == paper.id).limit(1)
                ).first()
                codex_item_checks = {}
                if first_dft_result is not None:
                    codex_item_checks["first_dft_result"] = summarize_codex_item_context(
                        context_service.build_item_context(paper.id, "dft_result", first_dft_result.id)
                    )
                if first_figure is not None:
                    codex_item_checks["first_figure"] = summarize_codex_item_context(
                        context_service.build_item_context(paper.id, "figure", first_figure.id)
                    )
                record.update(
                    {
                        "paper_id": str(paper.id),
                        "stored_title": paper.title,
                        "paper_type": paper.paper_type,
                        "classification_source": paper.classification_source,
                        "oa_status": paper.oa_status,
                        "counts": {
                            "sections": count(session, PaperSection, paper.id),
                            "figures": count(session, PaperFigure, paper.id),
                            "tables": count(session, PaperTable, paper.id),
                            "dft_settings": count(session, DFTSetting, paper.id),
                            "dft_results": count(session, DFTResult, paper.id),
                            "mechanism_claims": count(session, MechanismClaim, paper.id),
                            "writing_cards": count(session, WritingCard, paper.id),
                            "evidence_locators": count(session, EvidenceLocator, paper.id),
                        },
                        "codex_context": summarize_codex_context(context) if context else None,
                        "codex_item_checks": codex_item_checks,
                    }
                )
            record["elapsed_seconds"] = round(time.perf_counter() - paper_started, 2)
            results.append(record)
            print(json.dumps(record, ensure_ascii=False))

    report = {
        "run_id": run_id,
        "database_url": f"sqlite:///{db_path.as_posix()}",
        "storage_root": str(storage_root),
        "report_path": str(report_path),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "results": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    report = asyncio.run(run(parse_args()))
    print(f"REPORT={report['report_path']}")


if __name__ == "__main__":
    main()

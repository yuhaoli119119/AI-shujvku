import asyncio
import os
import sys
import time
import urllib.request
from pathlib import Path
from datetime import datetime

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Force output to use UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PAPERS_TO_INGEST = [
    {
        "arxiv_id": "1803.01219",
        "file": "1803.01219_graphdiyne_nanotubes.pdf",
        "title": "Structural and Electronic Properties of Graphdiyne Carbon Nanotubes",
    },
    {
        "arxiv_id": "1211.4310",
        "file": "1211.4310_graphdiyne_nanoribbons.pdf",
        "title": "Configurations and electronic properties of graphyne- and graphdiyne-based nanoribbons",
    }
]

def download_pdf(arxiv_id: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 10000:
        print(f"File {destination.name} already exists. Skipping download.")
        return
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    print(f"Downloading {url} to {destination}...")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Codex Literature AI e2e test client"}
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        destination.write_bytes(response.read())
    print(f"Downloaded {destination.name} successfully.")

def count_items(session, model, paper_id) -> int:
    from sqlalchemy import func, select
    return int(session.scalar(select(func.count()).select_from(model).where(model.paper_id == paper_id)) or 0)

async def main():
    # Configure environment variables to use the 140012 SQLite library
    db_path = "/data/libraries/graphite_defect_validation_20260604_140012/database.sqlite"
    storage_root = "/data/libraries/graphite_defect_validation_20260604_140012/papers"
    
    os.environ["LITAI_DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["LITAI_STORAGE_ROOT"] = storage_root
    os.environ["LITAI_GROBID_URL"] = "http://grobid:8070"
    os.environ["LITAI_WRITER_API_KEY"] = ""
    os.environ["LITAI_WRITER_API_BASE"] = ""
    
    # Import app services after setting environment variables
    from app.config import get_settings
    from app.db.session import get_engine, init_db
    from sqlalchemy.orm import sessionmaker
    from app.services.paper_ingestion import PaperIngestionService
    from app.db.models import (
        Paper,
        PaperSection,
        PaperFigure,
        PaperTable,
        DFTSetting,
        DFTResult,
        MechanismClaim,
        WritingCard,
        EvidenceLocator,
    )
    
    get_settings.cache_clear()
    settings = get_settings()
    
    # Ensure directories exist
    pdf_dir = Path(storage_root) / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    
    # Download PDFs
    for item in PAPERS_TO_INGEST:
        dest = pdf_dir / item["file"]
        download_pdf(item["arxiv_id"], dest)
    
    # Initialize DB session
    init_db(settings.database_url)
    engine = get_engine(settings.database_url)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    
    print("\nStarting ingestion...")
    with Session() as session:
        ingestion = PaperIngestionService(session=session, settings=settings)
        for item in PAPERS_TO_INGEST:
            source_path = pdf_dir / item["file"]
            print(f"\nProcessing: {item['title']} (arXiv:{item['arxiv_id']})")
            start_time = time.perf_counter()
            try:
                paper = await ingestion.ingest_pdf(
                    source_path=source_path,
                    original_filename=source_path.name,
                    copy_pdf=False, # We placed it directly in the storage pdf folder
                    external_metadata={
                        "title": item["title"],
                        "identifier": item["arxiv_id"],
                        "url": f"https://arxiv.org/abs/{item['arxiv_id']}",
                    },
                    source_reference=f"https://arxiv.org/abs/{item['arxiv_id']}",
                    library_name="graphite_defect_validation_20260604_140012",
                    ingest_source="arxiv_pdf",
                )
                session.refresh(paper)
                elapsed = time.perf_counter() - start_time
                print(f"Ingested successfully in {elapsed:.2f} seconds.")
                print(f"Paper ID: {paper.id}")
                print(f"Counts:")
                print(f"  - Sections: {count_items(session, PaperSection, paper.id)}")
                print(f"  - Figures: {count_items(session, PaperFigure, paper.id)}")
                print(f"  - Tables: {count_items(session, PaperTable, paper.id)}")
                print(f"  - DFT Settings: {count_items(session, DFTSetting, paper.id)}")
                print(f"  - DFT Results: {count_items(session, DFTResult, paper.id)}")
                print(f"  - Mechanism Claims: {count_items(session, MechanismClaim, paper.id)}")
                print(f"  - Writing Cards: {count_items(session, WritingCard, paper.id)}")
                print(f"  - Evidence Locators: {count_items(session, EvidenceLocator, paper.id)}")
            except Exception as e:
                print(f"Failed to ingest {item['title']}: {e}")
                import traceback
                traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())

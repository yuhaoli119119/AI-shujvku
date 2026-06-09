import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.db.session import session_scope
from app.db.models import Paper
from app.config import get_settings
from app.utils.artifact_paths import resolve_persisted_artifact_path
from app.utils.artifact_status import build_paper_artifact_status
import json

def run_audit():
    settings = get_settings()
    print(f"Current CWD: {Path.cwd()}")
    print(f"Settings storage_root: {settings.storage_root}")
    print(f"Settings storage_root (resolved): {settings.storage_root.resolve()}")
    
    with session_scope(settings.database_url) as db:
        papers = db.query(Paper).all()
        print(f"Total papers: {len(papers)}")
        
        blocked_count = 0
        ready_count = 0
        
        for paper in papers:
            # Check artifact status
            status = build_paper_artifact_status(paper, settings=settings)
            is_ready = status["artifact_ready_for_external_audit"]
            if is_ready:
                ready_count += 1
                continue
            
            blocked_count += 1
            print(f"\n--- Paper {paper.id} ---")
            print(f"Title: {paper.title}")
            print(f"DB pdf_path: {paper.pdf_path}")
            print(f"DB markdown_path: {paper.markdown_path}")
            print(f"DB docling_json_path: {paper.docling_json_path}")
            print(f"DB tei_path: {paper.tei_path}")
            print(f"DB workspace_path: {getattr(paper, 'workspace_path', None)}")
            
            pdf_res = resolve_persisted_artifact_path(paper.pdf_path, category="pdf", settings=settings)
            print(f"Resolved PDF: {pdf_res} (Exists: {pdf_res.exists() if pdf_res else False})")
            
            md_res = resolve_persisted_artifact_path(paper.markdown_path, category="markdown", settings=settings)
            print(f"Resolved MD: {md_res} (Exists: {md_res.exists() if md_res else False})")
            
            print(f"Blocking errors: {status['blocking_errors']}")
            
            # Stop after printing a few to avoid spam
            if blocked_count >= 5:
                print("\n... skipping remaining blocked papers for now.")
                break
                
        print(f"\nReady: {ready_count}, Blocked: {blocked_count} (out of {len(papers)})")

if __name__ == '__main__':
    run_audit()

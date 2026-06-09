import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.db.session import session_scope
from app.db.models import Paper
from app.config import get_settings
from app.utils.artifact_status import build_paper_artifact_status
from app.services.paper_workbench_service import PaperWorkbenchService

def recover_workspaces():
    settings = get_settings()
    
    with session_scope(settings.database_url) as db:
        papers = db.query(Paper).all()
        workbench = PaperWorkbenchService(db, settings)
        
        recovered = 0
        for paper in papers:
            status = build_paper_artifact_status(paper, settings=settings)
            if "missing_ai_reading_package" in status["blocking_errors"]:
                print(f"Recovering workspace for Paper {paper.id}...")
                try:
                    workbench.prepare_paper_workspace(paper.id, render_pages=False)
                    recovered += 1
                except Exception as e:
                    print(f"  Failed for {paper.id}: {e}")
                    
        print(f"\nRecovered {recovered} workspaces.")

if __name__ == '__main__':
    recover_workspaces()

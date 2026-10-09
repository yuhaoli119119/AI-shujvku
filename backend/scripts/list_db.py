import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from app.db.session import session_scope
from app.db.models import Paper
from app.config import get_settings

def list_db_papers():
    settings = get_settings()
    with session_scope(settings.database_url) as db:
        papers = db.query(Paper).all()
        print("DB Papers:")
        for p in papers:
            print(f"- {p.id} | {p.doi} | {p.pdf_path}")

if __name__ == '__main__':
    list_db_papers()

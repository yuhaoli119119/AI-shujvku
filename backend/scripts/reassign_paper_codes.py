import sys
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(backend_dir))

from sqlalchemy import select
from app.db.session import session_scope
from app.db.models import Paper
from app.config import get_settings
from app.services.paper_codes import paper_code_prefix, format_paper_code

def run():
    settings = get_settings()
    with session_scope(settings.database_url) as session:
        papers = session.scalars(
            select(Paper).order_by(Paper.created_at, Paper.id)
        ).all()
        
        # Clear existing to avoid unique constraint violations during shuffle
        for paper in papers:
            paper.paper_code = None
        session.flush()
            
        global_max = 0
        used_codes = set()
        
        for paper in papers:
            prefix = paper_code_prefix(paper.paper_type)
            number = global_max + 1
            code = format_paper_code(prefix, number)
            while code in used_codes:
                number += 1
                code = format_paper_code(prefix, number)
            
            paper.paper_code = code
            used_codes.add(code)
            global_max = number
            
        session.commit()
        print(f"Successfully reassigned paper_codes for {len(papers)} papers.")

if __name__ == '__main__':
    run()

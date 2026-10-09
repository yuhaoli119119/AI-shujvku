"""Read-only upload/job history for the website; no lifecycle repair on reads."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WorkflowJob
from app.db.session import get_db_session
from app.utils.library_names import build_library_name_clause, normalize_library_name

router = APIRouter()


@router.get('')
def list_website_jobs(job_type: str | None = Query(None, alias='type'),
                      library_name: str | None = None, status: str | None = None,
                      limit: int = Query(80,ge=1,le=200), session: Session = Depends(get_db_session)):
    query = select(WorkflowJob)
    if job_type:
        query = query.where(WorkflowJob.type == job_type)
    if library_name is not None:
        query = query.where(build_library_name_clause(WorkflowJob.library_name, library_name))
    if status:
        if status not in {'active','queued','running','completed','failed','cancelled'}:
            raise HTTPException(400, detail='Unsupported workflow job status')
        query = query.where(WorkflowJob.status.in_(['queued','running']) if status == 'active' else WorkflowJob.status == status)
    with session.no_autoflush:
        rows = session.scalars(query.order_by(WorkflowJob.created_at.desc(), WorkflowJob.job_id).limit(limit))
        return [{name:getattr(row,name) for name in ('job_id','type','status','progress','result','error',
                 'created_at','updated_at','payload')} | {'library_name':normalize_library_name(row.library_name)} for row in rows]

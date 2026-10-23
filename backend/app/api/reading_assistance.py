from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.db.models import Paper
from app.schemas.reading_assistance import ReadingAssistanceSave, AbstractTranslationSave
from app.security.session_auth import SessionState
from app.api.rebuild import _require_section_write_session, _require_work_package_session
from app.services import reading_assistance as service

router=APIRouter(prefix='/api/rebuild/papers',tags=['reading-assistance'])


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1,max_length=128)
    retry: bool = False
    source_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    scope: Literal['full','abstract_only'] = 'full'


@router.get('/{paper_id}/reading-assistance')
def read_saved(paper_id:UUID,session:Session=Depends(get_db_session),auth:SessionState=Depends(_require_work_package_session)):
    paper=session.get(Paper,paper_id)
    if not paper:raise HTTPException(404,'paper_not_found')
    return {'paper_id':str(paper_id),'abstract_zh':(paper.comprehensive_analysis or {}).get('abstract_zh'),
            'abstract_invalid_notice':service.invalid_translation(paper),'abstract_original':paper.abstract,'abstract_translation':(paper.reading_guide or {}).get('abstract_translation'),'guide':(paper.reading_guide or {}).get('beginner_zh'),
            'job':service.serialize(service.latest(session,paper_id))}


@router.get('/{paper_id}/reading-assistance/context')
def read_context(paper_id:UUID,session:Session=Depends(get_db_session),settings:Settings=Depends(get_settings),auth:SessionState=Depends(_require_work_package_session)):
    try:return service.input_context(session,paper_id,settings)
    except ValueError as exc:raise HTTPException(404,str(exc)) from exc


@router.post('/{paper_id}/reading-assistance/jobs')
async def generate(paper_id:UUID,body:GenerateRequest,session:Session=Depends(get_db_session),settings:Settings=Depends(get_settings),auth:SessionState=Depends(_require_section_write_session)):
    try:return await service.create(session,paper_id,settings,body.request_id,body.retry,body.scope,source_job_id=body.source_job_id,actor=auth.username)
    except ValueError as exc:
        session.rollback();raise HTTPException(409,str(exc)) from exc


@router.post('/{paper_id}/reading-assistance')
def save_reading(paper_id:UUID,body:ReadingAssistanceSave | AbstractTranslationSave,session:Session=Depends(get_db_session),settings:Settings=Depends(get_settings),auth:SessionState=Depends(_require_section_write_session)):
    try:
        result=service.save(session,paper_id,body,settings,auth.username);session.commit();return result
    except ValueError as exc:
        session.rollback();raise HTTPException(409,str(exc)) from exc

"""Explicit, sourced copy of a historical value; original records remain untouched."""
import hashlib
import json
from typing import Literal, Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.db.models import Paper, DFTResult, ElectrochemicalPerformance, RebuildDataRow
from app.schemas.rebuild import RebuildValueSourceInput, RebuildDataRowInput, ReactionType, ActiveSiteType
from app.services.rebuild_workflow_service import RebuildWorkflowError, import_data_row, serialize_row, get_file

class LegacyTransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    legacy_kind: Literal['dft','electrochemical']
    legacy_id: UUID
    legacy_field: str = 'value'
    field_name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]*$', max_length=128)
    unit: str | None = Field(default=None, max_length=64)
    reaction: ReactionType
    material: str = Field(min_length=1)
    support: str | None = None
    active_site: str | None = None
    active_site_type: ActiveSiteType
    condition: dict[str, Any] = Field(default_factory=dict)
    source: RebuildValueSourceInput
    reason: str = Field(min_length=1)
    request_id: UUID


def transfer_legacy_value(session, paper_id, payload, actor):
    if session.scalar(select(Paper.id).where(Paper.id==paper_id).with_for_update()) is None:
        raise RebuildWorkflowError('paper_not_found')
    model=DFTResult if payload.legacy_kind=='dft' else ElectrochemicalPerformance
    record=session.get(model,payload.legacy_id)
    if record is None or record.paper_id != paper_id:
        raise RebuildWorkflowError('legacy_record_not_in_paper')
    allowed={'value'} if payload.legacy_kind=='dft' else {'capacity_value','sulfur_loading_mg_cm2','sulfur_content_wt_percent','cycle_number','decay_per_cycle'}
    if payload.legacy_field not in allowed:
        raise RebuildWorkflowError('legacy_field_not_supported')
    number=getattr(record,payload.legacy_field)
    upper = record.value_upper if payload.legacy_kind == 'dft' else None
    if not payload.reason.strip() or not payload.material.strip():
        raise RebuildWorkflowError('legacy_transfer_reason_and_material_required')
    if not payload.source.file_id or not payload.source.page_number:
        raise RebuildWorkflowError('legacy_transfer_registered_file_and_page_required')
    if get_file(session,payload.source.file_id).paper_id != paper_id:
        raise RebuildWorkflowError('source_file_belongs_to_other_paper')
    fingerprint=hashlib.sha256(json.dumps(payload.model_dump(mode='json'),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    key=f'legacy-transfer:{payload.legacy_kind}:{payload.legacy_id}:{payload.legacy_field}'
    previous=session.scalar(select(RebuildDataRow).where(RebuildDataRow.paper_id==paper_id,RebuildDataRow.row_key==key))
    if previous:
        meta=(previous.properties or {}).get('_legacy_transfer',{})
        if meta.get('request_id')==str(payload.request_id) and meta.get('fingerprint')==fingerprint:
            return {'status':'replayed','row':serialize_row(session,previous)}
        raise RebuildWorkflowError('legacy_transfer_already_exists')
    unit=record.unit if payload.legacy_kind=='dft' else payload.unit
    value_type=record.value_kind if payload.legacy_kind=='dft' and record.value_kind in {'explicit','estimated','derived'} else 'explicit'
    if number is None and upper is None:
        value_type = 'missing'
    raw_value = None if value_type == 'missing' else str(number) if upper is None else f'{number if number is not None else "未报告下界"} ～ {upper}'
    numeric_value = number if upper is None and value_type != 'missing' else None
    if upper is not None and value_type in {'estimated', 'derived'}:
        raise RebuildWorkflowError('legacy_estimated_range_requires_separate_bound_verification')
    if value_type in {'estimated', 'derived'} and not payload.source.estimate_basis:
        raise RebuildWorkflowError('legacy_estimate_basis_required')
    row=RebuildDataRowInput(row_key=key,material=payload.material,reaction=payload.reaction,
        support=payload.support,active_site=payload.active_site,active_site_type=payload.active_site_type,
        data_type='dft' if payload.legacy_kind=='dft' else 'experimental',condition=payload.condition,
        notes=payload.reason,properties={'_legacy_transfer':{'kind':payload.legacy_kind,'id':str(payload.legacy_id),
            'field':payload.legacy_field,'request_id':str(payload.request_id),'fingerprint':fingerprint,
            'original_value':number,'original_upper':upper,'original_evidence':record.evidence_text,'reason':payload.reason,'actor':actor}},
        values=[{'field_name':payload.field_name,'raw_value':raw_value,'numeric_value':numeric_value,'unit':unit,
                 'missing_reason':'历史记录未提供数值' if value_type=='missing' else None,
                 'value_type':value_type,'sources':[payload.source.model_dump(mode='json')]}])
    result=import_data_row(session,paper_id,row)
    if not result.get('complete'):
        raise RebuildWorkflowError('legacy_transfer_requires_complete_identity_and_source')
    target=session.get(RebuildDataRow,UUID(result['row_id']))
    return {'status':'transferred','row':serialize_row(session,target)}

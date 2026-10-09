"""GET compatibility for the mature DFT/visuals pages, without old workflows."""
from collections import Counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.config import get_settings
from app.security.owner import require_owner_request
from app.services.analysis_readonly import AnalysisReadonlyService, POLICY
from app.services.catalyst_analysis_service import CatalystAnalysisService

router = APIRouter()


def export_policy(request):
    enabled=get_settings().exports_enabled
    effective=enabled
    if not enabled:
        try:
            require_owner_request(request)
            effective=True
        except HTTPException:
            pass
    return {'exports_enabled':enabled,'effective_exports_enabled':effective,
            'owner_exception_policy':'existing_owner_authentication','assessment_status':'not_evaluated'}


def options(source: Literal['legacy','rebuild']='legacy', library_name: str | None=None,
            property_type: str | None=None, adsorbate: str | None=None,
            catalyst_name: str | None=None, catalyst_type: str | None=None,
            year_min: int | None=None, year_max: int | None=None,
            min_confidence: float | None=Query(None,ge=0,le=1),
            reaction: str | None=None, material_family: str | None=None,
            paper_id: str | None=None, include_estimated: bool=False):
    return dict(source=source,library_name=library_name,include_estimated=include_estimated,
                filters=dict(property_type=property_type,adsorbate=adsorbate,catalyst_name=catalyst_name,
                             catalyst_type=catalyst_type,year_min=year_min,year_max=year_max,
                             min_confidence=min_confidence,reaction=reaction,material_family=material_family,paper_id=paper_id))


def service(args,session):
    return AnalysisReadonlyService(session,**args)


def exploratory(mode):
    if mode in {'approved','exportable','eligible','validated','reviewed','trusted'}:
        raise HTTPException(409,detail={'code':'approval_not_evaluated','assessment_status':'not_evaluated',
                                       'message':'Stored scientific approval has not been evaluated by this read-only projection. Use explicit exploratory mode to inspect observations.'})
    if mode not in {'exploratory','all','needs_review'}:
        raise HTTPException(422,detail='status/export_mode must be exploratory or approved')


@router.get('/papers/compare')
def compare(status: str='exploratory',offset: int=Query(0,ge=0),limit: int=Query(25,ge=1,le=500),
            sort: Literal['value','property_mix','catalyst_group']='catalyst_group', compact: bool=False,
            args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(status)
    return service(args,session).compare(offset=offset,limit=limit,sort=sort)


@router.get('/papers/analysis-summary')
def summary(request: Request,args=Depends(options),session: Session=Depends(get_db_session)):
    svc=service(args,session); result=svc.summary()
    records=svc.records
    result.update(export_policy=export_policy(request),catalyst_groups=dict(Counter(r['catalyst_name'] or '未绑定' for r in records)),
                  property_types=dict(Counter(r['property_type'] or '未记录' for r in records)),
                  adsorbates=dict(Counter(r['adsorbate'] or '未记录' for r in records)),
                  total_dft_results=len(records),total_papers=len({r['paper_id'] for r in records}))
    return result


@router.get('/papers/export/dft-dataset')
def dataset(export_mode: str='exploratory',limit: int | None=Query(None,ge=1,le=5000),
            offset: int=Query(0,ge=0),dataset_profile: str | None=None,
            args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(export_mode)
    try:
        return service(args,session).observation_dataset(limit=limit,offset=offset,dataset_profile=dataset_profile)
    except ValueError as exc:
        raise HTTPException(422,detail=str(exc)) from exc


@router.get('/papers/export/csv')
def export_csv(export_mode: str='exploratory',limit: int | None=Query(None,ge=1,le=5000),
               offset: int=Query(0,ge=0),dataset_profile: str | None=None,
               args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(export_mode)
    try:
        data,payload=service(args,session).csv(limit=limit,offset=offset,dataset_profile=dataset_profile)
    except ValueError as exc:
        raise HTTPException(422,detail=str(exc)) from exc
    return Response(data,media_type='text/csv; charset=utf-8',headers={
        'Content-Disposition':'attachment; filename="dft_exploratory.csv"',
        'X-Analysis-Mode':'exploratory','X-Approval-Assessment':'not_evaluated',
        'X-Source-Family':args['source'],'X-Exploratory-Row-Count':str(len(payload['records']))})


@router.get('/dft/catalyst-dataset')
def catalyst_dataset(export_mode: str='exploratory',args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(export_mode)
    return service(args,session).catalyst_dataset(args['library_name'])


@router.get('/dft/catalyst-dataset.csv')
def catalyst_csv(export_mode: str='exploratory',args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(export_mode)
    data,payload=service(args,session).catalyst_dataset_csv(args['library_name'])
    # Every CSV remains explicitly exploratory even when legacy status tokens
    # look approved. The companion JSON manifest contains cell-level sources.
    return Response(data,media_type='text/csv; charset=utf-8',headers={
        'Content-Disposition':'attachment; filename="dft_catalyst_exploratory.csv"',
        'X-Analysis-Mode':'exploratory','X-Approval-Assessment':'not_evaluated',
        'X-Source-Family':args['source'],'X-Exploratory-Row-Count':str(payload['row_count'])})


@router.get('/visuals/analysis-fields')
def fields(request: Request,args=Depends(options),session: Session=Depends(get_db_session)):
    return {'schema_version':'dft_catalyst_analysis_fields_v1','fields':service(args,session).field_registry(),
            'selection_policy':POLICY,'assessment_status':'not_evaluated','export_policy':export_policy(request)}


@router.get('/visuals/catalyst-correlation')
def correlation(x_field: str,y_field: str,min_n: int=Query(3,ge=3,le=50),
                args=Depends(options),session: Session=Depends(get_db_session)):
    try:
        return service(args,session).correlation(library_name=args['library_name'],x_field=x_field,y_field=y_field,min_n=min_n)
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc)) from exc


@router.get('/visuals/correlation-pairs')
def pairs(target_property: str,descriptor: str,min_n: int=Query(3,ge=3,le=50),allow_exploratory: bool=True,
          args=Depends(options),session: Session=Depends(get_db_session)):
    if not allow_exploratory:
        exploratory('approved')
    try:
        payload=service(args,session).correlation(library_name=args['library_name'],x_field=descriptor,y_field=target_property,min_n=min_n)
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc)) from exc
    payload['n']=payload['n_catalysts']
    # Original matrix mini-plot expects scalar x/y; preserve their detailed
    # candidates and source ids under x_detail/y_detail.
    payload['points']=[{**p,'x_detail':p['x'],'y_detail':p['y'],'x':p['x']['value'],'y':p['y']['value']} for p in payload['points']]
    return payload


@router.get('/visuals/overview')
def overview(request: Request,sections: str='overview,correlation',corr_min_n: int=Query(3,ge=3,le=50),
             corr_reaction: str | None=None,corr_adsorbate: str | None=None,corr_family: str | None=None,
             corr_allow_exploratory: bool=True,matrix_status: str='all',
             args=Depends(options),session: Session=Depends(get_db_session)):
    exploratory(matrix_status)
    requested=set(sections.split(','))
    if not requested <= {'overview','correlation','matrix'}:
        raise HTTPException(422,detail='Unsupported overview section')
    filters=dict(args['filters'])
    filters.update({k:v for k,v in {'reaction':corr_reaction,'adsorbate':corr_adsorbate,'material_family':corr_family}.items() if v})
    svc=service({**args,'filters':filters},session)
    counts=svc.summary()
    response={'source':args['source'],'library_name':args['library_name'],'included_sections':sorted(requested),
              'assessment_status':'not_evaluated','summary':counts,'catalyst_analysis_meta':counts,'export_policy':export_policy(request)}
    if 'overview' in requested:
        response.update(years=[{'year':k,'count':v} for k,v in Counter(r['year'] for r in svc.records).items()],
                        journals=[{'journal':k or '未记录期刊','count':v} for k,v in Counter(r['journal'] for r in svc.records).items()],
                        dft_status=[{'status':k,'count':v} for k,v in Counter(r['candidate_status'] for r in svc.records).items()])
    if 'correlation' in requested or 'matrix' in requested:
        if not corr_allow_exploratory:
            exploratory('approved')
        response['descriptor_correlation']=svc.correlation_matrix(args['library_name'],min_n=corr_min_n)
        response['descriptor_correlation'].update(source=args['source'],assessment_status='not_evaluated')
    return response

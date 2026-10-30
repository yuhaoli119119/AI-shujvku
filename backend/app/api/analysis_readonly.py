"""GET compatibility for the mature DFT/visuals pages, without old workflows."""
from collections import Counter
import gzip
import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.db.session import get_db_session
from app.config import get_settings
from app.security.owner import require_owner_request
from app.services.analysis_readonly import AnalysisReadonlyService, POLICY, _content_revision
from app.services.catalyst_analysis_service import CatalystAnalysisService
from app.services.analysis_visual_chart import (
    chart_cache_key,
    compact_correlation_payload,
    read_cached_svg,
    render_correlation_svg,
    write_cached_svg,
)

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


def options(source: Literal['legacy','rebuild']='rebuild', library_name: str | None=None,
            property_type: str | None=None, adsorbate: str | None=None,
            catalyst_name: str | None=None, catalyst_type: str | None=None,
            year_min: int | None=None, year_max: int | None=None,
            min_confidence: float | None=Query(None,ge=0,le=1),
            reaction: str | None=None, material_family: str | None=None,
            paper_id: str | None=None, include_estimated: bool=False, include_unlabelled: bool=False,
            data_type: Literal['dft','experimental']='dft'):
    if source == 'legacy':
        raise HTTPException(410, detail={'code':'legacy_extraction_retired',
            'message':'旧提取链路已停用；数据库、分析和导出只使用新 AI 提取数据。',
            'source':'rebuild'})
    return dict(source=source,library_name=library_name,include_estimated=include_estimated,include_unlabelled=include_unlabelled,
                filters=dict(property_type=property_type,adsorbate=adsorbate,catalyst_name=catalyst_name,
                             catalyst_type=catalyst_type,year_min=year_min,year_max=year_max,
                             min_confidence=min_confidence,reaction=reaction,material_family=material_family,paper_id=paper_id,data_type=data_type))


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
    svc=service(args,session)
    def build():
        result=svc.summary()
        records=svc.records
        result.update(catalyst_groups=dict(Counter(r['catalyst_name'] or '未绑定' for r in records)),
                      property_types=dict(Counter(r['property_type'] or '未记录' for r in records)),
                      adsorbates=dict(Counter(r['adsorbate'] or '未记录' for r in records)),
                      total_dft_results=len(records),total_papers=len({r['paper_id'] for r in records}))
        return result
    result=svc.cached_output('summary', {}, build)
    result['export_policy']=export_policy(request)
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
    svc=service(args,session)
    return {'schema_version':'dft_catalyst_analysis_fields_v1','fields':svc.cached_output('fields', {}, svc.field_registry),
            'selection_policy':POLICY,'assessment_status':'not_evaluated','export_policy':export_policy(request)}


@router.get('/visuals/data-revision')
def data_revision(args=Depends(options),session: Session=Depends(get_db_session)):
    return service(args,session).data_revision()


def scientific_response(request, result):
    encodings = [item.strip().lower() for item in request.headers.get('accept-encoding','').split(',')]
    def accepts_gzip(item):
        parts = [part.strip() for part in item.split(';')]
        if parts[0] != 'gzip':
            return False
        for part in parts[1:]:
            if part.startswith('q='):
                try:
                    return float(part[2:]) > 0
                except ValueError:
                    return False
        return True
    if any(accepts_gzip(item) for item in encodings):
        body=json.dumps(jsonable_encoder(result),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        return Response(gzip.compress(body,compresslevel=5),media_type='application/json',headers={'Content-Encoding':'gzip','Vary':'Accept-Encoding'})
    return JSONResponse(content=jsonable_encoder(result),headers={'Vary':'Accept-Encoding'})


@router.get('/visuals/catalyst-correlation/render.svg')
def correlation_svg(request: Request,x_field: str,y_field: str,min_n: int=Query(3,ge=3,le=50),
                    args=Depends(options),session: Session=Depends(get_db_session)):
    try:
        x_field=x_field.strip().lower()
        y_field=y_field.strip().lower()
        cache_enabled=args['source']=='rebuild' and session.get_bind().dialect.name=='postgresql'
        revision=_content_revision(session) if cache_enabled else None
        cache_key=chart_cache_key({'source':args['source'],'library_name':args['library_name'],'filters':args['filters'],
                                   'include_estimated':args['include_estimated'],'include_unlabelled':args['include_unlabelled'],
                                   'x_field':x_field,'y_field':y_field,'min_n':min_n,'revision':revision}) if cache_enabled else None
        cache_dir=get_settings().storage_root / '.visual-render-cache'
        svg=read_cached_svg(cache_dir,cache_key) if cache_key else None
        if svg is None:
            revision_stable=not cache_enabled
            for _attempt in range(3):
                # A service instance retains its first successful projection.
                # After a revision change, retry from a fresh object so the new
                # revision cannot be paired with stale in-memory rows.
                svc=service(args,session)
                payload=svc.cached_output('correlation', {'x':x_field,'y':y_field,'min_n':min_n},
                                          lambda: svc.correlation(library_name=args['library_name'],x_field=x_field,y_field=y_field,min_n=min_n))
                if not cache_enabled:
                    break
                current_revision=_content_revision(session)
                if current_revision==revision:
                    revision_stable=True
                    break
                revision=current_revision
                cache_key=chart_cache_key({'source':args['source'],'library_name':args['library_name'],'filters':args['filters'],
                                           'include_estimated':args['include_estimated'],'include_unlabelled':args['include_unlabelled'],
                                           'x_field':x_field,'y_field':y_field,'min_n':min_n,'revision':revision})
            if not revision_stable:
                raise HTTPException(409,detail='analysis_data_changed_during_render')
            svg=render_correlation_svg(payload,x_field=x_field,y_field=y_field)
            if cache_key:
                write_cached_svg(cache_dir,cache_key,svg)
        etag='"'+hashlib.sha256(svg.encode('utf-8')).hexdigest()+'"'
        headers={'ETag':etag,'Cache-Control':'private, no-cache, max-age=0, must-revalidate',
                 'X-Correlation-Render':'static-svg','X-Analysis-Mode':'exploratory',
                 'X-Content-Type-Options':'nosniff',
                 'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; sandbox"}
        if request.headers.get('if-none-match')==etag:
            return Response(status_code=304,headers=headers)
        return Response(content=svg,media_type='image/svg+xml; charset=utf-8',headers=headers)
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc)) from exc


@router.get('/visuals/catalyst-correlation')
def correlation(request: Request,x_field: str,y_field: str,min_n: int=Query(3,ge=3,le=50),
                view: Literal['full','compact']='full',
                args=Depends(options),session: Session=Depends(get_db_session)):
    try:
        svc=service(args,session)
        result=svc.cached_output('correlation', {'x':x_field,'y':y_field,'min_n':min_n}, lambda: svc.correlation(library_name=args['library_name'],x_field=x_field,y_field=y_field,min_n=min_n))
        if view=='compact':
            result=compact_correlation_payload(result)
        response=scientific_response(request, result)
        response.headers['Cache-Control']='private, no-cache'
        return response
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc)) from exc


@router.get('/visuals/correlation-pairs')
def pairs(request: Request,target_property: str,descriptor: str,min_n: int=Query(3,ge=3,le=50),allow_exploratory: bool=True,
          args=Depends(options),session: Session=Depends(get_db_session)):
    if not allow_exploratory:
        exploratory('approved')
    try:
        svc=service(args,session)
        payload=svc.cached_output('correlation', {'x':descriptor,'y':target_property,'min_n':min_n}, lambda: svc.correlation(library_name=args['library_name'],x_field=descriptor,y_field=target_property,min_n=min_n))
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc)) from exc
    payload['n']=payload['n_catalysts']
    # Original matrix mini-plot expects scalar x/y; preserve their detailed
    # candidates and source ids under x_detail/y_detail.
    payload['points']=[{**p,'x_detail':p['x'],'y_detail':p['y'],'x':p['x']['value'],'y':p['y']['value']} for p in payload['points']]
    return scientific_response(request, payload)


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
    def build():
        counts=svc.summary()
        response={'source':args['source'],'library_name':args['library_name'],'included_sections':sorted(requested),
                  'assessment_status':'not_evaluated','summary':counts,'catalyst_analysis_meta':counts}
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
    response=svc.cached_output('overview', {'sections':sorted(requested),'min_n':corr_min_n,'allow_exploratory':corr_allow_exploratory}, build)
    response['export_policy']=export_policy(request)
    return scientific_response(request, response)

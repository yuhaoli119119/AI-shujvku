"""Source plan metadata in isolated PostgreSQL; never production science."""
import copy
import hashlib
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4, UUID
import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from app.db.models import (Base, AuditLog, Paper, PaperSection, RebuildPaperFile, RebuildVisualAsset,
                           RebuildDataRow, RebuildDataValue, RebuildValueSource, WorkflowJob)
from app.main import app
from app.services.extraction_coverage import EVENT
from test_rebuild_mutation_audit import audit_env, row_body


def evidence(name, value):
    path=os.getenv('COVERAGE_EVIDENCE_ROOT')
    if path:
        root=Path(path);root.mkdir(parents=True,exist_ok=True)
        (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def endpoint(env): return f'/api/rebuild/papers/{env["paper"]}/coverage-plan'
def snapshot(env):
    with env['engine'].connect() as connection:
        return {table.name:sorted(connection.execute(text(f'SELECT row_to_json(t)::text FROM "{table.name}" t')).scalars())
                for table in Base.metadata.sorted_tables}
def disk(env):
    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in env['settings'].storage_root.rglob('*') if p.is_file()}
def get(env, suffix=''):
    response=env['client'].get(endpoint(env)+suffix); assert response.status_code==200,response.text; return response.json()
def preview(env, body):
    response=env['client'].post(endpoint(env)+'/preview',json=body);assert response.status_code==200,response.text;return response.json()
def save(env, body):
    response=env['client'].put(endpoint(env),json=body);assert response.status_code==200,response.text;return response.json()
def body(env):
    state=get(env)
    return {'request_id':str(uuid4()),'expected_plan_revision':state['plan_revision'],
            'expected_output_revision':state['output_revision'],'files':copy.deepcopy(env['files']), 'items':copy.deepcopy(env['items'])}

@pytest.fixture
def coverage_env(audit_env):
    env=audit_env
    root=env['settings'].storage_root
    with env['factory']() as session:
        main=session.get(RebuildPaperFile,env['file']);main.role='main'
        with fitz.open() as doc:
            for i in range(3):doc.new_page().insert_text((30,40),f'Synthetic SI page {i+1}, Table S1, Figure 1a.')
            doc.save(root/'si.pdf')
        content=(root/'si.pdf').read_bytes()
        si=RebuildPaperFile(paper_id=env['paper'],role='si',storage_path='si.pdf',original_filename='si.pdf',
            sha256=hashlib.sha256(content).hexdigest(),file_size=len(content));session.add(si);session.flush()
        with fitz.open(root/'fixture.pdf') as doc:doc[0].get_pixmap().save(root/'figure.png')
        assets=[]
        for file,pages,kind,key in [(main,[1],'figure','main-fig'),(si,[3],'subfigure','si-panel'),(si,[1,2],'table','si-table')]:
            asset=RebuildVisualAsset(paper_id=env['paper'],file_id=file.id,asset_key=key,asset_type=kind,figure_label='Figure 1' if kind!='table' else 'Table S1',
                subfigure_label='a' if kind=='subfigure' else None,page_numbers=pages,
                image_path='figure.png' if kind!='table' else None,explanation='Synthetic interpretation of original source image.',
                provenance={'table_data':{'columns':['material','energy'],'rows':[['A','0.0100']]},
                    'table_sources':[{'file_id':str(si.id),'page_number':p,'role':'body','locator':'Synthetic Table S1 body'} for p in [1,2]]} if kind=='table' else {})
            session.add(asset);session.flush();assets.append(str(asset.id))
        session.commit(); si_id=str(si.id);main_hash=main.sha256
    payload=row_body(env);payload['rows'][0].update(material='A',condition={'potential':1})
    payload['rows'].append(copy.deepcopy(payload['rows'][0]));payload['rows'][1].update(row_key='material-b',material='B',condition={'potential':2})
    response=env['client'].post('/api/rebuild/rows/import',json=payload);assert response.status_code==200,response.text
    rows=response.json()['results']
    with env['factory']() as session:
        values={str(v.row_id):v for v in session.scalars(select(RebuildDataValue))}
        sources={str(s.row_id):s for s in session.scalars(select(RebuildValueSource))}
        links=[{'row_id':r['row_id'],'row_key':r['row_key'],'value_id':str(values[r['row_id']].id),'source_id':str(sources[r['row_id']].id)} for r in rows]
    sections={'request_id':'synthetic-section','sections':[{'file_id':str(env['file']),'source_sha256':main_hash,'section_key':'all-main',
        'page_basis':'source_pdf','section_title':'All text pages','text':'Synthetic original text, not an image extraction.',
        'explanation_zh':'合成原文解读，不能代替图表提取。','page_start':1,'page_end':2,'sources':[{'page_number':p,'quote':'Synthetic','locator':f'page {p}'} for p in [1,2]]}]}
    resp=env['client'].post(f'/api/rebuild/papers/{env["paper"]}/sections/import',json=sections);assert resp.status_code==200,resp.text
    reading=get(env)
    env['files']=[{'file_id':f['file_id'],'source_sha256':f['source_sha256'],'reading_pdf_sha256':f['reading_pdf_sha256'],
                   'declared_pages':list(range(1,f['physical_pages']+1)),'scope_note':'Synthetic fixture original source explicitly reviewed.'}
                  for f in env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()['files']]
    def item(key,kind,file,pages,link=None,**extra):
        return dict(item_key=key,title=f'Synthetic expected {key}',kind=kind,source={'file_id':file,'pages':pages,'locator':'Original synthetic object'},
                    state='extracted' if link else 'pending',**({'link':link} if link else {'reason':{'code':'not_processed','detail':'Declared source object awaiting processing'}}),**extra)
    env['items']=[item('material-a','value',str(env['file']),[2],links[0],identity={'material':'A'},condition={'potential':1},field_name='adsorption_energy_ev'),
                  item('material-b','value',str(env['file']),[2],links[1],identity={'material':'B'},condition={'potential':2},field_name='adsorption_energy_ev'),
                  item('figure-main','figure',str(env['file']),[1],{'asset_id':assets[0]},label='Figure 1'),
                  item('table-si','table',si_id,[1,2],{'asset_id':assets[2]},label='Table S1',expected_columns=['material','energy']),
                  item('subfigure-si','subfigure',si_id,[3],None,label='Figure 1',subfigure_label='a')]
    with env['factory']() as session:section_id=str(session.scalar(select(PaperSection.id)))
    env.update(si=si_id,assets=assets,links=links,section_id=section_id)
    return env


def test_specific_missing_object_not_hidden_by_other_results_or_all_main_sections(coverage_env):
    env=coverage_env;request=body(env);before=snapshot(env);files=disk(env)
    checked=preview(env,request);assert checked['valid'],checked['errors']
    assert checked['summary']['counts']=={'extracted':4,'pending':1}
    assert snapshot(env)==before and disk(env)==files
    receipt=save(env,request);after=snapshot(env)
    for table,records in before.items():
        if table!='audit_logs':assert after[table]==records,table
    assert len(after['audit_logs'])==len(before['audit_logs'])+1
    plan=get(env,'?limit=2');assert plan['summary']['total']==5 and len(plan['items'])==2
    assert plan['scientific_acceptance']=='not_implied' and plan['summary']['status']=='partial'
    all_items=[];page=plan
    while True:
        all_items+=page['items']
        if not page['next_page']:break
        resp=env['client'].get(page['next_page']);assert resp.status_code==200,resp.text;page=resp.json()
    evidence('full-plan',get(env));evidence('first-page',plan);evidence('all-items',all_items)
    assert len({i['item_key'] for i in all_items})==5
    assert [i['item_key'] for i in all_items if i['effective_state']=='pending']==['subfigure-si']
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    evidence('quality',quality)
    assert any(i['code']=='coverage_plan_item_missing' and i['id']=='subfigure-si' for i in quality['issues'])
    assert plan['output_revision']==request['expected_output_revision'] # metadata never self-invalidates
    before=snapshot(env);get(env);preview(env,{**request,'expected_plan_revision':receipt['plan_revision']})
    assert snapshot(env)==before
    assert save(env,request)['replayed'] and snapshot(env)==before
    changed=copy.deepcopy(request);changed['items'][0]['title']='changed body'
    assert env['client'].put(endpoint(env),json=changed).status_code==409


def test_states_and_undeclared_scope_never_infer_full_inventory(coverage_env):
    env=coverage_env;assert get(env)['legacy_without_plan'] and get(env)['summary']['status']=='unknown'
    request=body(env);request['files']=request['files'][:1];fid=request['files'][0]['file_id'];request['files'][0]['declared_pages']=[1]
    request['items']=[]
    for state in ['not_reported','not_applicable','unknown','pending']:
        request['items'].append({'item_key':state,'title':state,'kind':'figure','source':{'file_id':fid,'pages':[1],'locator':'Synthetic checked region'},
            'state':state,'reason':{'code':state,'detail':'Honest synthetic scope statement','checked_pages':[1],'method':'Read original PDF region visually'}})
    assert preview(env,request)['valid'];save(env,request);plan=get(env)
    assert plan['summary']['status']=='unknown' and plan['summary']['scope_unknown']
    assert len(plan['summary']['files'])==2 and any(f['undeclared_pages'] for f in plan['summary']['files'])
    assert set(plan['summary']['counts'])=={'not_reported','not_applicable','unknown','pending'}
    bad=body(env);bad['items']=[request['items'][0]];del bad['items'][0]['reason']['method']
    assert env['client'].post(endpoint(env)+'/preview',json=bad).status_code==422


@pytest.mark.parametrize('fault,code',[
 ('file','item_file_not_declared'),('page','item_pages_outside_declared_scope'),('hash','file_identity_or_hash_unverified'),('readinghash','file_identity_or_hash_unverified'),
 ('row','value_row_mismatch'),('material','material_identity_mismatch'),('condition','condition_mismatch'),
 ('column','value_source_file_page_or_cell_mismatch'),('columns','table_columns_mismatch_or_unverified'),
 ('assettype','asset_type_mismatch'),('assetfile','asset_file_mismatch'),('duplicate','duplicate_item_key'),
 ('reuse','output_reused_for_distinct_plan_items'),('imagepending','image_processing_still_pending'),
 ('nolink','row_not_in_this_paper')])
def test_invalid_exact_correspondence_rejects_metadata_without_any_scientific_change(coverage_env,fault,code):
    env=coverage_env;request=body(env)
    if fault=='file':request['items'][0]['source']['file_id']=str(uuid4())
    if fault=='page':request['items'][0]['source']['pages']=[99]
    if fault=='hash':request['files'][0]['source_sha256']='0'*64
    if fault=='readinghash':request['files'][0]['reading_pdf_sha256']='0'*64
    if fault=='row':request['items'][0]['link']['row_id']=env['links'][1]['row_id'];request['items'][0]['link'].pop('row_key')
    if fault=='material':request['items'][0]['identity']['material']='B'
    if fault=='condition':request['items'][0]['condition']['potential']=2
    if fault=='column':request['items'][0]['source']['table_column']=99
    if fault=='columns':request['items'][3]['expected_columns']=['wrong-column','energy']
    if fault=='assettype':request['items'][2]['kind']='subfigure'
    if fault=='assetfile':request['items'][2]['source']['file_id']=env['si']
    if fault=='duplicate':request['items'].append(copy.deepcopy(request['items'][0]))
    if fault=='reuse':request['items'].append({**copy.deepcopy(request['items'][0]),'item_key':'borrowed-result'})
    if fault=='imagepending':request['items'][2]['origin']='image_pending'
    if fault=='nolink':request['items'][0]['link']={}
    before=snapshot(env);files=disk(env);result=preview(env,request)
    assert not result['valid'] and any(e['code']==code for e in result['errors']),result['errors']
    response=env['client'].put(endpoint(env),json=request);assert response.status_code==422,response.text
    assert snapshot(env)==before and disk(env)==files


def test_stale_output_plan_and_pagination_cas_not_accepted(coverage_env):
    env=coverage_env;request=body(env);receipt=save(env,request);old=get(env,'?limit=1')
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality?limit=1').json()
    # A plan-only update changes quality pagination but not the scientific hash.
    update=body(env);update['items'][-1]['reason']['detail']='Still awaiting original image interpretation';save(env,update)
    resp=env['client'].get(old['next_page']);assert resp.status_code==409
    assert env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality?expected_revision={quality["output_revision"]}&expected_coverage_revision={quality["quality_revision"]}').status_code==409
    assert env['client'].put(endpoint(env),json={**request,'request_id':'stale-cas-new-id'}).status_code==409
    with env['factory']() as session:
        value=session.get(RebuildDataValue,UUID(env['links'][0]['value_id']));value.raw_value='0.0200';value.numeric_value=.02;session.commit()
    current=get(env);evidence('stale-plan',current);assert current['summary']['stale_items']==4
    assert all(i['effective_state']=='unknown' for i in current['items'] if i['state']=='extracted')
    assert env['client'].get(old['next_page']).status_code==409
    stale=copy.deepcopy(update);stale['request_id']='old-output-new-id';stale['expected_plan_revision']=current['plan_revision']
    assert env['client'].put(endpoint(env),json=stale).status_code==409
    assert current['scientific_acceptance']=='not_implied'


def test_source_hash_change_unknown_readonly_and_auth_guards(coverage_env):
    env=coverage_env;request=body(env);save(env,request)
    with (env['settings'].storage_root/'fixture.pdf').open('ab') as stream:stream.write(b'\nSynthetic changed bytes')
    before=snapshot(env);files=disk(env);plan=get(env)
    assert plan['summary']['status']=='unknown' and plan['summary']['stale_items']>0
    assert snapshot(env)==before and disk(env)==files
    with TestClient(app) as anon:assert anon.get(endpoint(env)).status_code==401
    csrf=env['client'].headers.pop('X-CSRF-Token')
    assert env['client'].put(endpoint(env),json=request).status_code==403
    assert env['client'].post(endpoint(env)+'/preview',json=request).status_code==403
    env['client'].headers['X-CSRF-Token']=csrf
    assert env['client'].put(endpoint(env),json=request,headers={'Origin':'https://evil.invalid'}).status_code==403
    request['items'][0]['condition']['value']=float('nan')
    assert env['client'].put(endpoint(env),content=json.dumps(request),headers={'Content-Type':'application/json'}).status_code==422
    assert snapshot(env)==before


def test_representative_large_plan_bounded_pages_and_queries(coverage_env):
    env=coverage_env;request=body(env);template=request['items'][-1]
    request['items']=[{**copy.deepcopy(template),'item_key':f'expected-{i:04d}','title':f'Synthetic declared object {i}'} for i in range(1000)]
    save(env,request);queries=[]
    def capture(conn,cursor,statement,*args):queries.append(statement)
    event.listen(env['engine'],'before_cursor_execute',capture)
    start=perf_counter()
    try:page=get(env,'?limit=25')
    finally:event.remove(env['engine'],'before_cursor_execute',capture)
    elapsed=perf_counter()-start
    assert page['summary']['total']==1000 and len(page['items'])==25
    assert page['summary']['counts']['pending']==1000 and page['has_more']
    assert len(queries)<30,queries
    assert len(json.dumps(page))<55000
    print('COVERAGE_PERFORMANCE',json.dumps({'items':1000,'page_items':25,'queries':len(queries),'response_bytes':len(json.dumps(page)),'seconds':elapsed}))


def test_full_current_links_structure_pass_is_not_acceptance_and_section_is_separate(coverage_env):
    env=coverage_env;request=body(env)
    request['items'][-1].update(state='extracted',link={'asset_id':env['assets'][1]});request['items'][-1].pop('reason')
    request['items'].append({'item_key':'original-section','title':'Full original main text','kind':'section',
        'source':{'file_id':str(env['file']),'pages':[1,2],'locator':'Original whole section'},'state':'extracted','link':{'section_id':env['section_id']}})
    before=snapshot(env);checks=preview(env,request);assert checks['valid'],checks['errors']
    assert checks['summary']['status']=='declared_items_accounted_for'
    assert checks['scientific_acceptance']=='not_implied'
    save(env,request);state=get(env);assert state['summary']['counts']['extracted']==6
    assert state['declaration']['actor']=='writer'
    assert snapshot(env)['workflow_jobs']==before['workflow_jobs']
    assert snapshot(env)['papers']==before['papers']
    evidence('all-current',state)


def test_foreign_output_file_and_same_label_distinct_file_are_not_interchangeable(coverage_env):
    env=coverage_env
    with env['factory']() as session:
        other=Paper(title='Foreign synthetic paper',pdf_path='foreign.pdf');session.add(other);session.flush()
        foreign=RebuildVisualAsset(paper_id=other.id,file_id=None,asset_type='figure',asset_key='foreign',page_numbers=[1])
        session.add(foreign);session.commit();foreign_id=str(foreign.id)
        si_figure=RebuildVisualAsset(paper_id=env['paper'],file_id=UUID(env['si']),asset_type='figure',asset_key='same-label-si',
            figure_label='Figure 1',page_numbers=[1],image_path='figure.png',explanation='Independent SI figure interpretation')
        session.add(si_figure);session.commit();si_figure_id=str(si_figure.id)
    request=body(env);request['items'][2]['link']['asset_id']=foreign_id
    assert any(e['code']=='asset_not_in_this_paper' for e in preview(env,request)['errors'])
    request=body(env);copy_item=copy.deepcopy(request['items'][2]);copy_item.update(item_key='same-label-other-file')
    copy_item['source']['file_id']=env['si'];copy_item['link']['asset_id']=si_figure_id;request['items'].append(copy_item)
    assert preview(env,request)['valid'] # same Figure 1 label never merges main and SI


def test_stable_key_cannot_retarget_and_source_metadata_failure_is_atomic(coverage_env):
    env=coverage_env;request=body(env);save(env,request)
    update=body(env);update['items'][0]['identity']['material']='B'
    before=snapshot(env)
    assert env['client'].put(endpoint(env),json=update).status_code==422
    assert snapshot(env)==before
    update=body(env);update['items'][-1]['reason']['detail']='Metadata-only update, no scientific changes'
    def fail(conn,cursor,statement,*args):
        if statement.startswith('INSERT INTO audit_logs'):raise RuntimeError('synthetic metadata event failure')
    event.listen(env['engine'],'before_cursor_execute',fail)
    try:assert env['client'].put(endpoint(env),json=update).status_code==500
    finally:event.remove(env['engine'],'before_cursor_execute',fail)
    assert snapshot(env)==before
    assert save(env,update)['metadata_only']


def test_declared_cell_and_section_source_changes_are_detected(coverage_env):
    env=coverage_env;request=body(env)
    request['items'][0]['source'].update(table_row=0,table_column=1)
    assert preview(env,request)['valid']
    with env['factory']() as session:
        src=session.get(RebuildValueSource,UUID(env['links'][0]['source_id']));src.table_column=0;session.commit()
    request=body(env);request['items'][0]['source'].update(table_row=0,table_column=1)
    assert not preview(env,request)['valid']


def test_preview_id_unreserved_actor_bound_and_legacy_empty_text_not_absence(coverage_env):
    env=coverage_env;request=body(env);before=snapshot(env)
    assert preview(env,request)['valid'];assert preview(env,request)['valid'];assert snapshot(env)==before
    saved=save(env,request);before=snapshot(env)
    other=env['client_for']('other')
    try:assert other.put(endpoint(env),json=request).status_code==409
    finally:other.close()
    assert snapshot(env)==before
    empty=body(env);empty['items']=[];save(env,empty)
    assert get(env)['summary']['status']=='unknown' # no items is never evidence of a complete source inventory


def test_canonical_request_fingerprint_keeps_defaults_and_arbitrary_condition_keys(coverage_env):
    env=coverage_env;request=body(env);save(env,request)
    changed=copy.deepcopy(request);changed['items'][0]['origin']='reported'
    assert env['client'].put(endpoint(env),json=changed).status_code==409
    update=body(env);update['items'][-1]['item_key']='new-condition-declaration'
    update['items'][-1]['condition']={'access_token':'synthetic-condition-a'}
    # Non-extracted expectation may carry explicit conditions; hashing never redacts them.
    save(env,update)
    changed=copy.deepcopy(update);changed['items'][-1]['condition']['access_token']='synthetic-condition-b'
    assert env['client'].put(endpoint(env),json=changed).status_code==409
    assert get(env,'?limit=1')['next_page']
    assert env['client'].get(endpoint(env)+'?offset=1').status_code==400
    assert env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality?offset=1').status_code==400


def test_large_linked_plan_uses_batched_graph_not_per_item_queries(coverage_env):
    from test_extraction_quality_efficiency import seed_rows
    env=coverage_env;seed_rows(env,250)
    with env['factory']() as session:
        rows={r.material:r for r in session.scalars(select(RebuildDataRow))}
        values={str(v.row_id):v for v in session.scalars(select(RebuildDataValue))}
        sources={str(s.value_id):s for s in session.scalars(select(RebuildValueSource))}
        # Expectations are explicitly generated fixture declarations, not a
        # claim that a production result scan discovered a full source list.
        declared=[]
        for i in range(250):
            row=rows[f'fixture-{i}'];value=values[str(row.id)];source=sources[str(value.id)]
            declared.append({'item_key':f'fixture-fact-{i:04d}','title':f'Synthetic original fact {i}','kind':'value',
                'source':{'file_id':str(env['file']),'pages':[1],'locator':f'Synthetic source object {i}'},
                'identity':{'material':f'fixture-{i}'},'condition':{},'field_name':'adsorption_energy_ev','state':'extracted',
                'link':{'row_id':str(row.id),'value_id':str(value.id),'source_id':str(source.id)}})
    request=body(env);request['items']=declared;save(env,request);queries=[]
    def capture(conn,cursor,statement,*args):queries.append(statement)
    event.listen(env['engine'],'before_cursor_execute',capture);start=perf_counter()
    try:result=get(env,'?limit=25')
    finally:event.remove(env['engine'],'before_cursor_execute',capture)
    elapsed=perf_counter()-start
    assert result['summary']['counts']['extracted']==250 and result['summary']['total']==250
    assert len(queries)<35 and len(json.dumps(result))<55000
    print('LINKED_COVERAGE_PERFORMANCE',json.dumps({'items':250,'page_items':25,'queries':len(queries),
        'response_bytes':len(json.dumps(result)),'seconds':elapsed}))


def test_blank_pdf_text_does_not_create_not_reported_or_plan(coverage_env):
    env=coverage_env;path=env['settings'].storage_root/'blank.pdf'
    with fitz.open() as doc:doc.new_page();doc.save(path)
    with env['factory']() as session:
        record=RebuildPaperFile(paper_id=env['paper'],role='si',storage_path='blank.pdf',original_filename='blank.pdf',
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),file_size=path.stat().st_size)
        session.add(record);session.commit();file_id=str(record.id)
    before=snapshot(env);plan=get(env)
    assert plan['legacy_without_plan'] and plan['summary']['status']=='unknown'
    blank=next(f for f in plan['summary']['files'] if f['file_id']==file_id)
    assert blank['undeclared_pages']==[1]
    assert not plan['items'] and not plan['summary']['counts'].get('not_reported')
    assert snapshot(env)==before


def test_origin_and_absence_checks_never_borrow_other_result_types(coverage_env):
    env=coverage_env;request=body(env)
    request['items']=[{'item_key':'row-a','title':'Material state A','kind':'row','identity':{'material':'A'},'condition':{'potential':1},
        'source':{'file_id':str(env['file']),'pages':[2],'locator':'Synthetic A state'},'origin':'estimated',
        'state':'extracted','link':{'row_id':env['links'][0]['row_id']}}]
    assert not preview(env,request)['valid'] # an explicit fact is not an estimated fact
    request['items'][0]['origin']='reported';assert preview(env,request)['valid']
    request=body(env);request['items'][-1].update(state='not_reported',reason={'code':'not_reported','detail':'No value in checked source region',
        'checked_pages':[3],'method':'Explicit original visual inspection'})
    save(env,request)
    with env['factory']() as session:
        row=session.get(RebuildDataRow,UUID(env['links'][0]['row_id']));row.notes='Later evidence';session.commit()
    plan=get(env);absence=next(i for i in plan['items'] if i['item_key']=='subfigure-si')
    assert absence['freshness']=='stale' and absence['effective_state']=='unknown'


def test_unstructured_asset_condition_cannot_satisfy_declared_condition(coverage_env):
    env=coverage_env
    with env['factory']() as session:
        asset=session.get(RebuildVisualAsset,UUID(env['assets'][0]));asset.material_mapping={'material':'A','condition':'potential'};session.commit()
    request=body(env);request['items'][2].update(identity={'material':'A'},condition={'potential':1})
    before=snapshot(env);result=preview(env,request)
    assert not result['valid'] and any(e['code']=='asset_condition_mismatch' for e in result['errors'])
    assert snapshot(env)==before


@pytest.mark.parametrize('fault',['label','source_asset_file'])
def test_explicit_source_object_cannot_borrow_a_different_label_or_file(coverage_env,fault):
    env=coverage_env
    if fault=='source_asset_file':
        with env['factory']() as session:
            source=session.get(RebuildValueSource,UUID(env['links'][0]['source_id']));source.asset_id=UUID(env['assets'][2]);session.commit()
    request=body(env)
    if fault=='label':request['items'][0]['label']='A different original source object'
    before=snapshot(env);checks=preview(env,request)
    assert not checks['valid'] and any(e['code']=='value_source_file_page_or_cell_mismatch' for e in checks['errors'])
    assert env['client'].put(endpoint(env),json=request).status_code==422
    assert snapshot(env)==before


@pytest.mark.parametrize('index',[0,2,3],ids=['material-condition','whole-figure','whole-table'])
def test_each_declared_kind_missing_is_identified_even_when_output_already_exists(coverage_env,index):
    env=coverage_env;request=body(env)
    request['items'][-1].update(state='extracted',link={'asset_id':env['assets'][1]});request['items'][-1].pop('reason')
    missing=request['items'][index];missing.pop('link');missing.update(state='pending',reason={'code':'not_processed','detail':'Source object declared but not yet reconciled to output'})
    save(env,request);plan=get(env)
    assert plan['summary']['status']=='partial' and plan['summary']['counts']=={'extracted':4,'pending':1}
    assert [i['item_key'] for i in plan['items'] if i['effective_state']=='pending']==[missing['item_key']]
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert any(i['code']=='coverage_plan_item_missing' and i['id']==missing['item_key'] for i in quality['issues'])


def test_metadata_save_appends_exact_event_without_rewriting_prior_audit(coverage_env):
    from app.services.extraction_coverage import VERSION, _digest
    env=coverage_env;request=body(env);before=snapshot(env);files=disk(env)
    receipt=save(env,request);after=snapshot(env)
    old=set(before['audit_logs']);new=set(after['audit_logs'])
    assert old<=new and len(new-old)==1
    added=json.loads(next(iter(new-old)))
    assert added['action']==EVENT and added['source']==VERSION
    assert added['payload']['request']==request and added['payload']['request_sha256']==_digest(request)
    assert added['payload']['actor']=='writer' and added['id']==receipt['metadata_event_id']
    for table in before:
        if table!='audit_logs':assert before[table]==after[table],table
    assert disk(env)==files


@pytest.mark.parametrize('fault',['unresolved_conflict','numeric_disagreement'])
def test_row_and_value_share_legal_fact_check_and_good_sibling_can_prove_row(coverage_env,fault):
    env=coverage_env
    with env['factory']() as session:
        value=session.get(RebuildDataValue,UUID(env['links'][0]['value_id']))
        if fault=='unresolved_conflict':value.conflict={'status':'value_conflict','candidates':[]}
        else:value.numeric_value=999
        session.commit()
    request=body(env);row=request['items'][0];row['kind']='row';row.pop('field_name')
    row['link']={k:v for k,v in row['link'].items() if k in {'row_id','row_key'}};request['items']=[row]
    assert not preview(env,request)['valid']
    with env['factory']() as session:
        good=RebuildDataValue(paper_id=env['paper'],row_id=UUID(row['link']['row_id']),field_name='another_legal_fact',value_type='explicit',raw_value='2.00',numeric_value=2)
        session.add(good);session.flush()
        session.add(RebuildValueSource(paper_id=env['paper'],row_id=good.row_id,value_id=good.id,source_key='another-legal-source',source_kind='table',file_id=env['file'],page_number=2,quote='2.00'))
        session.commit()
    request['expected_output_revision']=get(env)['output_revision']
    assert preview(env,request)['valid'] # material-state existence, not all row properties complete


def test_removed_key_is_reserved_across_history_but_unchanged_reappearance_is_allowed(coverage_env):
    env=coverage_env;first=body(env);save(env,first)
    second=body(env);second['items']=[];save(env,second)
    third=body(env);third['items']=[copy.deepcopy(first['items'][-1])]
    third['items'][0]['identity']={'material':'a different object'}
    before=snapshot(env);assert not preview(env,third)['valid']
    assert env['client'].put(endpoint(env),json=third).status_code==422 and snapshot(env)==before
    third['items']=[copy.deepcopy(first['items'][-1])];assert preview(env,third)['valid'];save(env,third)


def test_precise_readonly_output_details_for_nonfirst_page_row_and_all_object_kinds(coverage_env):
    from test_extraction_quality_efficiency import seed_rows
    env=coverage_env;seed_rows(env,65)
    response=env['client'].get(f'/api/rebuild/rows?paper_id={env["paper"]}&limit=50&offset=0').json()
    first_ids={r['id'] for r in response['items']}
    target=env['client'].get(f'/api/rebuild/rows?paper_id={env["paper"]}&limit=50&offset=50').json()['items'][-1]
    assert target['id'] not in first_ids
    value=target['values'][0];source=value['sources'][0]
    request=body(env);request['items']=[{'item_key':'nonfirst-target','title':'Precise nonfirst-page scientific fact','kind':'value',
        'identity':{'material':target['material']},'condition':target['condition'],'field_name':value['field_name'],
        'source':{'file_id':source['file_id'],'pages':[source['page_number']],'locator':'Synthetic nonfirst target'},
        'state':'extracted','link':{'row_id':target['id'],'row_key':target['row_key'],'value_id':value['id'],'source_id':source['id']}}]
    save(env,request);plan=get(env);before=snapshot(env);files=disk(env)
    path=endpoint(env)+f'/items/nonfirst-target/output?expected_revision={plan["revision"]}'
    detail=env['client'].get(path);assert detail.status_code==200,detail.text;detail=detail.json()
    assert detail['output']['row']['id']==target['id'] and detail['output']['value']['id']==value['id']
    assert detail['output']['source']['id']==source['id'] and detail['read_only']
    assert snapshot(env)==before and disk(env)==files
    evidence('nonfirst-plan',plan);evidence('detail-row',detail);evidence('first-row-page-ids',sorted(first_ids))
    with TestClient(app) as anon:assert anon.get(path).status_code==401
    assert env['client'].get(endpoint(env)+f'/items/missing/output?expected_revision={plan["revision"]}').status_code==404
    request=body(env);request['items']=[copy.deepcopy(i) for i in env['items'] if i['kind'] in {'figure','table'}]
    request['items'].append({'item_key':'section-target','title':'Precise original section','kind':'section',
        'source':{'file_id':str(env['file']),'pages':[1,2],'locator':'Original section'},'state':'extracted','link':{'section_id':env['section_id']}})
    save(env,request);objects=get(env);details={}
    for item in objects['items']:
        r=env['client'].get(endpoint(env)+f'/items/{item["item_key"]}/output?expected_revision={objects["revision"]}')
        assert r.status_code==200,r.text;details[item['item_key']]=r.json()
    assert details['figure-main']['output']['asset']['id']==env['assets'][0]
    assert details['table-si']['output']['asset']['table_content']['data']['columns']==['material','energy']
    assert details['section-target']['output']['section']['id']==env['section_id']
    evidence('object-plan',objects);evidence('object-details',details)
    if os.getenv('COVERAGE_EVIDENCE_ROOT'):
        Path(os.getenv('COVERAGE_EVIDENCE_ROOT'),'fixture-figure.png').write_bytes((env['settings'].storage_root/'figure.png').read_bytes())
    assert env['client'].get(path).status_code==409


def test_plan_scope_does_not_replace_or_activate_formal_acceptance_scope(coverage_env):
    env=coverage_env;before=snapshot(env)['workflow_jobs'];save(env,body(env))
    assert not get(env)['summary']['scope_unknown']
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    formal=[i for i in quality['issues'] if i['code']=='file_extraction_coverage_unknown']
    assert len(formal)==2 and all('正式验收范围声明' in i['action_zh'] and '原文清单范围声明' in i['action_zh'] for i in formal)
    assert all(f['scope_kind']=='formal_acceptance_declaration' for f in quality['dimensions']['file_extraction_coverage'])
    assert snapshot(env)['workflow_jobs']==before


def test_row_fact_source_parent_must_match_the_fact_row(coverage_env):
    env=coverage_env
    with env['factory']() as session:
        source=session.get(RebuildValueSource,UUID(env['links'][0]['source_id']));source.row_id=UUID(env['links'][1]['row_id']);session.commit()
    request=body(env);row=request['items'][0];row['kind']='row';row.pop('field_name')
    row['link']={k:v for k,v in row['link'].items() if k in {'row_id','row_key'}};request['items']=[row]
    before=snapshot(env);assert not preview(env,request)['valid'];assert snapshot(env)==before

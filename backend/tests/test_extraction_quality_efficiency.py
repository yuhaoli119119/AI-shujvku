"""Real PostgreSQL/API behavior and query budgets; synthetic science only."""
from uuid import uuid4

from sqlalchemy import event, select, text

from app.db.models import (Paper, PaperSection, RebuildDataRow, RebuildDataValue,
                           RebuildValueSource, RebuildVisualAsset, AuditLog)
from app.services.rebuild_workflow_service import list_rows, serialize_row, import_data_row
from app.schemas.rebuild import RebuildDataRowInput
from test_rebuild_mutation_audit import audit_env, row_body
from test_rebuild_acceptance import base, freeze_body


def package(env):
    return f'/api/rebuild/papers/{env["paper"]}/ai-work-package'


def seed_rows(env, count):
    with env["factory"]() as session:
        for i in range(count):
            row = RebuildDataRow(paper_id=env["paper"], row_key=f'row-{i}', material=f'fixture-{i}', reaction='SRR',
                                data_type='dft', active_site_type='dual_atom', condition={}, properties={})
            session.add(row); session.flush()
            value = RebuildDataValue(paper_id=env["paper"], row_id=row.id, field_name='adsorption_energy_ev',
                value_type='explicit', raw_value='0.0100', numeric_value=.01, unit='eV')
            session.add(value); session.flush()
            session.add(RebuildValueSource(paper_id=env["paper"], row_id=row.id, value_id=value.id,
                source_key=f'source-{i}', source_kind='table', file_id=env["file"], page_number=1, quote='0.0100'))
        session.commit()


def test_batch_page_matches_existing_serializer_and_constant_query_budget(audit_env):
    env = audit_env; seed_rows(env, 130)
    statements = []
    def capture(*args): statements.append(args[2])
    event.listen(env["engine"], 'before_cursor_execute', capture)
    try:
        with env["factory"]() as session:
            page = list_rows(session, paper_id=env['paper'], limit=100)
            assert len(statements) == 5  # count, rows, values, sources, papers
            before = page['items']
            for item in before:
                row = session.get(RebuildDataRow, __import__('uuid').UUID(item['id']))
                assert serialize_row(session, row) == item
    finally:
        event.remove(env["engine"], 'before_cursor_execute', capture)
    assert len(before) == 100 and before[0]['values'][0]['raw_value'] == '0.0100'


def test_all_views_paginate_exactly_and_revision_change_is_409(audit_env):
    env = audit_env; seed_rows(env, 51)
    with env['factory']() as session:
        for i in range(7):
            session.add(PaperSection(paper_id=env['paper'], section_title=f'Original {i}', text='Full original text. '*100))
            session.add(RebuildVisualAsset(paper_id=env['paper'], file_id=env['file'], asset_key=f'asset-{i}',
                asset_type='figure', page_numbers=[1], context_text='Original figure context. '*100))
        session.commit()
    full = env['client'].get(package(env)+'?rows_limit=20').json()
    compact = env['client'].get(package(env)+'?view=compact').json()
    assert {'assets','sections','templates','rows','files','acceptance'} <= full.keys()
    assert compact['totals'] == {'rows':51,'assets':7,'sections':7,'legacy_tables':0}
    assert len(__import__('json').dumps(compact)) < len(__import__('json').dumps(full))
    for view in ['rows','assets','sections']:
        path=package(env)+f'?view={view}&rows_limit=3&expected_revision='+compact['output_revision'];ids=[]
        while path:
            response=env['client'].get(path);assert response.status_code==200,response.text
            body=response.json();assert body['output_revision']==compact['output_revision']
            page=body['rows'] if view=='rows' else body
            assert page['offset']==len(ids)
            ids.extend(item['id'] for item in page['items']);path=page['next_page']
        assert len(ids)==len(set(ids))==compact['totals'][view]
    with env['factory']() as session:
        row=session.scalar(select(RebuildDataRow));row.notes='Changed evidence';session.commit()
    response=env['client'].get(package(env)+'?view=rows&expected_revision='+compact['output_revision'])
    assert response.status_code==409 and response.json()['detail']=='work_package_revision_changed'


def test_quality_detects_cross_graph_damage_and_blocks_complete_without_writes(audit_env):
    env=audit_env;seed_rows(env,2)
    with env['factory']() as session:
        values=list(session.scalars(select(RebuildDataValue).order_by(RebuildDataValue.id)))
        sources=list(session.scalars(select(RebuildValueSource).order_by(RebuildValueSource.id)))
        first=values[0];source=next(s for s in sources if s.value_id==first.id)
        source.row_id=next(v.row_id for v in values if v.id!=first.id)
        source.page_number=9
        first.value_type='missing';first.missing_reason=None
        session.commit()
        before={model.__tablename__:session.execute(select(model.id)).all() for model in [AuditLog,RebuildDataRow,RebuildDataValue,RebuildValueSource]}
    response=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality?limit=1');assert response.status_code==200,response.text
    result=response.json();assert result['has_more'] and len(result['issues'])==1
    codes=set();offset=0
    while True:
        r=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality?offset={offset}&limit=2&expected_revision={result["output_revision"]}').json()
        codes.update(i['code'] for i in r['issues'])
        if not r['has_more']:break
        offset=r['next_offset']
    assert {'source_value_row_paper_mismatch','physical_page_out_of_range','missing_value_reason_or_payload_invalid'} <= codes
    with env['factory']() as session:
        after={model.__tablename__:session.execute(select(model.id)).all() for model in [AuditLog,RebuildDataRow,RebuildDataValue,RebuildValueSource]}
        assert before==after
    frozen=env['client'].post(base(env)+'/freeze',json=freeze_body(env)).json()
    assert frozen['status']=='partial'
    assert any(i['code']=='stored_output_integrity_incomplete' for i in frozen['progress']['rules']['issues'])


def test_valid_missing_reason_not_made_mandatory_measurement(audit_env):
    env=audit_env
    payload=row_body(env);payload['rows'][0]['values']=[{'field_name':'never_reported','value_type':'missing','missing_reason':'原文未报告'}]
    assert env['client'].post('/api/rebuild/rows/import',json=payload).status_code==200
    result=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert not result['counts'].get('error')
    assert result['unreported_fields']=='not_required_without_source_evidence'


def test_canonical_import_does_not_rescan_unrelated_row_values(audit_env):
    env=audit_env;seed_rows(env,130)
    statements=[]
    def capture(*args):statements.append(args[2])
    event.listen(env['engine'],'before_cursor_execute',capture)
    try:
        with env['factory']() as session:
            payload=RebuildDataRowInput(row_key='row-0',material='fixture-0',reaction='SRR',data_type='dft',active_site_type='dual_atom',values=[])
            result=import_data_row(session,env['paper'],payload)
            assert result['row_key']=='row-0'
            assert len(statements)<25
            session.rollback()
    finally:event.remove(env['engine'],'before_cursor_execute',capture)


def test_work_package_read_is_database_read_only_and_auth_stays_required(audit_env):
    env=audit_env
    seed_rows(env,1)
    with env['factory']() as session:
        session.execute(text('SET TRANSACTION READ ONLY'))
        from app.services.rebuild_quality import integrity_report
        result=integrity_report(session,env['paper'],env['settings'])
        assert result['read_only'] and not result['counts'].get('error')
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        assert client.get(package(env)+'?view=compact').status_code==401
        assert client.get(f'/api/rebuild/papers/{env["paper"]}/quality').status_code==401


def test_one_request_reuses_snapshot_and_remains_repeatable_during_external_change(audit_env, monkeypatch):
    env=audit_env;seed_rows(env,2)
    env['client'].post(base(env)+'/freeze',json=freeze_body(env))
    import app.services.rebuild_acceptance as acceptance
    original=acceptance.current_snapshot;calls=[]
    def traced(*args,**kwargs):
        calls.append(True);return original(*args,**kwargs)
    monkeypatch.setattr(acceptance,'current_snapshot',traced)
    first=env['client'].get(package(env)+'?rows_limit=2').json()
    assert len(calls)==1
    changed=[]
    def write_elsewhere(conn,cursor,statement,parameters,context,executemany):
        if not changed and 'ORDER BY rebuild_data_rows.created_at DESC' in statement:
            changed.append(True)
            with env['engine'].begin() as other:
                other.execute(text('UPDATE rebuild_data_rows SET notes=:note WHERE paper_id=:paper'),
                              {'note':'external committed change','paper':env['paper']})
    event.listen(env['engine'],'before_cursor_execute',write_elsewhere)
    try:
        body=env['client'].get(package(env)+'?rows_limit=2').json()
    finally:event.remove(env['engine'],'before_cursor_execute',write_elsewhere)
    assert changed and body['output_revision']==first['output_revision']
    assert all(row['notes'] is None for row in body['rows']['items'])
    assert env['client'].get(package(env)+'?view=compact').json()['output_revision']!=first['output_revision']


def test_producer_example_is_consumable_by_actual_asset_writer_and_not_raw_completion(audit_env):
    import copy
    env=audit_env
    compact=env['client'].get(package(env)+'?view=compact').json()
    body=copy.deepcopy(compact['producer_contract']['table_example'])
    body['asset_key']='contract-example';body['file_id']=str(env['file']);body['page_numbers']=[1,2]
    for index,source in enumerate(body['provenance']['table_sources']):
        source.update(file_id=str(env['file']),page_number=index+1)
    response=env['client'].post(f'/api/rebuild/papers/{env["paper"]}/assets',json=body)
    assert response.status_code==200,response.text
    content=response.json()['asset']['table_content']
    assert content['format']=='structured' and content['data']['rows'][0]==['s','1.00s']
    assert content['data']['rows'][1][1] is None
    from app.services.ai_extract_service import _build_prompt
    with env['factory']() as session:
        prompt,_=_build_prompt(session,env['paper'],env['settings'])
    assert 'view=compact' in prompt and 'table_sources' in prompt and 'quality_endpoint' in prompt
    assert env['client'].post(base(env)+'/freeze',json=freeze_body(env)).json()['status']=='rules_checked'


def test_cross_file_source_and_changed_section_event_are_actionable(audit_env):
    from app.db.models import RebuildPaperFile
    from app.services.rebuild_sections import EVENT, VERSION
    env=audit_env;seed_rows(env,1)
    with env['factory']() as session:
        own=session.get(RebuildPaperFile,env['file'])
        extra=RebuildPaperFile(paper_id=env['paper'],role='main',storage_path=own.storage_path,
            original_filename=own.original_filename,sha256=own.sha256,file_size=own.file_size)
        session.add(extra);session.flush()
        asset=RebuildVisualAsset(paper_id=env['paper'],file_id=extra.id,asset_key='other-file',asset_type='figure',page_numbers=[1])
        session.add(asset);session.flush()
        session.scalar(select(RebuildValueSource)).asset_id=asset.id
        chapter=PaperSection(paper_id=env['paper'],section_title='Imported',text='Changed mature text',page_start=1,page_end=1)
        session.add(chapter);session.flush()
        session.add(AuditLog(paper_id=env['paper'],action=EVENT,source=VERSION,target_type='paper_section',target_id=str(chapter.id),
            payload={'section':{'text':'Original immutable text'},'content_sha256':'invalid'}))
        session.commit()
    result=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert {'source_asset_file_mismatch','section_source_or_record_changed'} <= {i['code'] for i in result['issues']}
    assert result['counts']['error']>=2
    chapters=env['client'].get(package(env)+'?view=sections').json()['items']
    assert chapters[0]['text']=='Changed mature text'
    assert chapters[0]['source_import_event']['section']['text']=='Original immutable text'


def test_b006_legacy_body_is_discoverable_without_conversion_or_foreign_borrow(audit_env):
    from app.db.models import PaperTable, RebuildPaperFile
    env=audit_env;md='| Original | Time |\n| --- | --- |\n| s | 1.00s |'
    with env['factory']() as session:
        session.get(RebuildPaperFile,env['file']).role='main'
        legacy=PaperTable(paper_id=env['paper'],caption='Table 1. Original',page=1,markdown_content=md)
        session.add(legacy)
        other=Paper(title='Not linked',pdf_path='fixture.pdf',library_name='默认文献库')
        session.add(other);session.flush()
        session.add(PaperTable(paper_id=other.id,caption='Table 1. Other paper',page=1,markdown_content='DO NOT BORROW'))
        session.commit();legacy_id=str(legacy.id)
    compact=env['client'].get(package(env)+'?view=compact').json()
    assert compact['totals']['legacy_tables']==1
    page=env['client'].get(compact['pages']['legacy_tables']).json()
    assert page['total']==1 and page['items'][0]['id']==legacy_id
    assert page['items'][0]['markdown_content']==md and not page['has_more']
    assert page['items'][0]['source_identity']['status']=='source_bound'
    with env['factory']() as session:
        session.get(PaperTable,__import__('uuid').UUID(legacy_id)).markdown_content=md+'\nChanged';session.commit()
    assert env['client'].get(compact['pages']['legacy_tables']).status_code==409


def test_quality_summary_includes_raw_tables_file_coverage_and_legacy_unknowns(audit_env):
    env=audit_env
    with env['factory']() as session:
        session.add(RebuildVisualAsset(paper_id=env['paper'],file_id=env['file'],asset_key='raw',asset_type='table',
            figure_label='S1',page_numbers=[1],context_text='raw text only'))
        session.add(PaperSection(paper_id=env['paper'],section_title='Mature legacy',text='Retain this existing original text'))
        session.commit()
    result=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert result['status']=='partial' and result['counts']['unknown']>=3
    assert {'table_representation_incomplete','file_extraction_coverage_unknown','legacy_section_file_scope_unknown'} <= {i['code'] for i in result['issues']}
    assert result['dimensions']['table_representation']['incomplete']==1
    assert result['dimensions']['section_file_scope']['legacy_unknown']==1
    assert result['issues_total']==sum(result['counts'].values())
    assert all(item.get('action_zh') for item in result['issues'])


def test_unknown_legacy_body_is_listed_but_never_marked_trusted(audit_env):
    from app.db.models import PaperTable
    env=audit_env
    with env['factory']() as session:
        session.add(PaperTable(paper_id=env['paper'],caption='Table 1. Unbound own legacy',page=1,
            markdown_content='| A | B |\n| --- | --- |\n| x | y |'))
        session.commit()  # Only registered file is SI, not the paper's original main PDF.
    body=env['client'].get(package(env)+'?view=legacy_tables').json()
    assert body['total']==1
    assert body['items'][0]['source_identity']['status']=='unverified'
    assert body['items'][0]['source_identity']['use_for_completeness'] is False


def test_derived_chapter_hash_and_each_page_quote_stay_bound(audit_env):
    import hashlib,zipfile,copy
    from app.db.models import RebuildPaperFile
    from app.services.rebuild_file_preview import preview_cache_path
    from app.services.rebuild_sections import EVENT, _digest
    env=audit_env
    path=env['settings'].storage_root/'synthetic.docx'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('[Content_Types].xml','fixture');archive.writestr('word/document.xml','fixture')
    with env['factory']() as session:
        file=session.get(RebuildPaperFile,env['file']);file.storage_path=path.name;file.sha256=hashlib.sha256(path.read_bytes()).hexdigest();file.file_size=path.stat().st_size
        derived=preview_cache_path(file,env['settings']);derived.parent.mkdir(parents=True,exist_ok=True)
        derived.write_bytes((env['settings'].storage_root/'fixture.pdf').read_bytes())
        original_hash=file.sha256;reading_hash=hashlib.sha256(derived.read_bytes()).hexdigest();session.commit()
    body={'request_id':'derived-section','sections':[{'section_key':'full-chapter','file_id':str(env['file']),
        'source_sha256':original_hash,'reading_pdf_sha256':reading_hash,'page_basis':'derived_pdf',
        'section_title':'Original chapter','text':'Complete original chapter.','explanation_zh':'夹具解释',
        'page_start':1,'page_end':2,'sources':[{'page_number':1,'locator':'paragraph1','quote':'Synthetic table'},
                                            {'page_number':2,'locator':'paragraph2','quote':'Second synthetic page'}]}]}
    result=env['client'].post(f'/api/rebuild/papers/{env["paper"]}/sections/import',json=body)
    assert result.status_code==200,result.text
    section_page=env['client'].get(package(env)+'?view=sections').json()
    item=section_page['items'][0];projected=copy.deepcopy(item['source_import_event'])
    assert 'text' not in projected['section'] and item['text']==body['sections'][0]['text']
    projected['section']['text']=item['text']
    assert _digest(projected['section'])==projected['content_sha256']
    initial=env['client'].get(package(env)+'?view=compact').json()
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert not any(i['code']=='section_source_or_record_changed' for i in quality['issues'])
    with env['factory']() as session:
        log=session.scalar(select(AuditLog).where(AuditLog.action==EVENT));data=copy.deepcopy(log.payload)
        data['section']['sources'][1]['quote']=' '
        data['content_sha256']=_digest(data['section']);log.payload=data;session.commit()
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert any(i['code']=='section_source_or_record_changed' for i in quality['issues'])
    assert env['client'].get(package(env)+'?view=compact&expected_revision='+initial['output_revision']).status_code==409
    with env['factory']() as session:
        log=session.scalar(select(AuditLog).where(AuditLog.action==EVENT));data=copy.deepcopy(log.payload)
        data['section']['sources'][1]['quote']='Second synthetic page';data['content_sha256']=_digest(data['section']);log.payload=data;session.commit()
    before=env['client'].get(package(env)+'?view=compact').json()['output_revision']
    derived.write_bytes(derived.read_bytes()+b'changed-reading-version')
    quality=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert any(i['code']=='section_source_or_record_changed' for i in quality['issues'])
    assert env['client'].get(package(env)+'?view=compact&expected_revision='+before).status_code==409


def test_unknown_original_format_cannot_borrow_a_cached_pdf_as_verified_source(audit_env):
    import hashlib
    from app.db.models import RebuildPaperFile
    from app.services.rebuild_file_preview import preview_cache_path
    env=audit_env
    path=env['settings'].storage_root/'not-docx.bin';path.write_bytes(b'not a PDF or a supported DOCX')
    with env['factory']() as session:
        file=session.get(RebuildPaperFile,env['file']);file.storage_path=path.name
        file.sha256=hashlib.sha256(path.read_bytes()).hexdigest();file.file_size=path.stat().st_size
        cache=preview_cache_path(file,env['settings']);cache.parent.mkdir(parents=True,exist_ok=True)
        cache.write_bytes((env['settings'].storage_root/'fixture.pdf').read_bytes());session.commit()
    result=env['client'].get(f'/api/rebuild/papers/{env["paper"]}/quality').json()
    assert result['files'][0]['status']=='unknown'
    assert result['files'][0]['reason']=='source_preview_format_unsupported'
    assert result['status']=='unknown' and result['counts']['unknown']>=1

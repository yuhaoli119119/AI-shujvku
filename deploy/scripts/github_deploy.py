#!/usr/bin/env python3
"""Deploy an exact GitHub commit. Plan first; apply requires its reviewed digest."""
import argparse,fcntl,hashlib,json,os,re,subprocess,uuid
from contextlib import contextmanager,nullcontext
from datetime import datetime,timezone
from pathlib import Path

REMOTE='https://github.com/yuhaoli119119/AI-shujvku.git'
PRODUCTION_RUNTIME=Path('/opt/AI-shujvku/literature-ai')
PLAN_SCHEMA_VERSION=2
SERVICES=['backend','worker','worker-pdf','owner-gateway','share-gateway','public-gateway']
RELEASE_MOUNTS={
    'backend':{'/app':'backend','/frontend':'frontend','/prompts':'prompts'},
    'worker':{'/app':'backend','/prompts':'prompts'},
    'worker-pdf':{'/app':'backend','/prompts':'prompts'},
    'owner-gateway':{'/etc/nginx/templates/default.conf.template':'deploy/nginx/owner.conf.template'},
    'share-gateway':{'/etc/nginx/conf.d/default.conf':'deploy/nginx/share.conf'},
    'public-gateway':{'/etc/nginx/conf.d/default.conf':'deploy/nginx/public.conf'},
}
ML_EXPLORER_MOUNT='/frontend/pages/ml_explorer/data'
ML_EXPLORER_BUNDLE_FILES={'app_data.json','manifest.json'}
ML_EXPLORER_SCOPE={'libraryName':'锂硫双原子','libraryScopeKey':'lithium-sulfur-dual-atom',
                   'domains':['LITHIUM_SULFUR_MODEL'],'unitCount':177948,
                   'exactMaterialContexts':5775}
def run(args,**kwargs):
    return subprocess.check_output(args,text=True,**kwargs).strip()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def plan_compose_path(state,plan_sha256):return state/f'compose.{plan_sha256}.json'
def resolve_plan_compose(state,plan_sha256):
    versioned=plan_compose_path(state,plan_sha256)
    return versioned if versioned.is_file() else state/'compose.private.json'
def private_directory(path):
    path.mkdir(parents=True,exist_ok=True)
    os.chmod(path,0o700)
    return path
def write_private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    except Exception:
        try:temporary.unlink()
        except FileNotFoundError:pass
        raise
def production_runtime(value):
    try:resolved=Path(value).resolve(strict=True)
    except FileNotFoundError as exc:raise RuntimeError('Production runtime does not exist') from exc
    expected=PRODUCTION_RUNTIME.resolve()
    if resolved!=expected:
        raise RuntimeError(f'Production runtime must resolve to {expected}')
    return resolved
def current_snapshot(runtime):
    pointer=runtime/'DEPLOYED_GITHUB_COMMIT'
    if not pointer.is_file():
        return {'commit':None,'pointer_sha256':None,'receipt_sha256':None,
                'plan_sha256':None,'compose_sha256':None}
    commit=pointer.read_text().strip()
    if not re.fullmatch('[0-9a-f]{40}',commit):
        raise RuntimeError('Invalid deployed commit pointer')
    receipt=runtime/'deploy-state'/commit/'deployment-receipt.json'
    if not receipt.is_file():raise RuntimeError('Current deployment receipt is missing')
    state=runtime/'deploy-state'/commit
    plan=state/'plan.json'
    if not plan.is_file():raise RuntimeError('Current deployment plan is missing')
    try:receipt_data=json.loads(receipt.read_text())
    except Exception as exc:raise RuntimeError('Current deployment receipt is invalid') from exc
    if receipt_data.get('commit')!=commit:raise RuntimeError('Current deployment receipt commit mismatch')
    plan_sha=digest(plan)
    compose=resolve_plan_compose(state,plan_sha)
    if not compose.is_file():raise RuntimeError('Current deployment compose is missing')
    compose_sha=digest(compose)
    if receipt_data.get('plan_sha256')!=plan_sha:raise RuntimeError('Current receipt plan digest mismatch')
    try:plan_data=json.loads(plan.read_text())
    except Exception as exc:raise RuntimeError('Current deployment plan is invalid') from exc
    if plan_data.get('commit')!=commit or plan_data.get('compose_sha256')!=compose_sha:
        raise RuntimeError('Current plan commit or compose digest mismatch')
    return {'commit':commit,'pointer_sha256':digest(pointer),'receipt_sha256':digest(receipt),
            'plan_sha256':plan_sha,'compose_sha256':compose_sha}
def assert_current(runtime,expected):
    actual=current_snapshot(runtime)
    if actual!=expected:
        raise RuntimeError(f'Current deployment changed since plan: expected {expected}, found {actual}')
    return actual
@contextmanager
def deployment_lock(runtime):
    state=private_directory(runtime/'deploy-state')
    path=state/'deployment.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Deployment lock is already held') from exc
        yield
    finally:
        try:fcntl.flock(fd,fcntl.LOCK_UN)
        finally:os.close(fd)
def application_root(checkout):
    # Support both the single-directory repository and older frozen commits.
    for app in (checkout,checkout/'literature-ai'):
        if (app/'docker-compose.yml').is_file() and (app/'backend/app/main.py').is_file():
            return app
    raise RuntimeError('GitHub commit does not contain a Literature AI application')
def ml_explorer_enabled(app):
    return (app/'frontend/pages/ml_explorer/index.html').is_file()
def validate_ml_explorer_assets(runtime,source):
    root=(runtime/'data/ml-explorer-releases').resolve()
    resolved=Path(source).resolve()
    if resolved.parent!=root or not re.fullmatch('[0-9a-f]{64}',resolved.name):
        raise RuntimeError('ML Explorer assets must come from data/ml-explorer-releases/<64 lowercase hex>')
    if not resolved.is_dir():raise RuntimeError('Missing ML Explorer asset directory')
    entries={path.name for path in resolved.iterdir()}
    if entries!=ML_EXPLORER_BUNDLE_FILES:
        raise RuntimeError('ML Explorer asset directory must contain exactly app_data.json and manifest.json')
    bundle_path=resolved/'app_data.json';manifest_path=resolved/'manifest.json'
    try:manifest=json.loads(manifest_path.read_text())
    except Exception as exc:raise RuntimeError('Invalid ML Explorer manifest') from exc
    if manifest.get('schemaVersion')!='ml-explorer-runtime.v2' or manifest.get('adapterEntry')!='data/app_data.json':
        raise RuntimeError('ML Explorer runtime manifest contract mismatch')
    bundle=manifest.get('bundle') or {}
    if bundle.get('sha256')!=digest(bundle_path) or bundle.get('bytes')!=bundle_path.stat().st_size:
        raise RuntimeError('ML Explorer bundle hash/bytes mismatch')
    if resolved.name!=bundle['sha256']:
        raise RuntimeError('ML Explorer asset directory name must equal bundle SHA-256')
    try:payload=json.loads(bundle_path.read_text())
    except Exception as exc:raise RuntimeError('Invalid ML Explorer payload') from exc
    if bundle.get('payloadSchemaVersion')!=payload.get('schemaVersion'):
        raise RuntimeError('ML Explorer payload schema mismatch')
    payload_manifest=payload.get('manifest') or {}
    if manifest.get('scope')!=ML_EXPLORER_SCOPE:
        raise RuntimeError('ML Explorer runtime scope mismatch')
    if payload_manifest.get('libraryName')!=ML_EXPLORER_SCOPE['libraryName'] or \
       payload_manifest.get('libraryScopeKey')!=ML_EXPLORER_SCOPE['libraryScopeKey'] or \
       payload_manifest.get('dataDomains')!=ML_EXPLORER_SCOPE['domains']:
        raise RuntimeError('ML Explorer payload scope mismatch')
    runtime_allow=manifest.get('allow') or {}
    payload_allow=payload_manifest.get('allow') or {}
    if not isinstance(runtime_allow,dict) or runtime_allow!=payload_allow or runtime_allow.get('domains')!=ML_EXPLORER_SCOPE['domains']:
        raise RuntimeError('ML Explorer runtime/payload allowlist mismatch')
    return {'source':str(resolved),'read_only':True,
            'bundle_sha256':bundle['sha256'],'bundle_bytes':bundle['bytes'],
            'manifest_sha256':digest(manifest_path),'manifest_bytes':manifest_path.stat().st_size}
def verify_ml_explorer_mount(runtime,info,app):
    if not ml_explorer_enabled(app):return None
    mounts=[mount for mount in info.get('Mounts',[]) if mount.get('Destination')==ML_EXPLORER_MOUNT]
    if len(mounts)!=1:raise RuntimeError('Missing or duplicate ML Explorer asset mount')
    mount=mounts[0]
    if mount.get('Type')!='bind' or mount.get('RW'):
        raise RuntimeError('ML Explorer asset mount must be read-only bind')
    return validate_ml_explorer_assets(runtime,mount.get('Source',''))
def fetch(runtime,sha):
    checkout=runtime/'releases'/sha
    if not checkout.exists():
        checkout.mkdir(parents=True)
        run(['git','init','--quiet',str(checkout)])
        run(['git','-C',str(checkout),'remote','add','origin',REMOTE])
    if run(['git','-C',str(checkout),'remote','get-url','origin'])!=REMOTE:raise RuntimeError('Unexpected Git origin')
    run(['git','-C',str(checkout),'fetch','--no-tags','--depth=1','origin',sha],stderr=subprocess.PIPE)
    if run(['git','-C',str(checkout),'rev-parse','FETCH_HEAD'])!=sha:raise RuntimeError('Commit mismatch')
    try:head=run(['git','-C',str(checkout),'rev-parse','HEAD'],stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:head=None
    if head is None:run(['git','-C',str(checkout),'checkout','--detach','--quiet',sha])
    elif head!=sha:raise RuntimeError('Release directory belongs to another commit')
    if run(['git','-C',str(checkout),'status','--porcelain','--untracked-files=all']):raise RuntimeError('Release checkout is dirty')
    return application_root(checkout)
def configuration(runtime,app,sha):
    raw=run(['docker','compose','-p','literature-ai','--project-directory',str(runtime),'--env-file',str(runtime/'.env'),'-f',str(app/'docker-compose.yml'),'config','--format','json'],stderr=subprocess.PIPE)
    cfg=json.loads(raw)
    frozen_image=app/'deploy/runtime-image.json'
    if frozen_image.is_file():
        image=json.loads(frozen_image.read_text())
        actual=run(['docker','image','inspect',image['local_tag'],'--format','{{.Id}}'])
        if actual!=image['image_id']:raise RuntimeError('Frozen dependency image changed; refuse server image drift')
        if digest(app/'backend/requirements.txt')!=image['requirements_sha256']:raise RuntimeError('Requirements changed without updating the pinned dependency image')
    for name,service in cfg['services'].items():
        if 'build' in service:
            service['build']['context']=str(app)
            if frozen_image.is_file():service['build']['dockerfile']='backend/Dockerfile.frozen'
        if name not in SERVICES:continue
        service.setdefault('labels',{})['org.opencontainers.image.revision']=sha
        if name in {'backend','worker','worker-pdf'}:service.setdefault('environment',{})['LITAI_GIT_COMMIT']=sha
        for volume in service.get('volumes',[]):
            if volume.get('type')!='bind':continue
            path=Path(volume['source'])
            try:relative=path.relative_to(runtime)
            except ValueError:continue
            if relative.parts[0] in {'backend','frontend','prompts','deploy'} and not path.name.endswith('.htpasswd'):
                replacement=app/relative
                if not replacement.exists():raise RuntimeError('Missing GitHub-controlled mount: '+str(relative))
                volume['source']=str(replacement);volume['read_only']=True
    private_assets=None
    if ml_explorer_enabled(app):
        backend_mounts=[volume for volume in cfg.get('services',{}).get('backend',{}).get('volumes',[]) if volume.get('target')==ML_EXPLORER_MOUNT]
        if len(backend_mounts)!=1:
            raise RuntimeError('Backend must mount ML Explorer assets exactly once')
        mount=backend_mounts[0]
        if mount.get('type')!='bind' or not mount.get('read_only'):
            raise RuntimeError('ML Explorer asset mount must be read-only bind')
        private_assets=validate_ml_explorer_assets(runtime,mount.get('source',''))
    cfg['_litai_private_assets']=private_assets
    return cfg
def build_plan(runtime,app,sha,expected_current):
    cfg=configuration(runtime,app,sha)
    private_assets=cfg.pop('_litai_private_assets',None)
    raw=json.dumps(cfg,sort_keys=True,indent=2)+'\n'
    config_hash=hashlib.sha256(raw.encode()).hexdigest()
    plan={'schema_version':PLAN_SCHEMA_VERSION,'commit':sha,'remote':REMOTE,'code_root':str(app),
          'expected_current':expected_current,
          'rollback':{'commit':expected_current['commit'],
                      'plan_sha256':expected_current['plan_sha256'],
                      'compose_sha256':expected_current['compose_sha256'],
                      'receipt_sha256':expected_current['receipt_sha256']},
          'services_to_recreate':SERVICES,
          'durable_data_root':str(runtime/'data'),'credential_file':str(runtime/'.env'),
          'compose_sha256':config_hash,'database_services_recreated':[],
          'private_assets':private_assets,
          'code_mounts':{s:[v for v in cfg['services'][s].get('volumes',[]) if v.get('source','').startswith(str(app))] for s in SERVICES}}
    plan_raw=json.dumps(plan,sort_keys=True,indent=2)+'\n'
    return plan,plan_raw,raw,hashlib.sha256(plan_raw.encode()).hexdigest()
def archive_plan(state,new_plan_sha256,reason,expected_old_plan_sha256,writer=write_private):
    if not reason or not reason.strip():raise RuntimeError('Plan rotation reason is required')
    plan_file=state/'plan.json'
    if not plan_file.is_file():raise RuntimeError('Existing plan file is required for rotation')
    old_plan_sha=digest(plan_file)
    config_file=resolve_plan_compose(state,old_plan_sha)
    if not plan_file.is_file() or not config_file.is_file():
        raise RuntimeError('Existing plan and compose files are required for rotation')
    if not expected_old_plan_sha256 or old_plan_sha!=expected_old_plan_sha256:
        raise RuntimeError('Old plan digest does not match the reviewed rotation target')
    history=private_directory(state/'plan-history'/old_plan_sha)
    for source in (plan_file,config_file):
        target=history/source.name
        data=source.read_text()
        if target.exists() and target.read_text()!=data:
            raise RuntimeError('Existing archived plan differs from plan being rotated')
        if not target.exists():writer(target,data)
    rotation=history/'rotation.json'
    record={'archived_at_utc':datetime.now(timezone.utc).isoformat(),
            'old_plan_sha256':old_plan_sha,'new_plan_sha256':new_plan_sha256,
            'reason':reason.strip()}
    if rotation.exists():
        existing=json.loads(rotation.read_text())
        immutable=('old_plan_sha256','new_plan_sha256','reason')
        if any(existing.get(key)!=record[key] for key in immutable):
            raise RuntimeError('Existing plan rotation record differs; preserve it and inspect manually')
    else:writer(rotation,json.dumps(record,sort_keys=True,indent=2)+'\n')
    return {'archived_plan':str(history/'plan.json'),'old_plan_sha256':old_plan_sha}
def reviewed_plan(runtime,sha,plan_sha256):
    state=runtime/'deploy-state'/sha
    plan_file=state/'plan.json'
    if not plan_file.is_file() or digest(plan_file)!=plan_sha256:
        raise RuntimeError('Exact reviewed plan digest required')
    try:plan=json.loads(plan_file.read_text())
    except Exception as exc:raise RuntimeError('Reviewed plan is invalid') from exc
    if plan.get('schema_version')!=PLAN_SCHEMA_VERSION:
        raise RuntimeError('Legacy plan has no deployment CAS schema; rotate it explicitly before review')
    if plan.get('commit')!=sha or plan.get('remote')!=REMOTE:
        raise RuntimeError('Reviewed plan identity mismatch')
    expected=plan.get('expected_current')
    if not isinstance(expected,dict) or set(expected)!={'commit','pointer_sha256','receipt_sha256','plan_sha256','compose_sha256'}:
        raise RuntimeError('Reviewed plan expected-current contract is invalid')
    rollback=plan.get('rollback') or {}
    if not isinstance(rollback,dict) or rollback!={'commit':expected['commit'],
            'plan_sha256':expected['plan_sha256'],'compose_sha256':expected['compose_sha256'],
            'receipt_sha256':expected['receipt_sha256']}:
        raise RuntimeError('Reviewed plan rollback contract is invalid')
    config_file=resolve_plan_compose(state,plan_sha256)
    if not config_file.is_file():raise RuntimeError('Reviewed compose configuration is missing')
    return plan,config_file,plan['compose_sha256']
def verify_release_mounts(name,info,app):
    expected=RELEASE_MOUNTS[name]
    mounts={mount.get('Destination'):mount for mount in info.get('Mounts',[])}
    for destination,relative in expected.items():
        mount=mounts.get(destination)
        if mount is None:raise RuntimeError(f'Missing release mount: {name} {destination}')
        source=str(app/relative)
        actual=str(Path(mount.get('Source','')).resolve())
        if mount.get('Type')!='bind' or actual!=str(Path(source).resolve()) or mount.get('RW'):
            raise RuntimeError(f'Uncontrolled release mount: {name} {destination}')
def verify_containers(runtime,sha):
    infos={}
    for name in SERVICES:
        info=json.loads(run(['docker','inspect',f'literature-ai-{name}-1']))[0]
        infos[name]=info
        if not info['State'].get('Running') or info['State'].get('Restarting'):raise RuntimeError('Container is not running stably: '+name)
        if name in {'backend','worker','worker-pdf'} and info['State'].get('Health',{}).get('Status')!='healthy':raise RuntimeError('Service health check has not passed: '+name)
        if info['Config']['Labels'].get('org.opencontainers.image.revision')!=sha:raise RuntimeError('Container revision mismatch: '+name)

    app=application_root(runtime/'releases'/sha)
    for name,info in infos.items():
        verify_release_mounts(name,info,app)
    verify_ml_explorer_mount(runtime,infos['backend'],app)
    result=json.loads(run(['docker','exec','literature-ai-backend-1','python','-c',"import json,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"]))
    if result.get('git_commit')!=sha:raise RuntimeError('Backend health revision mismatch')
    return result
def verify_consistent(runtime,sha):
    current=current_snapshot(runtime)
    if current['commit']!=sha:
        raise RuntimeError(f'Deployment pointer mismatch: expected {sha}, found {current["commit"]}')
    return verify_containers(runtime,sha)
def verify(runtime,sha):
    require_no_transaction(runtime)
    return verify_consistent(runtime,sha)
def transaction_path(runtime):return runtime/'deploy-state'/'.deployment-transaction.json'
def require_no_transaction(runtime):
    path=transaction_path(runtime)
    if path.exists():
        try:data=json.loads(path.read_text())
        except Exception:data={}
        raise RuntimeError('Unfinished deployment transaction blocks changes: '
                           f'phase={data.get("phase","unknown")} '
                           f'deployment_id={data.get("deployment_id","unknown")}; '
                           'inspect the journal and run the explicit recover action')
def begin_transaction(runtime,expected,target,plan_hash,compose_hash):
    require_no_transaction(runtime)
    data={'deployment_id':uuid.uuid4().hex,'expected_current_commit':expected['commit'],
          'expected_current':dict(expected),
          'target_commit':target,'plan_sha256':plan_hash,'compose_sha256':compose_hash,
          'previous_plan_sha256':expected['plan_sha256'],'previous_compose_sha256':expected['compose_sha256'],
          'phase':'prepared','started_at_utc':datetime.now(timezone.utc).isoformat()}
    write_private(transaction_path(runtime),json.dumps(data,sort_keys=True,indent=2)+'\n')
    return data
def update_transaction(runtime,transaction,phase):
    transaction=dict(transaction);transaction['phase']=phase
    transaction['updated_at_utc']=datetime.now(timezone.utc).isoformat()
    write_private(transaction_path(runtime),json.dumps(transaction,sort_keys=True,indent=2)+'\n')
    return transaction
def finish_failed_attempt(runtime,transaction,error,rolled_back):
    state=private_directory(runtime/'deploy-state'/transaction['target_commit']/'failed-attempts')
    record={**transaction,'phase':'rolled_back','error':str(error),'rolled_back':rolled_back,
            'finished_at_utc':datetime.now(timezone.utc).isoformat()}
    write_private(state/(transaction['deployment_id']+'.json'),json.dumps(record,sort_keys=True,indent=2)+'\n')
def cas_pointer(runtime,allowed_current,target,*,writer=write_private):
    current=current_snapshot(runtime)
    if current['commit']==target:return current
    if current['commit'] not in allowed_current:
        raise RuntimeError(f'Pointer CAS rejected: current {current["commit"]} is not in {sorted(x or "missing" for x in allowed_current)}')
    writer(runtime/'DEPLOYED_GITHUB_COMMIT',target+'\n')
    updated=current_snapshot(runtime)
    if updated['commit']!=target:raise RuntimeError('Pointer CAS did not activate the target commit')
    return updated
def rollback_to_expected(runtime,expected,target,runner,container_verifier,verifier):
    runtime=Path(runtime)
    state=runtime/'deploy-state'/expected['commit']
    compose_file=resolve_plan_compose(state,expected['plan_sha256'])
    if not compose_file.is_file() or digest(compose_file)!=expected['compose_sha256']:
        raise RuntimeError('Rollback compose configuration is missing or changed')
    current=current_snapshot(runtime)
    if current['commit'] not in {expected['commit'],target}:
        raise RuntimeError('Deployment pointer moved to an unrelated commit during rollback')
    compose=['docker','compose','-p','literature-ai','--project-directory',str(runtime),'-f',str(compose_file)]
    runner([*compose,'build','backend','worker','worker-pdf'],check=True)
    runner([*compose,'up','-d','--no-deps','--force-recreate','--wait','--wait-timeout','180',*SERVICES],check=True)
    container_verifier(runtime,expected['commit'])
    cas_pointer(runtime,{expected['commit'],target},expected['commit'])
    assert_current(runtime,expected)
    verifier(runtime,expected['commit'])
def record_deployment(runtime,sha,plan_hash,verified,expected_current,deployment_id,*,writer=write_private):
    assert_current(runtime,expected_current)
    state=private_directory(runtime/'deploy-state'/sha)
    target_plan=runtime/'deploy-state'/sha/'plan.json'
    target_compose=resolve_plan_compose(runtime/'deploy-state'/sha,plan_hash)
    if not target_plan.is_file() or not target_compose.is_file() or digest(target_plan)!=plan_hash:
        raise RuntimeError('Target plan is missing or changed before pointer CAS')
    receipt={'commit':sha,'previous_commit':expected_current['commit'],'remote':REMOTE,
             'plan_sha256':plan_hash,'deployment_id':deployment_id,'verified_health':verified}
    writer(state/'deployment-receipt.json',json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    assert_current(runtime,expected_current)
    cas_pointer(runtime,{expected_current['commit']},sha,writer=writer)
    return receipt
def apply_release(runtime,sha,plan_hash,compose,expected_current,compose_hash,*,runner=None,
                  container_verifier=None,post_verifier=None,recorder=None,lock=True):
    runner=runner or subprocess.run
    container_verifier=container_verifier or verify_containers
    post_verifier=post_verifier or verify_consistent
    recorder=recorder or record_deployment
    context=deployment_lock(runtime) if lock else nullcontext()
    with context:
        if sha==expected_current['commit']:
            raise RuntimeError('Target commit is already active; use verify instead of apply')
        assert_current(runtime,expected_current)
        require_no_transaction(runtime)
        transaction=begin_transaction(runtime,expected_current,sha,plan_hash,compose_hash)
        containers_touched=False
        try:
            runner([*compose,'build','backend','worker','worker-pdf'],check=True)
            update_transaction(runtime,transaction,'built')
            assert_current(runtime,expected_current)
            containers_touched=True
            runner([*compose,'up','-d','--no-deps','--force-recreate','--wait','--wait-timeout','180',*SERVICES],check=True)
            transaction=update_transaction(runtime,transaction,'up')
            assert_current(runtime,expected_current)
            preverified=container_verifier(runtime,sha)
            transaction=update_transaction(runtime,transaction,'target_verified')
            assert_current(runtime,expected_current)
            receipt=recorder(runtime,sha,plan_hash,preverified,expected_current,transaction['deployment_id'])
            transaction=update_transaction(runtime,transaction,'committed')
            post_verifier(runtime,sha)
            transaction_path(runtime).unlink()
            return receipt
        except Exception as exc:
            if not containers_touched:
                try:transaction_path(runtime).unlink()
                except FileNotFoundError:pass
                raise
            try:
                try:update_transaction(runtime,transaction,'rolling_back')
                except Exception:pass
                rollback_to_expected(runtime,expected_current,sha,runner,container_verifier,post_verifier)
                finish_failed_attempt(runtime,transaction,exc,True)
                try:transaction_path(runtime).unlink()
                except FileNotFoundError:pass
            except Exception as rollback_exc:
                try:update_transaction(runtime,transaction,'recovery_required')
                except Exception:pass
                raise RuntimeError(f'Deployment failed and rollback was not verified: {rollback_exc}; original: {exc}') from exc
            raise
def recover(runtime,sha):
    with deployment_lock(runtime):
        path=transaction_path(runtime)
        if not path.exists():
            current=current_snapshot(runtime)
            if current['commit']!=sha:raise RuntimeError('No transaction exists and pointer does not match recover commit')
            return {'status':'no_transaction','commit':sha,'verified':verify_consistent(runtime,sha)}
        try:transaction=json.loads(path.read_text())
        except Exception as exc:raise RuntimeError('Deployment transaction journal is invalid') from exc
        if transaction.get('target_commit')!=sha:
            raise RuntimeError('Recover commit does not match transaction target')
        expected=transaction.get('expected_current')
        if not isinstance(expected,dict) or set(expected)!={'commit','pointer_sha256','receipt_sha256','plan_sha256','compose_sha256'}:
            raise RuntimeError('Transaction lacks a recoverable expected-current snapshot')
        allowed={expected['commit'],sha}
        try:
            current=current_snapshot(runtime)
            if current['commit'] not in allowed:
                raise RuntimeError('Deployment pointer is unrelated to the transaction')
            if current['commit']==expected['commit'] and current!=expected:
                raise RuntimeError('Expected-current plan, compose, pointer or receipt changed during transaction')
            if current['commit']==sha:
                if current['plan_sha256']!=transaction.get('plan_sha256') or current['compose_sha256']!=transaction.get('compose_sha256'):
                    raise RuntimeError('Target plan or compose does not match transaction')
                receipt=runtime/'deploy-state'/sha/'deployment-receipt.json'
                receipt_data=json.loads(receipt.read_text())
                if receipt_data.get('deployment_id')!=transaction.get('deployment_id'):
                    raise RuntimeError('Target receipt deployment id does not match transaction')
            verified=verify_consistent(runtime,current['commit'])
        except Exception as exc:
            try:update_transaction(runtime,transaction,'recovery_required')
            except Exception:pass
            raise RuntimeError(f'Recovery verification failed: {exc}') from exc
        recovered=update_transaction(runtime,transaction,'recovered')
        state=private_directory(runtime/'deploy-state'/sha/'recoveries')
        write_private(state/(transaction['deployment_id']+'.json'),json.dumps({
            'transaction':recovered,'result':'verified','verified':verified,
            'current_snapshot':current,
        },sort_keys=True,indent=2)+'\n')
        path.unlink()
        return {'status':'recovered','commit':current['commit'],'verified':verified}
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','apply','verify','recover'])
    parser.add_argument('commit',help='Full 40-character GitHub commit SHA')
    parser.add_argument('--runtime',default=str(PRODUCTION_RUNTIME))
    parser.add_argument('--plan-sha256')
    parser.add_argument('--rotate-plan-reason')
    parser.add_argument('--expected-old-plan-sha256')
    args=parser.parse_args();sha=args.commit
    if not re.fullmatch('[0-9a-f]{40}',sha):parser.error('A full commit SHA is required; branch names are not accepted')
    if args.action=='apply' and not re.fullmatch('[0-9a-f]{64}',args.plan_sha256 or ''):parser.error('apply requires the exact reviewed --plan-sha256')
    if args.action!='plan' and (args.rotate_plan_reason or args.expected_old_plan_sha256):parser.error('plan rotation options are only valid for plan')
    if bool(args.rotate_plan_reason)!=bool(args.expected_old_plan_sha256):parser.error('plan rotation requires both reason and exact old plan digest')
    if args.expected_old_plan_sha256 and not re.fullmatch('[0-9a-f]{64}',args.expected_old_plan_sha256):parser.error('Old plan digest must be 64 lowercase hex characters')
    try:runtime=production_runtime(args.runtime)
    except RuntimeError as exc:parser.error(str(exc))
    if args.action=='verify':print(json.dumps(verify(runtime,sha)));return
    if args.action=='recover':print(json.dumps(recover(runtime,sha),sort_keys=True));return
    if args.action=='plan':
        with deployment_lock(runtime):
            require_no_transaction(runtime)
            expected_current=current_snapshot(runtime)
            if expected_current['commit'] is None:
                raise RuntimeError('Current deployment pointer is required for a rollbackable plan')
            app=fetch(runtime,sha);state=private_directory(runtime/'deploy-state'/sha)
            plan,plan_raw,raw,plan_hash=build_plan(runtime,app,sha,expected_current)
            plan_file=state/'plan.json';config_file=plan_compose_path(state,plan_hash)
            changed=False
            for path,data in ((plan_file,plan_raw),(config_file,raw)):
                if path.exists() and path.read_text()!=data:changed=True
            rotation=None
            if changed:
                if not args.rotate_plan_reason:raise RuntimeError('plan_rotation_required: explicit reason and old plan digest are required')
                if sha==expected_current['commit']:
                    raise RuntimeError('Active commit plan rotation is forbidden; deploy a new exact commit or use the preserved active helper/plan')
                rotation=archive_plan(state,plan_hash,args.rotate_plan_reason,args.expected_old_plan_sha256)
            if rotation or not config_file.exists():write_private(config_file,raw)
            if rotation or not plan_file.exists():write_private(plan_file,plan_raw)
            result={'plan':str(plan_file),'compose':str(config_file),'plan_sha256':plan_hash,'compose_sha256':plan['compose_sha256'],
                    'commit':sha,'expected_current':expected_current,'rotation':rotation}
            print(json.dumps(result,sort_keys=True));return
    with deployment_lock(runtime):
        require_no_transaction(runtime)
        plan,config_file,config_hash=reviewed_plan(runtime,sha,args.plan_sha256)
        expected_current=plan['expected_current']
        assert_current(runtime,expected_current)
        app=fetch(runtime,sha)
        rebuilt,plan_raw,raw,rebuilt_hash=build_plan(runtime,app,sha,expected_current)
        if rebuilt_hash!=args.plan_sha256 or digest(config_file)!=config_hash or rebuilt['compose_sha256']!=config_hash:
            raise RuntimeError('Reviewed plan no longer matches current configuration')
        compose=['docker','compose','-p','literature-ai','--project-directory',str(runtime),'-f',str(config_file)]
        receipt=apply_release(runtime,sha,args.plan_sha256,compose,expected_current,config_hash,lock=False)
        print(json.dumps(receipt,sort_keys=True))
if __name__=='__main__':main()

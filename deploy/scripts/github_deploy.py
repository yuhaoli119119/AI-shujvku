#!/usr/bin/env python3
"""Deploy an exact GitHub commit. Plan first; apply requires its reviewed digest."""
import argparse,hashlib,json,os,re,subprocess,uuid
from pathlib import Path

REMOTE='https://github.com/yuhaoli119119/AI-shujvku.git'
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
def verify(runtime,sha):
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
def record_deployment(runtime,sha,plan_hash,verified,*,writer=write_private):
    state=private_directory(runtime/'deploy-state'/sha)
    receipt={'commit':sha,'remote':REMOTE,'plan_sha256':plan_hash,'verified_health':verified}
    writer(state/'deployment-receipt.json',json.dumps(receipt,indent=2)+'\n')
    writer(runtime/'DEPLOYED_GITHUB_COMMIT',sha+'\n')
    return receipt
def apply_release(runtime,sha,plan_hash,compose,*,runner=None,verifier=None,recorder=None):
    runner=runner or subprocess.run
    verifier=verifier or verify
    recorder=recorder or record_deployment
    runner([*compose,'build','backend','worker','worker-pdf'],check=True)
    runner([*compose,'up','-d','--no-deps','--force-recreate','--wait','--wait-timeout','180',*SERVICES],check=True)
    verified=verifier(runtime,sha)
    return recorder(runtime,sha,plan_hash,verified)
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','apply','verify'])
    parser.add_argument('commit',help='Full 40-character GitHub commit SHA')
    parser.add_argument('--runtime',default='/opt/literature-ai')
    parser.add_argument('--plan-sha256')
    args=parser.parse_args();sha=args.commit
    if not re.fullmatch('[0-9a-f]{40}',sha):parser.error('A full commit SHA is required; branch names are not accepted')
    if args.action=='apply' and not args.plan_sha256:parser.error('apply requires the exact reviewed --plan-sha256')
    runtime=Path(args.runtime).resolve()
    if not str(runtime).startswith('/opt/'):parser.error('Server project path required')
    if args.action=='verify':print(json.dumps(verify(runtime,sha)));return
    app=fetch(runtime,sha);state=private_directory(runtime/'deploy-state'/sha)
    cfg=configuration(runtime,app,sha)
    private_assets=cfg.pop('_litai_private_assets',None)
    raw=json.dumps(cfg,sort_keys=True,indent=2)+'\n'
    config_hash=hashlib.sha256(raw.encode()).hexdigest()
    plan={'commit':sha,'remote':REMOTE,'code_root':str(app),'services_to_recreate':SERVICES,
          'durable_data_root':str(runtime/'data'),'credential_file':str(runtime/'.env'),
          'compose_sha256':config_hash,'database_services_recreated':[],
          'private_assets':private_assets,
          'code_mounts':{s:[v for v in cfg['services'][s].get('volumes',[]) if v.get('source','').startswith(str(app))] for s in SERVICES}}
    plan_raw=json.dumps(plan,sort_keys=True,indent=2)+'\n';plan_hash=hashlib.sha256(plan_raw.encode()).hexdigest()
    plan_file=state/'plan.json';config_file=state/'compose.private.json'
    if args.action=='plan':
        for path,data in [(plan_file,plan_raw),(config_file,raw)]:
            if path.exists() and path.read_text()!=data:raise RuntimeError('Existing release plan changed; review a new plan before deploying')
            if not path.exists():write_private(path,data)
        print(json.dumps({'plan':str(plan_file),'plan_sha256':plan_hash,'commit':sha}));return
    if not args.plan_sha256 or not plan_file.exists() or digest(plan_file)!=args.plan_sha256 or args.plan_sha256!=plan_hash:raise RuntimeError('Exact reviewed plan digest required')
    if not config_file.exists() or digest(config_file)!=config_hash:raise RuntimeError('Compose configuration changed after review')
    compose=['docker','compose','-p','literature-ai','--project-directory',str(runtime),'-f',str(config_file)]
    receipt=apply_release(runtime,sha,plan_hash,compose)
    print(json.dumps(receipt))
if __name__=='__main__':main()

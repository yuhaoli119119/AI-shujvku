#!/usr/bin/env python3
"""Deploy an exact GitHub commit. Plan first; apply requires its reviewed digest."""
import argparse,hashlib,json,os,re,subprocess
from pathlib import Path

REMOTE='https://github.com/yuhaoli119119/AI-shujvku.git'
SERVICES=['backend','worker','worker-pdf','owner-gateway','share-gateway','public-gateway']
def run(args,**kwargs):
    return subprocess.check_output(args,text=True,**kwargs).strip()
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write_private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write(data)
def application_root(checkout):
    # Support both the single-directory repository and older frozen commits.
    for app in (checkout,checkout/'literature-ai'):
        if (app/'docker-compose.yml').is_file() and (app/'backend/app/main.py').is_file():
            return app
    raise RuntimeError('GitHub commit does not contain a Literature AI application')
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
    return cfg
def verify(runtime,sha):
    for name in SERVICES:
        info=json.loads(run(['docker','inspect',f'literature-ai-{name}-1']))[0]
        if not info['State'].get('Running') or info['State'].get('Restarting'):raise RuntimeError('Container is not running stably: '+name)
        if name in {'backend','worker','worker-pdf'} and info['State'].get('Health',{}).get('Status')!='healthy':raise RuntimeError('Service health check has not passed: '+name)
        if info['Config']['Labels'].get('org.opencontainers.image.revision')!=sha:raise RuntimeError('Container revision mismatch: '+name)
        for mount in info['Mounts']:
            if mount['Destination'] in {'/app','/frontend','/prompts'}:
                expected=str(application_root(runtime/'releases'/sha))+'/'
                if not mount['Source'].startswith(expected) or mount['RW']:raise RuntimeError('Uncontrolled source mount: '+name)
    result=json.loads(run(['docker','exec','literature-ai-backend-1','python','-c',"import json,urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health').read().decode())"]))
    if result.get('git_commit')!=sha:raise RuntimeError('Backend health revision mismatch')
    return result
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
    app=fetch(runtime,sha);state=runtime/'deploy-state'/sha;state.mkdir(parents=True,exist_ok=True)
    cfg=configuration(runtime,app,sha)
    raw=json.dumps(cfg,sort_keys=True,indent=2)+'\n'
    config_hash=hashlib.sha256(raw.encode()).hexdigest()
    plan={'commit':sha,'remote':REMOTE,'code_root':str(app),'services_to_recreate':SERVICES,
          'durable_data_root':str(runtime/'data'),'credential_file':str(runtime/'.env'),
          'compose_sha256':config_hash,'database_services_recreated':[],
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
    subprocess.run([*compose,'build','backend','worker','worker-pdf'],check=True)
    subprocess.run([*compose,'up','-d','--no-deps','--force-recreate','--wait','--wait-timeout','180',*SERVICES],check=True)
    verified=verify(runtime,sha)
    receipt={'commit':sha,'remote':REMOTE,'plan_sha256':plan_hash,'verified_health':verified}
    write_private(state/'deployment-receipt.json',json.dumps(receipt,indent=2)+'\n')
    pointer=runtime/'DEPLOYED_GITHUB_COMMIT';temporary=runtime/'DEPLOYED_GITHUB_COMMIT.next'
    write_private(temporary,sha+'\n');temporary.replace(pointer)
    print(json.dumps(receipt))
if __name__=='__main__':main()

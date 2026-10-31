import importlib.util,hashlib,io,json,tempfile,threading,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('github_deploy',Path(__file__).parents[1]/'scripts/github_deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

class DeploymentBoundaryTests(unittest.TestCase):
    def _ml_assets(self,runtime,asset_sha=None):
        scope={'libraryName':'锂硫双原子','libraryScopeKey':'lithium-sulfur-dual-atom',
               'domains':['LITHIUM_SULFUR_MODEL'],'unitCount':177948,'exactMaterialContexts':5775}
        allow={'domains':['LITHIUM_SULFUR_MODEL'],'models':['random_forest','dummy_median']}
        payload={'schemaVersion':'ml-explorer-candidate.v2','manifest':{
            'libraryName':scope['libraryName'],'libraryScopeKey':scope['libraryScopeKey'],
            'dataDomains':scope['domains'],'allow':allow}}
        bundle=(json.dumps(payload)+'\n').encode()
        bundle_sha=hashlib.sha256(bundle).hexdigest()
        source=runtime/'data/ml-explorer-releases'/(asset_sha or bundle_sha)
        source.mkdir(parents=True)
        (source/'app_data.json').write_bytes(bundle)
        manifest={'schemaVersion':'ml-explorer-runtime.v2','adapterEntry':'data/app_data.json',
                  'bundle':{'sha256':bundle_sha,'bytes':len(bundle),'payloadSchemaVersion':'ml-explorer-candidate.v2'},
                  'scope':scope,'allow':allow}
        (source/'manifest.json').write_text(json.dumps(manifest)+'\n')
        return source

    def _complete_infos(self,runtime,sha):
        app=runtime/'releases'/sha
        infos={}
        for name,expected in deploy.RELEASE_MOUNTS.items():
            infos[name]={'State':{'Running':True,'Health':{'Status':'healthy'}},
                         'Config':{'Labels':{'org.opencontainers.image.revision':sha}},
                         'Mounts':[{'Type':'bind','Source':str(app/relative),'Destination':destination,'RW':False}
                                   for destination,relative in expected.items()]}
        return app,infos

    def _seed_state(self,runtime,sha,plan_hash='1'*64,compose_hash='2'*64):
        state=runtime/'deploy-state'/sha
        state.mkdir(parents=True,exist_ok=True)
        compose={'commit':sha,'compose_marker':compose_hash,'services':{}}
        (state/'compose.private.json').write_text(json.dumps(compose,sort_keys=True)+'\n')
        plan={'commit':sha,'plan_marker':plan_hash,'compose_sha256':deploy.digest(state/'compose.private.json')}
        (state/'plan.json').write_text(json.dumps(plan,sort_keys=True)+'\n')
        receipt={'commit':sha,'plan_sha256':deploy.digest(state/'plan.json'),'verified_health':{'git_commit':sha}}
        (state/'deployment-receipt.json').write_text(json.dumps(receipt,sort_keys=True)+'\n')
        return state,deploy.digest(state/'plan.json'),deploy.digest(state/'compose.private.json')

    def _seed_deployment(self,runtime,sha,plan_hash='1'*64,compose_hash='2'*64):
        self._seed_state(runtime,sha,plan_hash,compose_hash)
        (runtime/'DEPLOYED_GITHUB_COMMIT').write_text(sha+'\n')
        return deploy.current_snapshot(runtime)

    def _pointer_only(self,runtime,sha):
        (runtime/'DEPLOYED_GITHUB_COMMIT').write_text(sha+'\n')

    def test_repository_root_layout_is_supported(self):
        checkout=Path('/opt/literature-ai/releases')/('a'*40)
        files={checkout/'docker-compose.yml',checkout/'backend/app/main.py'}
        with patch.object(Path,'is_file',lambda p:p in files):
            self.assertEqual(deploy.application_root(checkout),checkout)

    def test_frozen_nested_layout_remains_deployable(self):
        checkout=Path('/opt/literature-ai/releases')/('a'*40)
        app=checkout/'literature-ai'
        files={app/'docker-compose.yml',app/'backend/app/main.py'}
        with patch.object(Path,'is_file',lambda p:p in files):
            self.assertEqual(deploy.application_root(checkout),app)

    def test_missing_application_cannot_be_deployed(self):
        with patch.object(Path,'is_file',return_value=False):
            with self.assertRaisesRegex(RuntimeError,'does not contain'):
                deploy.application_root(Path('/opt/literature-ai/releases')/('a'*40))

    def test_code_and_build_come_from_release_while_data_and_credentials_stay(self):
        runtime=Path('/opt/literature-ai');app=runtime/'releases'/('a'*40)/'literature-ai'
        cfg={'services':{'backend':{'build':{'context':str(runtime),'dockerfile':'backend/Dockerfile'},'volumes':[
            {'type':'bind','source':str(runtime/'backend'),'target':'/app'},
            {'type':'bind','source':str(runtime/'data'),'target':'/data'},
            {'type':'bind','source':str(runtime/'deploy/nginx/owner.htpasswd'),'target':'/etc/litai/owner.htpasswd'}]},
            'postgres':{'volumes':[{'type':'volume','source':'postgres_data','target':'/var/lib/postgresql/data'}]}}}
        with patch.object(deploy,'run',return_value=json.dumps(cfg)),patch.object(Path,'exists',return_value=True):
            result=deploy.configuration(runtime,app,'a'*40)
        backend=result['services']['backend']
        self.assertEqual(backend['build']['context'],str(app))
        self.assertEqual(backend['volumes'][0]['source'],str(app/'backend'))
        self.assertTrue(backend['volumes'][0]['read_only'])
        self.assertEqual(backend['volumes'][1]['source'],str(runtime/'data'))
        self.assertEqual(backend['volumes'][2]['source'],str(runtime/'deploy/nginx/owner.htpasswd'))
        self.assertEqual(result['services']['postgres'],cfg['services']['postgres'])
        self.assertEqual(backend['environment']['LITAI_GIT_COMMIT'],'a'*40)

    def test_nginx_configuration_is_bound_from_release_read_only(self):
        runtime=Path('/opt/literature-ai');app=runtime/'releases'/('a'*40)
        cfg={'services':{'owner-gateway':{'volumes':[
            {'type':'bind','source':str(runtime/'deploy/nginx/owner.conf.template'),'target':'/etc/nginx/templates/default.conf.template'}]}}}
        with patch.object(deploy,'run',return_value=json.dumps(cfg)),patch.object(Path,'exists',return_value=True):
            result=deploy.configuration(runtime,app,'a'*40)
        mount=result['services']['owner-gateway']['volumes'][0]
        self.assertEqual(mount['source'],str(app/'deploy/nginx/owner.conf.template'))
        self.assertTrue(mount['read_only'])

    def test_ml_explorer_private_asset_mount_is_validated_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='b'*40;app=runtime/'releases'/sha
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            source=self._ml_assets(runtime)
            cfg={'services':{'backend':{'volumes':[
                {'type':'bind','source':str(source),'target':deploy.ML_EXPLORER_MOUNT,'read_only':True}]}}}
            with patch.object(deploy,'run',return_value=json.dumps(cfg)):
                result=deploy.configuration(runtime,app,sha)
            assets=result.pop('_litai_private_assets')
            self.assertEqual(assets['source'],str(source.resolve()))
            self.assertEqual(assets['bundle_sha256'],hashlib.sha256((source/'app_data.json').read_bytes()).hexdigest())
            self.assertTrue(assets['read_only'])

    def test_ml_explorer_requires_private_asset_mount_when_page_is_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='b'*40;app=runtime/'releases'/sha
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            cfg={'services':{'backend':{'volumes':[]}}}
            with patch.object(deploy,'run',return_value=json.dumps(cfg)):
                with self.assertRaisesRegex(RuntimeError,'must mount ML Explorer assets exactly once'):
                    deploy.configuration(runtime,app,sha)

    def test_ml_explorer_writable_asset_mount_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='b'*40;app=runtime/'releases'/sha
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            source=self._ml_assets(runtime)
            cfg={'services':{'backend':{'volumes':[
                {'type':'bind','source':str(source),'target':deploy.ML_EXPLORER_MOUNT,'read_only':False}]}}}
            with patch.object(deploy,'run',return_value=json.dumps(cfg)):
                with self.assertRaisesRegex(RuntimeError,'must be read-only bind'):
                    deploy.configuration(runtime,app,sha)

    def test_ml_explorer_asset_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);source=self._ml_assets(runtime)
            manifest=json.loads((source/'manifest.json').read_text())
            manifest['bundle']['sha256']='0'*64
            (source/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError,'hash/bytes mismatch'):
                deploy.validate_ml_explorer_assets(runtime,source)

    def test_ml_explorer_asset_directory_must_match_bundle_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);source=self._ml_assets(runtime,'a'*64)
            with self.assertRaisesRegex(RuntimeError,'directory name must equal bundle SHA-256'):
                deploy.validate_ml_explorer_assets(runtime,source)

    def test_ml_explorer_runtime_scope_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);source=self._ml_assets(runtime)
            manifest=json.loads((source/'manifest.json').read_text())
            manifest['scope']['domains']=['OTHER_DOMAIN']
            (source/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError,'runtime scope mismatch'):
                deploy.validate_ml_explorer_assets(runtime,source)

    def test_ml_explorer_scope_count_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);source=self._ml_assets(runtime)
            manifest=json.loads((source/'manifest.json').read_text())
            manifest['scope']['unitCount']=1
            (source/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError,'runtime scope mismatch'):
                deploy.validate_ml_explorer_assets(runtime,source)

    def test_ml_explorer_allowlist_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);source=self._ml_assets(runtime)
            manifest=json.loads((source/'manifest.json').read_text())
            manifest['allow']['domains']=['OTHER_DOMAIN']
            (source/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError,'allowlist mismatch'):
                deploy.validate_ml_explorer_assets(runtime,source)

    def test_ml_explorer_asset_source_outside_release_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            with self.assertRaisesRegex(RuntimeError,'must come from'):
                deploy.validate_ml_explorer_assets(runtime,runtime/'data/other')

    def test_verify_accepts_exact_ml_explorer_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            source=self._ml_assets(runtime)
            infos['backend']['Mounts'].append({'Type':'bind','Source':str(source),
                                               'Destination':deploy.ML_EXPLORER_MOUNT,'RW':False})
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            outputs.append(json.dumps({'status':'ok','git_commit':sha}))
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                self.assertEqual(deploy.verify(runtime,sha)['git_commit'],sha)

    def test_verify_rejects_missing_ml_explorer_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                with self.assertRaisesRegex(RuntimeError,'Missing or duplicate ML Explorer asset mount'):
                    deploy.verify(runtime,sha)

    def test_verify_rejects_duplicate_ml_explorer_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            source=self._ml_assets(runtime)
            mount={'Type':'bind','Source':str(source),'Destination':deploy.ML_EXPLORER_MOUNT,'RW':False}
            infos['backend']['Mounts'].extend([mount,dict(mount)])
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                with self.assertRaisesRegex(RuntimeError,'Missing or duplicate ML Explorer asset mount'):
                    deploy.verify(runtime,sha)

    def test_verify_rejects_ml_explorer_scope_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            (app/'frontend/pages/ml_explorer').mkdir(parents=True)
            (app/'frontend/pages/ml_explorer/index.html').write_text('ok')
            source=self._ml_assets(runtime)
            manifest=json.loads((source/'manifest.json').read_text())
            manifest['scope']['libraryName']='wrong-library'
            (source/'manifest.json').write_text(json.dumps(manifest))
            infos['backend']['Mounts'].append({'Type':'bind','Source':str(source),
                                               'Destination':deploy.ML_EXPLORER_MOUNT,'RW':False})
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                with self.assertRaisesRegex(RuntimeError,'runtime scope mismatch'):
                    deploy.verify(runtime,sha)

    def test_production_runtime_must_match_unique_realpath(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)/'canonical';runtime.mkdir()
            alias=Path(directory)/'alias';alias.symlink_to(runtime, target_is_directory=True)
            other=Path(directory)/'other';other.mkdir()
            with patch.object(deploy,'PRODUCTION_RUNTIME',runtime):
                self.assertEqual(deploy.production_runtime(alias),runtime.resolve())
                with self.assertRaisesRegex(RuntimeError,'must resolve to'):
                    deploy.production_runtime(other)

    def test_current_snapshot_requires_matching_receipt_plan_and_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            (runtime/'deploy-state'/sha/'deployment-receipt.json').unlink()
            with self.assertRaisesRegex(RuntimeError,'receipt is missing'):
                deploy.current_snapshot(runtime)

    def test_current_snapshot_rejects_receipt_plan_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            receipt=runtime/'deploy-state'/sha/'deployment-receipt.json'
            data=json.loads(receipt.read_text());data['plan_sha256']='0'*64
            receipt.write_text(json.dumps(data))
            with self.assertRaisesRegex(RuntimeError,'receipt plan digest mismatch'):
                deploy.current_snapshot(runtime)

    def test_current_snapshot_rejects_plan_compose_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            state=runtime/'deploy-state'/sha
            plan=json.loads((state/'plan.json').read_text());plan['compose_sha256']='0'*64
            (state/'plan.json').write_text(json.dumps(plan))
            receipt=json.loads((state/'deployment-receipt.json').read_text())
            receipt['plan_sha256']=deploy.digest(state/'plan.json')
            (state/'deployment-receipt.json').write_text(json.dumps(receipt))
            with self.assertRaisesRegex(RuntimeError,'compose digest mismatch'):
                deploy.current_snapshot(runtime)

    def test_second_apply_is_rejected_before_build_while_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;first_target='b'*40;second_target='c'*40
            expected=self._seed_deployment(runtime,old)
            _,first_plan,first_compose=self._seed_state(runtime,first_target,'3'*64,'4'*64)
            entered=threading.Event();release=threading.Event();first_runner=[];errors=[]
            def first_verifier(_runtime,sha):
                entered.set();release.wait(5)
                return {'status':'ok','git_commit':sha}
            def run_first(args,**kwargs):first_runner.append(args)
            def run_second(args,**kwargs):raise AssertionError('second apply reached build')
            def worker():
                try:
                    deploy.apply_release(runtime,first_target,first_plan,['mock-compose'],expected,first_compose,
                                         runner=run_first,container_verifier=first_verifier,
                                         post_verifier=lambda *args:None)
                except Exception as exc:errors.append(exc)
            thread=threading.Thread(target=worker);thread.start();self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(RuntimeError,'lock is already held'):
                deploy.apply_release(runtime,second_target,'5'*64,['mock-compose'],expected,'6'*64,
                                     runner=run_second,container_verifier=lambda *args:{},
                                     post_verifier=lambda *args:None)
            release.set();thread.join(10)
            self.assertFalse(thread.is_alive());self.assertEqual(errors,[])
            self.assertEqual(len(first_runner),2)
            self.assertEqual((runtime/'DEPLOYED_GITHUB_COMMIT').read_text().strip(),first_target)

    def test_stale_expected_current_blocks_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;changed='b'*40;target='c'*40
            expected=self._seed_deployment(runtime,old)
            self._seed_deployment(runtime,changed,'3'*64,'4'*64)
            calls=[]
            with self.assertRaisesRegex(RuntimeError,'Current deployment changed since plan'):
                deploy.apply_release(runtime,target,'5'*64,['mock-compose'],expected,'6'*64,
                                     runner=lambda args,**kwargs:calls.append(args))
            self.assertEqual(calls,[])
            self.assertFalse(deploy.transaction_path(runtime).exists())

    def test_apply_to_already_active_commit_never_rewrites_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40
            expected=self._seed_deployment(runtime,current)
            receipt=runtime/'deploy-state'/current/'deployment-receipt.json'
            before=receipt.read_bytes();calls=[]
            with self.assertRaisesRegex(RuntimeError,'already active'):
                deploy.apply_release(runtime,current,expected['plan_sha256'],['mock-compose'],expected,expected['compose_sha256'],
                                     runner=lambda args,**kwargs:calls.append(args))
            self.assertEqual(receipt.read_bytes(),before)
            self.assertEqual(calls,[])
            self.assertFalse(deploy.transaction_path(runtime).exists())

    def test_current_change_during_build_blocks_up(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;changed='b'*40;target='c'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            calls=[]
            def runner(args,**kwargs):
                calls.append(args)
                if len(calls)==1:self._seed_deployment(runtime,changed,'5'*64,'6'*64)
            with self.assertRaisesRegex(RuntimeError,'Current deployment changed since plan'):
                deploy.apply_release(runtime,target,target_plan,['mock-compose'],expected,target_compose,runner=runner)
            self.assertEqual(len(calls),1)
            self.assertNotIn('up',calls[0])
            self.assertFalse(deploy.transaction_path(runtime).exists())

    def test_current_change_during_up_never_advances_target_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;changed='b'*40;target='c'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            calls=[]
            def runner(args,**kwargs):
                calls.append(args)
                if len(calls)==2:self._seed_deployment(runtime,changed,'5'*64,'6'*64)
            with self.assertRaisesRegex(RuntimeError,'rollback was not verified'):
                deploy.apply_release(runtime,target,target_plan,['mock-compose'],expected,target_compose,
                                     runner=runner,container_verifier=lambda *args:{},post_verifier=lambda *args:None)
            self.assertEqual((runtime/'DEPLOYED_GITHUB_COMMIT').read_text().strip(),changed)
            transaction=json.loads(deploy.transaction_path(runtime).read_text())
            self.assertEqual(transaction['phase'],'recovery_required')

    def test_post_pointer_verification_failure_rolls_back_pointer_and_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            calls=[]
            def runner(args,**kwargs):calls.append(args)
            def container_verifier(_runtime,sha):return {'status':'ok','git_commit':sha}
            def post_verifier(_runtime,sha):
                if sha==target:raise RuntimeError('post-pointer verification failed')
                return {'status':'ok','git_commit':sha}
            with self.assertRaisesRegex(RuntimeError,'post-pointer verification failed'):
                deploy.apply_release(runtime,target,target_plan,['mock-compose'],expected,target_compose,
                                     runner=runner,container_verifier=container_verifier,
                                     post_verifier=post_verifier)
            self.assertEqual(deploy.current_snapshot(runtime),expected)
            self.assertGreaterEqual(len(calls),4)
            self.assertFalse(deploy.transaction_path(runtime).exists())
            failed=list((runtime/'deploy-state'/target/'failed-attempts').glob('*.json'))
            self.assertEqual(len(failed),1)
            self.assertEqual(json.loads(failed[0].read_text())['phase'],'rolled_back')

    def test_rollback_never_overwrites_unrelated_pointer_after_up(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40;unrelated='c'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            def container_verifier(_runtime,sha):
                if sha==old:self._seed_deployment(runtime,unrelated,'5'*64,'6'*64)
                return {'status':'ok','git_commit':sha}
            def post_verifier(_runtime,sha):
                if sha==target:raise RuntimeError('post verify failed')
                return {'status':'ok','git_commit':sha}
            with self.assertRaisesRegex(RuntimeError,'rollback was not verified'):
                deploy.apply_release(runtime,target,target_plan,['mock-compose'],expected,target_compose,
                                     runner=lambda *args,**kwargs:None,container_verifier=container_verifier,
                                     post_verifier=post_verifier)
            self.assertEqual((runtime/'DEPLOYED_GITHUB_COMMIT').read_text().strip(),unrelated)
            transaction=json.loads(deploy.transaction_path(runtime).read_text())
            self.assertEqual(transaction['phase'],'recovery_required')

    def test_unfinished_transaction_blocks_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            deploy.write_private(deploy.transaction_path(runtime),json.dumps({'phase':'recovery_required'})+'\n')
            calls=[]
            with self.assertRaisesRegex(RuntimeError,'Unfinished deployment transaction'):
                deploy.apply_release(runtime,target,'3'*64,['mock-compose'],expected,'4'*64,
                                     runner=lambda args,**kwargs:calls.append(args))
            self.assertEqual(calls,[])

    def test_verify_fails_closed_on_unfinished_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            deploy.begin_transaction(runtime,expected,target,'1'*64,'2'*64)
            with self.assertRaisesRegex(RuntimeError,'Unfinished deployment transaction'):
                deploy.verify(runtime,current)

    def test_recover_clears_transaction_when_expected_current_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            deploy.begin_transaction(runtime,expected,target,'1'*64,'2'*64)
            with patch.object(deploy,'verify_consistent',return_value={'status':'ok','git_commit':current}) as verifier:
                result=deploy.recover(runtime,target)
            self.assertEqual(result['status'],'recovered')
            verifier.assert_called_once_with(runtime,current)
            self.assertFalse(deploy.transaction_path(runtime).exists())
            self.assertEqual(list((runtime/'deploy-state'/target/'recoveries').glob('*.json')).__len__(),1)

    def test_recover_clears_transaction_when_target_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            target_snapshot=self._seed_deployment(runtime,target,'3'*64,'4'*64)
            transaction=deploy.begin_transaction(runtime,expected,target,target_snapshot['plan_sha256'],target_snapshot['compose_sha256'])
            receipt=runtime/'deploy-state'/target/'deployment-receipt.json'
            receipt_data=json.loads(receipt.read_text());receipt_data['deployment_id']=transaction['deployment_id']
            receipt.write_text(json.dumps(receipt_data))
            with patch.object(deploy,'verify_consistent',return_value={'status':'ok','git_commit':target}) as verifier:
                result=deploy.recover(runtime,target)
            self.assertEqual(result['status'],'recovered')
            verifier.assert_called_once_with(runtime,target)
            self.assertFalse(deploy.transaction_path(runtime).exists())

    def test_recover_rejects_target_with_different_plan_or_receipt_identity(self):
        cases=('plan','compose','deployment_id')
        for changed in cases:
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                runtime=Path(directory);current='a'*40;target='b'*40
                expected=self._seed_deployment(runtime,current)
                target_snapshot=self._seed_deployment(runtime,target,'3'*64,'4'*64)
                transaction=deploy.begin_transaction(runtime,expected,target,target_snapshot['plan_sha256'],target_snapshot['compose_sha256'])
                receipt=runtime/'deploy-state'/target/'deployment-receipt.json'
                receipt_data=json.loads(receipt.read_text());receipt_data['deployment_id']=transaction['deployment_id']
                if changed=='deployment_id':receipt_data['deployment_id']='different-deployment'
                receipt.write_text(json.dumps(receipt_data))
                if changed=='plan':
                    journal=json.loads(deploy.transaction_path(runtime).read_text());journal['plan_sha256']='0'*64
                    deploy.write_private(deploy.transaction_path(runtime),json.dumps(journal))
                if changed=='compose':
                    journal=json.loads(deploy.transaction_path(runtime).read_text());journal['compose_sha256']='0'*64
                    deploy.write_private(deploy.transaction_path(runtime),json.dumps(journal))
                with patch.object(deploy,'verify_consistent',return_value={'status':'ok','git_commit':target}):
                    with self.assertRaisesRegex(RuntimeError,'Recovery verification failed'):
                        deploy.recover(runtime,target)
                journal=json.loads(deploy.transaction_path(runtime).read_text())
                self.assertEqual(journal['phase'],'recovery_required')

    def test_recover_failure_preserves_recovery_required_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            deploy.begin_transaction(runtime,expected,target,'1'*64,'2'*64)
            with patch.object(deploy,'verify_consistent',side_effect=RuntimeError('containers inconsistent')):
                with self.assertRaisesRegex(RuntimeError,'Recovery verification failed'):
                    deploy.recover(runtime,target)
            transaction=json.loads(deploy.transaction_path(runtime).read_text())
            self.assertEqual(transaction['phase'],'recovery_required')

    def test_recover_rejects_changed_expected_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            deploy.begin_transaction(runtime,expected,target,'1'*64,'2'*64)
            receipt=runtime/'deploy-state'/current/'deployment-receipt.json'
            data=json.loads(receipt.read_text());data['verified_health']={'status':'changed'}
            receipt.write_text(json.dumps(data))
            with patch.object(deploy,'verify_consistent',return_value={'status':'ok','git_commit':current}):
                with self.assertRaisesRegex(RuntimeError,'Recovery verification failed'):
                    deploy.recover(runtime,target)
            journal=json.loads(deploy.transaction_path(runtime).read_text())
            self.assertEqual(journal['phase'],'recovery_required')

    def test_plan_rotation_preserves_old_plan_and_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);target='b'*40;state=runtime/'deploy-state'/target
            deploy.private_directory(state)
            deploy.write_private(state/'plan.json','{"old":true}\n')
            deploy.write_private(state/'compose.private.json','{"services":{}}\n')
            old_sha=deploy.digest(state/'plan.json')
            result=deploy.archive_plan(state,'c'*64,'schema v2 rollback review',old_sha)
            archived=Path(result['archived_plan'])
            self.assertEqual(archived.read_text(),'{"old":true}\n')
            self.assertEqual((archived.parent/'compose.private.json').read_text(),'{"services":{}}\n')
            rotation=json.loads((archived.parent/'rotation.json').read_text())
            self.assertEqual(rotation['old_plan_sha256'],old_sha)
            self.assertEqual(rotation['new_plan_sha256'],'c'*64)

    def test_plan_rotation_requires_exact_old_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);state=runtime/'deploy-state'/('b'*40)
            deploy.private_directory(state)
            deploy.write_private(state/'plan.json','{"old":true}\n')
            deploy.write_private(state/'compose.private.json','{}\n')
            with self.assertRaisesRegex(RuntimeError,'Old plan digest does not match'):
                deploy.archive_plan(state,'c'*64,'reviewed rotation','0'*64)

    def test_legacy_plan_requires_explicit_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);target='b'*40;state=runtime/'deploy-state'/target
            deploy.private_directory(state)
            deploy.write_private(state/'plan.json','{"commit":"'+target+'"}\n')
            deploy.write_private(state/'compose.private.json','{}\n')
            with self.assertRaisesRegex(RuntimeError,'Legacy plan has no deployment CAS schema'):
                deploy.reviewed_plan(runtime,target,deploy.digest(state/'plan.json'))

    def test_plan_cli_rotation_archives_old_plan_and_writes_new_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            state=runtime/'deploy-state'/target;deploy.private_directory(state)
            old_plan='{"legacy":true}\n';old_compose='{"services":{}}\n'
            deploy.write_private(state/'plan.json',old_plan)
            deploy.write_private(state/'compose.private.json',old_compose)
            old_digest=hashlib.sha256(old_plan.encode()).hexdigest()
            raw='{"services":{}}\n';config_hash=hashlib.sha256(raw.encode()).hexdigest()
            new_plan={'schema_version':2,'commit':target,'remote':deploy.REMOTE,
                      'expected_current':expected,'rollback':{'commit':current,'plan_sha256':expected['plan_sha256'],
                      'compose_sha256':expected['compose_sha256'],'receipt_sha256':expected['receipt_sha256']},
                      'compose_sha256':config_hash}
            plan_raw=json.dumps(new_plan,sort_keys=True,indent=2)+'\n'
            plan_hash=hashlib.sha256(plan_raw.encode()).hexdigest()
            with patch.object(deploy,'PRODUCTION_RUNTIME',runtime), \
                 patch('sys.argv',['github_deploy','plan',target,'--runtime',str(runtime),
                                   '--rotate-plan-reason','reviewed schema migration',
                                   '--expected-old-plan-sha256',old_digest]), \
                 patch.object(deploy,'fetch',return_value=runtime/'releases'/target), \
                 patch.object(deploy,'build_plan',return_value=(new_plan,plan_raw,raw,plan_hash)):
                with redirect_stdout(io.StringIO()):deploy.main()
            self.assertEqual((state/'plan.json').read_text(),plan_raw)
            self.assertEqual(deploy.plan_compose_path(state,plan_hash).read_text(),raw)
            archived=state/'plan-history'/old_digest
            self.assertEqual((archived/'plan.json').read_text(),old_plan)
            self.assertEqual((archived/'compose.private.json').read_text(),old_compose)
            self.assertTrue((archived/'rotation.json').is_file())

    def test_plan_cli_without_explicit_rotation_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,current)
            state=runtime/'deploy-state'/target;deploy.private_directory(state)
            old_plan='{"legacy":true}\n';old_compose='{"services":{}}\n'
            deploy.write_private(state/'plan.json',old_plan)
            deploy.write_private(state/'compose.private.json',old_compose)
            raw='{"services":{}}\n';config_hash=hashlib.sha256(raw.encode()).hexdigest()
            new_plan={'schema_version':2,'commit':target,'remote':deploy.REMOTE,'expected_current':expected,
                      'rollback':{'commit':current,'plan_sha256':expected['plan_sha256'],
                      'compose_sha256':expected['compose_sha256'],'receipt_sha256':expected['receipt_sha256']},
                      'compose_sha256':config_hash}
            plan_raw=json.dumps(new_plan,sort_keys=True,indent=2)+'\n'
            plan_hash=hashlib.sha256(plan_raw.encode()).hexdigest()
            with patch.object(deploy,'PRODUCTION_RUNTIME',runtime), \
                 patch('sys.argv',['github_deploy','plan',target,'--runtime',str(runtime)]), \
                 patch.object(deploy,'fetch',return_value=runtime/'releases'/target), \
                 patch.object(deploy,'build_plan',return_value=(new_plan,plan_raw,raw,plan_hash)):
                with self.assertRaisesRegex(RuntimeError,'plan_rotation_required'):
                    deploy.main()
            self.assertEqual((state/'plan.json').read_text(),old_plan)
            self.assertEqual((state/'compose.private.json').read_text(),old_compose)
            self.assertFalse((state/'plan-history').exists())

    def test_active_commit_plan_rotation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            expected=self._seed_deployment(runtime,sha)
            state=runtime/'deploy-state'/sha
            old_digest=deploy.digest(state/'plan.json')
            raw='{"services":{}}\n';config_hash=hashlib.sha256(raw.encode()).hexdigest()
            new_plan={'schema_version':2,'commit':sha,'remote':deploy.REMOTE,'expected_current':expected,
                      'rollback':{'commit':sha,'plan_sha256':expected['plan_sha256'],
                      'compose_sha256':expected['compose_sha256'],'receipt_sha256':expected['receipt_sha256']},
                      'compose_sha256':config_hash}
            plan_raw=json.dumps(new_plan,sort_keys=True,indent=2)+'\n'
            plan_hash=hashlib.sha256(plan_raw.encode()).hexdigest()
            with patch.object(deploy,'PRODUCTION_RUNTIME',runtime), \
                 patch('sys.argv',['github_deploy','plan',sha,'--runtime',str(runtime),
                                   '--rotate-plan-reason','active rotation',
                                   '--expected-old-plan-sha256',old_digest]), \
                 patch.object(deploy,'fetch',return_value=runtime/'releases'/sha), \
                 patch.object(deploy,'build_plan',return_value=(new_plan,plan_raw,raw,plan_hash)):
                with self.assertRaisesRegex(RuntimeError,'Active commit plan rotation is forbidden'):
                    deploy.main()
            self.assertEqual(deploy.digest(state/'plan.json'),old_digest)

    def test_plan_rotation_retry_after_config_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);target='b'*40;state=runtime/'deploy-state'/target
            deploy.private_directory(state)
            deploy.write_private(state/'plan.json','{"old":true}\n')
            deploy.write_private(state/'compose.private.json','{"old":true}\n')
            old_sha=deploy.digest(state/'plan.json')
            first=deploy.archive_plan(state,'c'*64,'reviewed rotation',old_sha)
            deploy.write_private(deploy.plan_compose_path(state,'c'*64),'{"new":true}\n')
            second=deploy.archive_plan(state,'c'*64,'reviewed rotation',old_sha)
            self.assertEqual(first['old_plan_sha256'],second['old_plan_sha256'])
            rotation=json.loads((Path(first['archived_plan']).parent/'rotation.json').read_text())
            self.assertEqual(rotation['new_plan_sha256'],'c'*64)

    def test_runtime_override_is_rejected_before_fetch(self):
        with patch('sys.argv',['github_deploy','plan','a'*40,'--runtime','/opt/isolated-worktree']), \
             patch.object(deploy,'fetch') as fetch:
            with self.assertRaises(SystemExit):deploy.main()
            fetch.assert_not_called()

    def test_update_script_without_arguments_never_calls_apply(self):
        source=(Path(__file__).parents[2]/'update.sh').read_text()
        self.assertIn('Plan generated only',source)
        self.assertNotIn('PLAN_SHA="$(python3 -c',source)
        self.assertNotIn('exec python3 "$PROJECT_DIR/deploy/scripts/github_deploy.py" apply',source)

    def test_verify_rejects_pointer_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);current='a'*40;target='b'*40
            self._seed_deployment(runtime,current)
            with self.assertRaisesRegex(RuntimeError,'Deployment pointer mismatch'):
                deploy.verify(runtime,target)

    def test_failed_rollback_leaves_recovery_required_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            calls=[]
            def runner(args,**kwargs):calls.append(args)
            def verifier(_runtime,sha):raise RuntimeError('verification unavailable')
            with self.assertRaisesRegex(RuntimeError,'rollback was not verified'):
                deploy.apply_release(runtime,target,target_plan,['mock-compose'],expected,target_compose,
                                     runner=runner,container_verifier=verifier,post_verifier=verifier)
            transaction=json.loads(deploy.transaction_path(runtime).read_text())
            self.assertEqual(transaction['phase'],'recovery_required')
            self.assertEqual(deploy.current_snapshot(runtime),expected)

    def test_private_state_write_atomically_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'nested'/'state.json'
            deploy.write_private(path,'first\n')
            deploy.write_private(path,'second\n')
            self.assertEqual(path.read_text(),'second\n')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual([item.name for item in path.parent.iterdir()],['state.json'])

    def test_receipt_write_failure_does_not_advance_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            def writer(path,data):
                if path.name=='deployment-receipt.json':raise OSError('synthetic receipt write failure')
                deploy.write_private(path,data)
            with self.assertRaisesRegex(OSError,'receipt write failure'):
                deploy.record_deployment(runtime,target,target_plan,{'status':'ok'},expected,'d'*32,writer=writer)
            self.assertEqual(deploy.current_snapshot(runtime),expected)

    def test_continuous_rollback_replaces_receipts_and_pointer_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            first='a'*40;second='b'*40
            expected=self._seed_deployment(runtime,first)
            _,second_plan,_=self._seed_state(runtime,second,'3'*64,'4'*64)
            first_plan=expected['plan_sha256']
            for sha,plan_hash in ((second,second_plan),(first,first_plan)):
                deploy.record_deployment(runtime,sha,plan_hash,{'status':'ok','git_commit':sha},expected,'d'*32)
                self.assertEqual((runtime/'DEPLOYED_GITHUB_COMMIT').read_text(),sha+'\n')
                state=runtime/'deploy-state'/sha
                self.assertEqual(state.stat().st_mode & 0o777,0o700)
                receipt=json.loads((state/'deployment-receipt.json').read_text())
                self.assertEqual(receipt['commit'],sha)
                self.assertEqual(receipt['plan_sha256'],plan_hash)
                expected=deploy.current_snapshot(runtime)

    def test_pointer_write_failure_leaves_current_pointer_receipt_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            def writer(path,data):
                if path.name=='DEPLOYED_GITHUB_COMMIT':raise OSError('synthetic pointer failure')
                deploy.write_private(path,data)
            with self.assertRaisesRegex(OSError,'pointer failure'):
                deploy.record_deployment(runtime,target,target_plan,{'status':'ok'},expected,'d'*32,writer=writer)
            self.assertEqual(deploy.current_snapshot(runtime),expected)

    def test_verify_failure_cannot_record_receipt_or_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            def container_verifier(_runtime,sha):
                if sha==target:raise RuntimeError('synthetic health failure')
                return {'status':'ok','git_commit':sha}
            with patch.object(deploy,'record_deployment') as record:
                with self.assertRaisesRegex(RuntimeError,'synthetic health failure'):
                    deploy.apply_release(runtime,target,target_plan,['docker','compose'],expected,target_compose,
                                         runner=lambda *args,**kwargs:None,container_verifier=container_verifier,
                                         post_verifier=lambda *args,**kwargs:None)
            record.assert_not_called()
            self.assertEqual(deploy.current_snapshot(runtime),expected)
            self.assertFalse(deploy.transaction_path(runtime).exists())

    def test_container_update_failure_cannot_record_receipt_or_pointer(self):
        calls=[]
        def runner(args,**kwargs):
            calls.append(args)
            if len(calls)==2:raise RuntimeError('synthetic compose failure')
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);old='a'*40;target='b'*40
            expected=self._seed_deployment(runtime,old)
            _,target_plan,target_compose=self._seed_state(runtime,target,'3'*64,'4'*64)
            with patch.object(deploy,'record_deployment') as record:
                with self.assertRaisesRegex(RuntimeError,'synthetic compose failure'):
                    deploy.apply_release(runtime,target,target_plan,['docker','compose'],expected,target_compose,
                                         runner=runner,container_verifier=lambda *args:{'status':'ok'},
                                         post_verifier=lambda *args:None)
            record.assert_not_called()
            self.assertEqual(deploy.current_snapshot(runtime),expected)

    def test_verify_accepts_exact_read_only_release_and_nginx_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            outputs.append(json.dumps({'status':'ok','git_commit':sha}))
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                self.assertEqual(deploy.verify(runtime,sha)['git_commit'],sha)

    def test_verify_rejects_writable_nginx_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            infos['owner-gateway']['Mounts'][0]['RW']=True
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                with self.assertRaisesRegex(RuntimeError,'Uncontrolled release mount: owner-gateway'):
                    deploy.verify(runtime,sha)

    def test_verify_rejects_nginx_mount_from_wrong_release(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory);sha='a'*40
            self._seed_deployment(runtime,sha)
            app,infos=self._complete_infos(runtime,sha)
            infos['share-gateway']['Mounts'][0]['Source']=str(runtime/'releases'/('b'*40)/'deploy/nginx/share.conf')
            outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
            with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
                with self.assertRaisesRegex(RuntimeError,'Uncontrolled release mount: share-gateway'):
                    deploy.verify(runtime,sha)

    def test_branch_name_cannot_be_deployed(self):
        with patch('sys.argv',['github_deploy','plan','master']),patch.object(deploy,'fetch') as fetch:
            with self.assertRaises(SystemExit):deploy.main()
            fetch.assert_not_called()

    def test_apply_without_reviewed_plan_cannot_fetch_or_restart(self):
        with patch('sys.argv',['github_deploy','apply','a'*40]),patch.object(deploy,'fetch') as fetch:
            with self.assertRaises(SystemExit):deploy.main()
            fetch.assert_not_called()

    def test_restarting_container_cannot_pass_verification(self):
        sha='a'*40
        info={'State':{'Running':False,'Restarting':True}}
        current={'commit':sha,'pointer_sha256':'1'*64,'receipt_sha256':'2'*64,'plan_sha256':'3'*64,'compose_sha256':'4'*64}
        with patch.object(deploy,'current_snapshot',return_value=current),patch.object(deploy,'run',return_value=json.dumps([info])):
            with self.assertRaisesRegex(RuntimeError,'not running stably'):deploy.verify(Path('/opt/literature-ai'),sha)

    def test_worker_must_pass_its_own_health_check(self):
        sha='a'*40
        backend={'State':{'Running':True,'Health':{'Status':'healthy'}},'Config':{'Labels':{'org.opencontainers.image.revision':sha}},'Mounts':[]}
        worker={'State':{'Running':True,'Health':{'Status':'unhealthy'}}}
        current={'commit':sha,'pointer_sha256':'1'*64,'receipt_sha256':'2'*64,'plan_sha256':'3'*64,'compose_sha256':'4'*64}
        with patch.object(deploy,'current_snapshot',return_value=current),patch.object(deploy,'run',side_effect=[json.dumps([backend]),json.dumps([worker])]):
            with self.assertRaisesRegex(RuntimeError,'health check has not passed: worker'):deploy.verify(Path('/opt/literature-ai'),sha)

if __name__=='__main__':unittest.main()

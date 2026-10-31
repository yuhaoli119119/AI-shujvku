import importlib.util,hashlib,json,tempfile,unittest
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
            pointer=runtime/'DEPLOYED_GITHUB_COMMIT'
            pointer.write_text('old\n')
            def writer(path,data):
                if path.name=='deployment-receipt.json':raise OSError('synthetic receipt write failure')
                deploy.write_private(path,data)
            with self.assertRaisesRegex(OSError,'receipt write failure'):
                deploy.record_deployment(runtime,'b'*40,'c'*64,{'status':'ok'},writer=writer)
            self.assertEqual(pointer.read_text(),'old\n')

    def test_continuous_rollback_replaces_receipts_and_pointer_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime=Path(directory)
            for sha,plan_hash in (('a'*40,'1'*64),('b'*40,'2'*64),('a'*40,'1'*64)):
                deploy.record_deployment(runtime,sha,plan_hash,{'status':'ok','git_commit':sha})
                self.assertEqual((runtime/'DEPLOYED_GITHUB_COMMIT').read_text(),sha+'\n')
                state=runtime/'deploy-state'/sha
                self.assertEqual(state.stat().st_mode & 0o777,0o700)
                receipt=json.loads((state/'deployment-receipt.json').read_text())
                self.assertEqual(receipt['commit'],sha)
                self.assertEqual(receipt['plan_sha256'],plan_hash)

    def test_verify_failure_cannot_record_receipt_or_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(deploy,'verify',side_effect=RuntimeError('synthetic health failure')), \
                 patch.object(deploy,'record_deployment') as record:
                with self.assertRaisesRegex(RuntimeError,'synthetic health failure'):
                    deploy.apply_release(Path(directory),'a'*40,'b'*64,['docker','compose'],runner=lambda *args,**kwargs:None)
            record.assert_not_called()

    def test_container_update_failure_cannot_record_receipt_or_pointer(self):
        calls=[]
        def runner(args,**kwargs):
            calls.append(args)
            if len(calls)==2:raise RuntimeError('synthetic compose failure')
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(deploy,'verify') as verify, patch.object(deploy,'record_deployment') as record:
                with self.assertRaisesRegex(RuntimeError,'synthetic compose failure'):
                    deploy.apply_release(Path(directory),'a'*40,'b'*64,['docker','compose'],runner=runner)
            verify.assert_not_called()
            record.assert_not_called()

    def test_verify_accepts_exact_read_only_release_and_nginx_mounts(self):
        runtime=Path('/opt/literature-ai');sha='a'*40
        app,infos=self._complete_infos(runtime,sha)
        outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
        outputs.append(json.dumps({'status':'ok','git_commit':sha}))
        with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
            self.assertEqual(deploy.verify(runtime,sha)['git_commit'],sha)

    def test_verify_rejects_writable_nginx_mount(self):
        runtime=Path('/opt/literature-ai');sha='a'*40
        app,infos=self._complete_infos(runtime,sha)
        infos['owner-gateway']['Mounts'][0]['RW']=True
        outputs=[json.dumps([infos[name]]) for name in deploy.SERVICES]
        with patch.object(deploy,'application_root',return_value=app),patch.object(deploy,'run',side_effect=outputs):
            with self.assertRaisesRegex(RuntimeError,'Uncontrolled release mount: owner-gateway'):
                deploy.verify(runtime,sha)

    def test_verify_rejects_nginx_mount_from_wrong_release(self):
        runtime=Path('/opt/literature-ai');sha='a'*40
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
        info={'State':{'Running':False,'Restarting':True}}
        with patch.object(deploy,'run',return_value=json.dumps([info])):
            with self.assertRaisesRegex(RuntimeError,'not running stably'):deploy.verify(Path('/opt/literature-ai'),'a'*40)

    def test_worker_must_pass_its_own_health_check(self):
        backend={'State':{'Running':True,'Health':{'Status':'healthy'}},'Config':{'Labels':{'org.opencontainers.image.revision':'a'*40}},'Mounts':[]}
        worker={'State':{'Running':True,'Health':{'Status':'unhealthy'}}}
        with patch.object(deploy,'run',side_effect=[json.dumps([backend]),json.dumps([worker])]):
            with self.assertRaisesRegex(RuntimeError,'health check has not passed: worker'):deploy.verify(Path('/opt/literature-ai'),'a'*40)

if __name__=='__main__':unittest.main()

import importlib.util,json,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('github_deploy',Path(__file__).parents[1]/'scripts/github_deploy.py')
deploy=importlib.util.module_from_spec(spec);spec.loader.exec_module(deploy)

class DeploymentBoundaryTests(unittest.TestCase):
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

    def test_branch_name_cannot_be_deployed(self):
        with patch('sys.argv',['github_deploy','plan','master']),patch.object(deploy,'fetch') as fetch:
            with self.assertRaises(SystemExit):deploy.main()
            fetch.assert_not_called()

    def test_apply_without_reviewed_plan_cannot_fetch_or_restart(self):
        with patch('sys.argv',['github_deploy','apply','a'*40]),patch.object(deploy,'fetch') as fetch:
            with self.assertRaises(SystemExit):deploy.main()
            fetch.assert_not_called()

if __name__=='__main__':unittest.main()

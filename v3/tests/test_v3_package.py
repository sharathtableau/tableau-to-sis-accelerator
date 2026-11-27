"""Candidate packaging and isolated execution; no deployment or live SQL."""
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import v3_package as P
import v3_runtime
from test_v3_tables import fixture,parse


class CandidateTests(unittest.TestCase):
    def test_snowflake_entry_cloud_binding_and_export_survive_reruns(self):
        import xml.etree.ElementTree as ET
        from streamlit.testing.v1 import AppTest
        payload={'bytes':ET.tostring(fixture()),'filename':'renamed.twb','workbook_id':'development-test'}
        with patch('backend._active_session',return_value=object()),patch('tableau_server.fetch_workbook',return_value=payload),patch('v3_source_binding.validate_table',return_value='DB.SCHEMA.T') as validated:
            app=AppTest.from_file(str(P.ROOT/'v3_sis_app.py')).run(timeout=30)
            self.assertFalse(app.exception)
            app.toggle(key='v3c_classic').set_value(True).run()
            app.radio(key='v3-source-mode').set_value('Tableau Cloud').run()
            app.radio(key='v3-cloud-mode').set_value('Direct workbook or view link').run()
            app.text_input[0].set_value('https://example.tableau.com/views/Test').run()
            next(b for b in app.button if b.label=='Pull and assess workbook').click().run(timeout=30)
            self.assertFalse(app.exception)
            for control in app.text_input:
                if control.label.startswith('Snowflake table/view'):control.set_value('DB.SCHEMA.T')
            app.run()
            next(b for b in app.button if b.label=='Build standalone candidate').click().run(timeout=30)
            self.assertFalse(app.exception)
            data=app.session_state['v3_export']['data']
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
            validated.assert_called()
            self.assertTrue(app.get('download_button'))

    def test_workbench_archive_has_isolated_target_and_explicit_artifacts(self):
        data,build=P.workbench_bundle()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names=set(z.namelist());manifest=json.loads(z.read('snowflake.yml'))
            entity=manifest['entities']['v3_candidate']
            self.assertEqual(entity['identifier'],P.TARGET)
            self.assertEqual(entity['main_file'],'v3_sis_app.py')
            self.assertEqual(entity['runtime_name'],'SYSTEM$WAREHOUSE_RUNTIME')
            self.assertNotIn('  - python=',z.read('environment.yml').decode())
            self.assertEqual(entity['external_access_integrations'],['TABLEAU_ACCESS_INTEGRATION'])
            self.assertEqual(entity['secrets'],{'tableau_pat':'WBR_DB.PUBLIC.TABLEAU_PAT_SECRET'})
            self.assertEqual(set(entity['artifacts']),names-{'snowflake.yml'})
            self.assertTrue({'filter_plan.py','field_identity.py','v3_runtime.py','v3_package.py','environment_v3.yml'}<=names)
            self.assertFalse(any(n.endswith(('.twb','.twbx','.csv','.toml')) or n=='datasources.json' for n in names))
            self.assertEqual(json.loads(z.read('candidate_build.json'))['build_sha256'],build)
            self.assertEqual(data,P.workbench_bundle()[0])

    def test_standalone_roundtrip_and_no_legacy_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(fixture(),folder);before=deepcopy(ir)
            bindings={cap:f'DB.SCHEMA.T{i}' for i,cap in enumerate(ir['datasources'])}
            data,_=P.workbook_bundle(ir,bindings)
            self.assertEqual(ir,before)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                captured=[]
                with patch.object(v3_runtime,'run_candidate',side_effect=lambda a,b:captured.append((a,b))):
                    exec(compile(z.read('app.py'),'app.py','exec'),{})
                self.assertEqual(captured[0][1],bindings)
                self.assertEqual(captured[0][0]['dashboards'],ir['dashboards'])
                self.assertRegex(captured[0][0]['source_file'],r'^[a-f0-9]{64}\.twb$')
                self.assertEqual(json.loads(z.read('candidate_build.json'))['acceptance'],'NOT_VERIFIED')

    def test_export_refuses_missing_extra_unsafe_and_unscoped_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(fixture(),folder);bindings={cap:'DB.SCHEMA.T' for cap in ir['datasources']}
            for bad in ({},dict(bindings,Unknown='DB.SCHEMA.T'),dict.fromkeys(bindings,'DB.SCHEMA.T;DROP')):
                with self.assertRaises(ValueError):P.workbook_bundle(ir,bad)
            bad=deepcopy(ir);bad.pop('fields');bad.pop('field_scopes')
            with self.assertRaises(ValueError):P.workbook_bundle(bad,bindings)

    def test_isolated_bundle_startup_and_export_without_repository_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(fixture(),folder)
            data,_=P.workbench_bundle()
            package=Path(folder)/'package';package.mkdir()
            with zipfile.ZipFile(io.BytesIO(data)) as z:z.extractall(package)
            (package/'fixture.json').write_text(json.dumps(ir))
            script="""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import v3_package
from streamlit.testing.v1 import AppTest
app=AppTest.from_file('v3_sis_app.py').run(timeout=30)
assert not app.exception
assert len(app.error)==1 and 'Snowpark' in app.error[0].value
ir=json.loads(Path('fixture.json').read_text())
data,build=v3_package.workbook_bundle(ir,{cap:'DB.SCHEMA.T' for cap in ir['datasources']})
assert data and build
print('ISOLATED_PACKAGE_PASS')
"""
            result=subprocess.run([sys.executable,'-I','-c',script],cwd=package,capture_output=True,text=True,timeout=90)
            self.assertEqual(result.returncode,0,result.stderr[-3000:])
            self.assertIn('ISOLATED_PACKAGE_PASS',result.stdout)

    def test_generated_runtime_requires_session_and_restores_configuration(self):
        import config,engine
        from streamlit.runtime.scriptrunner import StopException
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(fixture(),folder);bindings={cap:'DB.SCHEMA.T' for cap in ir['datasources']}
            before=deepcopy(config.DATASOURCES)
            with patch('backend._active_session',return_value=None),patch.object(v3_runtime.st,'error'),patch.object(v3_runtime.st,'stop',side_effect=StopException),patch.object(v3_runtime,'render_workbook') as run:
                with self.assertRaises(StopException):v3_runtime.run_candidate(ir,bindings)
                run.assert_not_called()
            def check(ir,ask_bindings=None):
                self.assertEqual(ask_bindings,bindings)
                self.assertEqual(set(config.DATASOURCES),set(bindings))
                self.assertTrue(all(v['local_file'] is None for v in config.DATASOURCES.values()))
            # DM6: object() cannot answer the runtime table re-check (tested in test_v3_dm6_drift); this test is about
            # the session requirement and restoring configuration
            with patch('backend._active_session',return_value=object()),patch('v3_local._validate_table_once'),patch.object(v3_runtime.st,'caption'),patch.object(v3_runtime.st,'title'),patch.object(v3_runtime.st,'set_page_config'),patch.object(v3_runtime,'render_workbook',side_effect=check):
                v3_runtime.run_candidate(ir,bindings)
            self.assertEqual(config.DATASOURCES,before)


if __name__=='__main__':unittest.main()

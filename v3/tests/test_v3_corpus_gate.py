"""EN07 "no failed run" gate: the generic fixes it found, and the runner's own classification."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import init_workbook as IW

BLEND = """<workbook><datasource-relationships>
  <datasource-relationship source='federated.a' target='federated.b'>
    <column-mapping><map key='[federated.a].[none:Product:nk]' value='[federated.b].[none:Product:nk]' /></column-mapping>
  </datasource-relationship></datasource-relationships>
  <datasources><datasource name='federated.c'><object-graph><relationships>
    <relationship><expression op='='><expression op='[customer_id]' /><expression op='[customer_id (Customers)]' /></expression>
      <first-end-point object-id='Events_0123456789ABCDEF0123' /><second-end-point object-id='Customers_0123456789ABCDEF0123' />
    </relationship></relationships></object-graph></datasource></datasources></workbook>"""


class RelationshipParsingTests(unittest.TestCase):
    def test_blending_links_are_not_object_model_relationships(self):
        root = ET.fromstring(BLEND)
        for strict in (False, True):
            rels = IW.parse_relationships(root, strict=strict)
            self.assertEqual(len(rels), 1, strict)
            self.assertEqual((rels[0]['lkey'], rels[0]['rkey']), ('customer_id', 'customer_id'))

    def test_a_malformed_object_model_relationship_is_still_refused_in_strict_mode(self):
        bad = BLEND.replace("<expression op='[customer_id]' />", "<expression op='[a] + [b]' />")
        with self.assertRaises(ValueError):
            IW.parse_relationships(ET.fromstring(bad), strict=True)


class FlattenTests(unittest.TestCase):
    def test_dimension_side_key_is_kept_under_tableaus_name(self):
        events = pd.DataFrame({'event_id': [1, 2, 3], 'customer_id': [10, 11, 99], 'revenue': [5.0, 6.0, 7.0]})
        customers = pd.DataFrame({'customer_id': [10, 11], 'region': ['EU', 'US']})
        tables = {'Events_0123456789ABCDEF0123': events, 'Customers_0123456789ABCDEF0123': customers}
        rels = [{'first': 'Events_0123456789ABCDEF0123', 'second': 'Customers_0123456789ABCDEF0123',
                 'lkey': 'customer_id', 'rkey': 'customer_id'}]
        for strict in (False, True):
            df, _log = IW.flatten_tables(tables, rels, strict=strict)
            self.assertEqual(len(df), 3)                                   # fact grain unchanged
            self.assertEqual(df['customer_id'].tolist(), [10, 11, 99])
            self.assertEqual(df['customer_id (Customers)'].tolist()[:2], [10, 11])
            self.assertTrue(pd.isna(df['customer_id (Customers)'].tolist()[2]))   # no match -> null, as in Tableau
            self.assertEqual(df['region'].tolist()[:2], ['EU', 'US'])


class RunnerClassificationTests(unittest.TestCase):
    def test_warning_instead_of_content_fails_the_gate(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        import corpus_runner as R
        ok = {'stages': {'Parsing': 'done'}, 'sheets': [{'outcome': 'Converted'}, {'outcome': 'Refused'}]}
        self.assertEqual(R.verdict(ok), 'PASS')
        self.assertEqual(R.verdict(dict(ok, sheets=[{'outcome': 'Failed'}])), 'FAIL')
        self.assertEqual(R.verdict(dict(ok, stages={'Parsing': 'failed'})), 'FAIL')
        self.assertEqual(R.verdict(dict(ok, error='run_pipeline raised ValueError')), 'FAIL')


class CloudTestSetTests(unittest.TestCase):
    def test_frozen_cloud_workbook_runs_through_the_console_cloud_path(self):
        import hashlib, json, shutil, tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        import corpus_runner as R
        src = Path(__file__).resolve().parents[1] / 'Workbooks' / 'Sales Dashboard Ver 01.twbx'
        if not src.exists():
            self.skipTest('corpus workbook not present')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'Any name.twbx'
            shutil.copy(src, target)
            entry = {'id': 'w-1', 'name': 'Any name', 'content_url': 'AnyName', 'project': 'P',
                     'server_url': 'https://example.test', 'site_content_url': 's', 'file': target.name,
                     'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'published_views': ['SALES']}
            (Path(tmp) / 'manifest.json').write_text(json.dumps({'workbooks': [entry]}))
            r = R.run_one(target)
            self.assertIsNone(r['error'], r['error'])
            self.assertEqual(r['cloud']['id'], 'w-1')
            self.assertEqual(R.verdict(r), 'PASS')
            target.write_bytes(target.read_bytes() + b'x')               # altered file: refused, not run
            self.assertEqual(R.verdict(R.run_one(target)), 'FAIL')

    def test_project_tree_includes_sub_projects_only_under_the_named_project(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        import fetch_cloud_project as F
        projects = [{'id': 'a', 'name': 'Tableau to Streamlit', 'parent_project_id': None},
                    {'id': 'b', 'name': 'Child', 'parent_project_id': 'a'},
                    {'id': 'c', 'name': 'Grandchild', 'parent_project_id': 'b'},
                    {'id': 'd', 'name': 'Other', 'parent_project_id': None}]
        ids, path = F.project_tree(projects, 'tableau to streamlit')
        self.assertEqual(ids, {'a', 'b', 'c'})
        self.assertEqual(path('c'), 'Tableau to Streamlit / Child / Grandchild')
        with self.assertRaises(SystemExit):
            F.project_tree(projects, 'Missing')


if __name__ == '__main__':
    unittest.main()

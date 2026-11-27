"""DM3: live Snowflake datasources that are a physical join, author custom SQL, or a relationship model whose column
metadata names the owner by caption. Written BEFORE the fix. A fake Snowpark session runs the compiled read-only SQL
on DuckDB (SANDBOX.DS.T -> SANDBOX_DS_T); expected values come from plain pandas on the same rows.

  * Physical join (Global_Sales SANDBOX): Tableau computes on the JOINED rows -- duplicates included -- so the plan
    is the authored join tree (left-deep, equality clauses), compiled once, verified (account, schemas, key types,
    a zero-row run) before binding.
  * Custom SQL (Regional Analysis): the author's SQL, one read-only SELECT (same guard as Ask-the-data), wrapped
    and projected column by column; writes, second statements, SYSTEM$ / EXECUTE / CALL and parameter
    placeholders refuse.
  * R9: <metadata-record> without <object-id> / <local-type>: the owner is the table whose caption is the
    parent-name (when exactly one), the type comes from the <column> declaration."""
from pathlib import Path
import re
import sys
import unittest

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

_TYPES = {'string': 'VARCHAR', 'real': 'DOUBLE', 'integer': 'BIGINT', 'boolean': 'BOOLEAN', 'date': 'DATE',
          'datetime': 'TIMESTAMP'}


class Session:
    """Snowpark-shaped fake over DuckDB; 3-part names become DB_SCHEMA_TABLE."""

    def __init__(self, server):
        host = server.split('.')[0]
        self.org, self.account = host.split('-', 1)
        self.con = duckdb.connect()
        self.calls, self.last = [], None

    def local(self, sql):
        return re.sub(r'\b([A-Z_][A-Z0-9_$]*)\.([A-Z_][A-Z0-9_$]*)\.([A-Z_][A-Z0-9_$]*)\b',
                      lambda m: '_'.join(m.groups()), sql)

    def table(self, fqn, frame):
        types = getattr(frame, 'attrs', {}).get('sql_types', {})
        self.con.register('_tmp', frame)
        cols = ', '.join(f'CAST("{c}" AS {types.get(c, "VARCHAR")}) AS "{c}"' for c in frame.columns)
        self.con.execute(f'CREATE TABLE {self.local(fqn)} AS SELECT {cols} FROM _tmp')
        self.con.unregister('_tmp')

    def sql(self, text):
        self.calls.append(text)
        self.query = text
        return self

    def collect(self):
        q = self.query
        if q.startswith('SELECT CURRENT'):
            return [{'ORG': self.org, 'ACCOUNT_NAME': self.account, 'LOCATOR': 'x', 'REGION': 'r'}]
        if q.startswith('DESCRIBE TABLE'):
            res = self.con.execute('DESCRIBE ' + self.local(q.split()[-1])).fetchall()
            return [{'NAME': r[0], 'TYPE': r[1]} for r in res]
        if q.startswith('DESCRIBE RESULT'):
            return [{'NAME': d[0], 'TYPE': str(d[1])} for d in self.last]
        res = self.con.execute(self.local(q))
        self.last = res.description
        return [dict(zip([c[0] for c in res.description], row)) for row in res.fetchall()]


def datasource(path, caption_part):
    import tableau_parser
    root = tableau_parser.load_twb_xml(str(ROOT / 'Workbooks' / path))
    return root, next(d for d in root.findall('./datasources/datasource') if caption_part in (d.get('caption') or ''))


def columns_for(plan, owner, data):
    """Fixture frame with every remote column the plan projects for this table (typed), test-owned values."""
    n = len(next(iter(data.values())))
    frame = {}
    for f in plan['fields'].values():
        if f['owner'] != owner or f['remote'] in frame:
            continue
        frame[f['remote']] = data.get(f['remote'], [None] * n)
    df = pd.DataFrame(frame)
    df.attrs['sql_types'] = {f['remote']: _TYPES.get(f['datatype'], 'VARCHAR') for f in plan['fields'].values()
                             if f['owner'] == owner}
    return df


class PhysicalJoin(unittest.TestCase):
    def test_global_sales_join_tree(self):
        import v3_model_binding as M
        root, ds = datasource('cloud_20261010/Global_Sales_Dashboard.twbx', 'SAMPLE_SUPER')
        plan = M.build_live(ds)
        self.assertEqual(plan['kind'], 'physical-join')
        leaves = {t['name']: t['table'] for t in plan['tables'].values()}
        self.assertEqual(leaves['SAMPLE_SUPER_STORE_RETURNS'], 'SANDBOX.DS.SAMPLE_SUPER_STORE_RETURNS')
        orders = pd.DataFrame({'ORDER_ID': ['O1', 'O1', 'O2', 'O3'], 'REGION': ['East', 'East', 'West', 'South'],
                               'SALES': [10.0, 5.0, 7.0, 3.0]})
        people = pd.DataFrame({'REGION': ['East', 'West'], 'REGIONAL_MANAGER': ['Ann', 'Bob']})
        returns = pd.DataFrame({'ORDER_ID': ['O1', 'O1'], 'RETURNED': [True, True]})       # a repeated key
        s = Session(plan['server'])
        by = {t['name']: k for k, t in plan['tables'].items()}
        for name, df in (('SAMPLE_SUPER_STORE_ORDERS', orders), ('SAMPLE_SUPER_STORE_PEOPLE', people),
                         ('SAMPLE_SUPER_STORE_RETURNS', returns)):
            s.table(plan['tables'][by[name]]['table'], columns_for(plan, by[name], {c: df[c].tolist() for c in df}))
        report = M.validate(s, plan)
        want = orders.merge(people, on='REGION', how='left').merge(returns, on='ORDER_ID', how='left')
        self.assertEqual(report['output_rows'], len(want))                      # 6: Tableau joins at row level
        got = pd.DataFrame(s.sql('SELECT REGIONAL_MANAGER, SUM(SALES) AS V FROM (' + M.compile_query(plan) +
                                 ') GROUP BY 1').collect())
        exp = want.groupby('REGIONAL_MANAGER', dropna=False)['SALES'].sum()
        self.assertEqual(dict(zip(got['REGIONAL_MANAGER'].fillna('-'), got['V'])),
                         dict(zip(exp.index.fillna('-'), exp.values)))
        self.assertTrue(all(not q.upper().lstrip().startswith(('CREATE', 'INSERT', 'DROP', 'ALTER')) for q in s.calls))

    def test_unsupported_join_shapes_refuse(self):
        import v3_model_binding as M
        for mutate in (lambda d: d.find('.//relation[@type="join"]').set('join', 'cross'),
                       lambda d: d.find('.//relation[@type="join"]/clause/expression').set('op', '<'),
                       lambda d: d.find('.//relation[@type="table"]').set('table', '[SANDBOX].[DS].[X;DROP]')):
            _root, ds = datasource('cloud_20261010/Global_Sales_Dashboard.twbx', 'SAMPLE_SUPER')
            mutate(ds)
            with self.assertRaises(ValueError):
                M.compile_query(M.build_live(ds))


class CustomSQL(unittest.TestCase):
    def test_regional_custom_sql(self):
        import v3_model_binding as M
        root, ds = datasource('cloud_20261010/Regional_Analysis.twb', 'Custom-SQL')
        plan = M.build_live(ds)
        self.assertEqual(plan['kind'], 'custom-sql')
        s = Session(plan['server'])
        owner = next(iter({f['owner'] for f in plan['fields'].values()}))
        data = {'SALES': [1.0, 2.0, 4.0], 'REGION': ['East', 'West', 'East']}
        frame = columns_for(plan, owner, data)
        s.table('SANDBOX.DS.SAMPLE_SUPER_STORE', frame)
        report = M.validate(s, plan)
        self.assertEqual(report['output_rows'], 3)
        got = s.sql('SELECT REGION, SUM(SALES) AS V FROM (' + M.compile_query(plan) + ') GROUP BY 1 ORDER BY 1').collect()
        self.assertEqual([(r['REGION'], r['V']) for r in got], [('East', 5.0), ('West', 2.0)])

    def test_anything_but_one_read_only_select_refuses(self):
        import v3_model_binding as M
        for text in ('delete from SANDBOX.DS.T', 'select 1; drop table SANDBOX.DS.T', 'select system$wait(5)',
                     "execute immediate 'drop table t'", 'select * from T where x = <[Parameters].[P]>'):
            _root, ds = datasource('cloud_20261010/Regional_Analysis.twb', 'Custom-SQL')
            ds.find('.//relation[@type="text"]').text = text
            with self.assertRaises(ValueError, msg=text):
                M.compile_query(M.build_live(ds))


class CaptionOwnedRelationshipModel(unittest.TestCase):
    def test_owner_by_caption_when_object_id_is_absent(self):
        import xml.etree.ElementTree as ET
        import v3_model_binding as M
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_v3_model_binding import Session as Fake, fixture
        ds = fixture()
        for mr in ds.iter('metadata-record'):                 # the shape older Tableau versions write
            oid = mr.find('object-id')
            ET.SubElement(mr, 'parent-name').text = oid.text
            mr.remove(oid)
            mr.remove(mr.find('local-type'))
        plan = M.build(ds)
        self.assertEqual({f['owner'] for f in plan['fields'].values()}, {'Fact', 'Dim'})
        self.assertEqual(M.validate(Fake(), plan)['output_rows'], 3)

    def test_r9_inconsistent_metadata_still_refuses(self):
        """R9 (hand-written) declares [PRODUCT_ID] on two tables while its relationship names [PRODUCT_ID (Product)],
        which no column record has -- genuinely ambiguous, so it refuses (Tableau itself writes the suffix)."""
        import v3_model_binding as M
        _root, ds = datasource('R9_Live_Join_Orders_Product_Category.twb', 'R9')
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            M.build(ds)


class Binding(unittest.TestCase):
    def test_resolve_sources_offers_each_as_a_typed_plan(self):
        import v3_source_binding as SB
        import plan_census as PC
        for path, part, kind in (('cloud_20261010/Global_Sales_Dashboard.twbx', 'SAMPLE_SUPER', 'physical-join'),
                                 ('cloud_20261010/Regional_Analysis.twb', 'Custom-SQL', 'custom-sql')):
            root, ds = datasource(path, part)
            ir = PC.intake(ROOT / 'Workbooks' / path)['ir']
            item = SB.source_candidates(root, ir)[ds.get('name')]
            self.assertEqual((item.get('model_candidate') or {}).get('kind'), kind, item.get('reason'))


if __name__ == '__main__':
    unittest.main()

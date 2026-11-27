"""E04 relationships whose lookup keys repeat. Written BEFORE the fix (tests first).

Tableau relationships are contextual: a related table is queried only when a sheet uses its fields, and a lookup
table never multiplies the fact's measures. The flatten joins lookup tables at the fact's grain, which is exact only
when their keys are unique; when a table's keys repeat (Superstore Returns: one row per returned line, 800 rows for
296 orders) the whole datasource was refused, so every sheet was unreachable even though none used Returns.

Rule: a lookup table whose keys repeat (and every table reached through it) is LEFT OUT of the flatten, never joined
(that would inflate the fact). Sheets that do not use its fields run with Tableau's numbers; a sheet that uses one
refuses (its fields do not resolve). The evidence names the table and why."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import init_workbook as IW  # noqa: E402

H = '0123456789ABCDEF0123'


def model():
    orders = pd.DataFrame({'Order ID': ['A', 'A', 'B', 'C'], 'Region': ['East', 'East', 'West', 'East'],
                           'Sales': [10.0, 5.0, 7.0, 3.0]})
    people = pd.DataFrame({'Region': ['East', 'West'], 'Manager': ['Ann', 'Bob']})
    returns = pd.DataFrame({'Order ID': ['A', 'A', 'C'], 'Returned': ['Yes', 'Yes', 'Yes']})
    reasons = pd.DataFrame({'Returned': ['Yes'], 'Reason': ['Damaged']})        # reached only through Returns
    tables = {f'Orders_{H}': orders, f'People_{H}': people, f'Returns_{H}': returns, f'Reasons_{H}': reasons}
    rels = [{'first': f'Orders_{H}', 'second': f'People_{H}', 'lkey': 'Region', 'rkey': 'Region'},
            {'first': f'Orders_{H}', 'second': f'Returns_{H}', 'lkey': 'Order ID', 'rkey': 'Order ID'},
            {'first': f'Returns_{H}', 'second': f'Reasons_{H}', 'lkey': 'Returned', 'rkey': 'Returned'}]
    return tables, rels


class Flatten(unittest.TestCase):
    def test_repeating_lookup_keys_leave_the_table_out(self):
        tables, rels = model()
        ev = {}
        df, log = IW.flatten_tables(tables, rels, strict=True, evidence=ev)
        self.assertEqual(len(df), 4)                                       # fact grain, nothing inflated
        self.assertEqual(df['Sales'].sum(), 25.0)
        self.assertEqual(df['Manager'].tolist(), ['Ann', 'Ann', 'Bob', 'Ann'])
        self.assertNotIn('Returned', df.columns)
        self.assertNotIn('Reason', df.columns)
        out = {e['table']: e for e in ev['left_out']}
        self.assertEqual(set(out), {f'Returns_{H}', f'Reasons_{H}'})
        self.assertEqual((out[f'Returns_{H}']['rows'], out[f'Returns_{H}']['distinct_keys']), (3, 2))
        self.assertIn('Returns', log)

    def test_unique_keys_still_join_and_null_keys_still_refuse(self):
        tables, rels = model()
        tables[f'Returns_{H}'] = tables[f'Returns_{H}'].drop_duplicates()
        ev = {}
        df, _log = IW.flatten_tables(tables, rels, strict=True, evidence=ev)
        self.assertEqual(df['Returned'].fillna('-').tolist(), ['Yes', 'Yes', '-', 'Yes'])
        self.assertEqual(ev.get('left_out', []), [])
        tables[f'People_{H}'] = pd.DataFrame({'Region': ['East', None], 'Manager': ['Ann', 'Bob']})
        with self.assertRaises(ValueError):
            IW.flatten_tables(tables, rels, strict=True)


def wb(name):
    return ROOT / 'Workbooks' / 'cloud_20261010' / name


@unittest.skipUnless(wb('R1_Test_Upload_-_Superstore.twbx').exists(), 'cloud corpus workbook')
class Corpus(unittest.TestCase):
    """R1_Test_Upload (and its V2 copy): 'Sample - Superstore' = Orders + People + Returns; no sheet uses Returns."""

    def check(self, name):
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(wb(name))
        cap = 'Sample - Superstore'
        self.assertTrue(r['bindings'].get(cap), r.get('intake_errors'))
        rep = next(x for x in r['data_model_reports'] if x.get('state') == 'DECODED' and x.get('left_out'))
        self.assertEqual([e['table'].split('_')[0] for e in rep['left_out']], ['Returns'])
        self.assertEqual(rep['output_rows'], rep['table_rows'][next(t for t in rep['table_rows'] if t.startswith('Orders'))])
        ir = r['ir']
        did = next(d for d, s in ir['field_scopes'].items() if s.get('caption') == cap)
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets'] if s.get('datasource_id') == did}
        self.assertGreaterEqual(len(sheets), 10)
        ran = 0
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            for s in sheets.values():
                df = P.execute(P.from_sheet(s, ir), s, engine)
                ran += 1 if len(df) else 0
            s = next(iter(sheets.values()))
            with engine.field_scope(s):
                total = engine.q(f'SELECT SUM(SALES) AS S, COUNT(*) AS N FROM {engine.tbl(s)}')
        self.assertEqual(ran, len(sheets))
        self.assertEqual(int(total['N'].iloc[0]), 10194)                       # Orders rows, not 12,620
        return float(total['S'].iloc[0])

    def test_r1_and_v2(self):
        a = self.check('R1_Test_Upload_-_Superstore.twbx')
        b = self.check('R1_Test_Upload_-_Superstore-V2.twbx')
        self.assertAlmostEqual(a, b, places=4)


    def test_a_sheet_using_a_left_out_field_refuses_clearly(self):
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(wb('R1_Test_Upload_-_Superstore.twbx'))
        ir = r['ir']
        fid = 'federated.10nnk8d1vgmw8q17yu76u06pnbcj::Order%20ID%20%28Returns%29'
        self.assertIn('Returns', ir['fields'][fid].get('unavailable', ''))
        # DM4: the comparison plan computes such a sheet per table (test_v3_dm4_relationship_query); the flat
        # dashboard path, which reads the flattened table, still refuses the field with the reason
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Sales by Segment')
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            with self.assertRaises(ValueError) as cm:
                with engine.field_scope(dict(s, field_bindings={'Order ID (Returns)': fid})):
                    pass
        self.assertIn('Returns', str(cm.exception))
        self.assertNotIn('Binder', str(cm.exception))


class MultiFact(unittest.TestCase):
    """DM4: Orders -> Product <- Targets (two facts, one shared dimension). The largest fact is flattened with what it
    reaches; the other fact is never joined (it would multiply Orders), it is a related table of role 'fact' linked to
    the shared dimension; a table reached only through it is left out. Cycles still refuse."""

    def model(self):
        orders = pd.DataFrame({'pid': ['P1', 'P1', 'P2'], 'Sales': [1.0, 2.0, 4.0]})
        product = pd.DataFrame({'pid': ['P1', 'P2'], 'Name': ['Pen', 'Ink']})
        targets = pd.DataFrame({'tpid': ['P1', 'P2'], 'Target': [10.0, 20.0], 'mgr': ['M1', 'M1']})
        mgrs = pd.DataFrame({'mgr': ['M1'], 'Boss': ['Ann']})
        t = {f'Orders_{H}': orders, f'Product_{H}': product, f'Targets_{H}': targets, f'Mgr_{H}': mgrs}
        rels = [{'first': f'Orders_{H}', 'second': f'Product_{H}', 'lkey': 'pid', 'rkey': 'pid'},
                {'first': f'Targets_{H}', 'second': f'Product_{H}', 'lkey': 'tpid', 'rkey': 'pid'},
                {'first': f'Targets_{H}', 'second': f'Mgr_{H}', 'lkey': 'mgr', 'rkey': 'mgr'}]
        return t, rels

    def test_other_fact_is_related_not_joined(self):
        t, rels = self.model()
        ev = {}
        df, _log = IW.flatten_tables(t, rels, strict=True, evidence=ev)
        self.assertEqual((len(df), df['Sales'].sum()), (3, 7.0))
        self.assertNotIn('Target', df.columns)
        out = {e['table']: e for e in ev['left_out']}
        fact = out[f'Targets_{H}']
        self.assertEqual((fact['role'], fact['parent'], fact['parent_key'], fact['key']),
                         ('fact', f'Product_{H}', 'pid', 'tpid'))
        self.assertIn('reached only through', out[f'Mgr_{H}']['reason'])

    def test_cycles_still_refuse(self):
        t = {f'{n}_{H}': pd.DataFrame({'k': [1]}) for n in 'ABC'}
        rels = [{'first': f'A_{H}', 'second': f'B_{H}', 'lkey': 'k', 'rkey': 'k'},
                {'first': f'B_{H}', 'second': f'C_{H}', 'lkey': 'k', 'rkey': 'k'},
                {'first': f'C_{H}', 'second': f'A_{H}', 'lkey': 'k', 'rkey': 'k'}]
        with self.assertRaises(ValueError):
            IW.flatten_tables(t, rels, strict=True)


if __name__ == '__main__':
    unittest.main()

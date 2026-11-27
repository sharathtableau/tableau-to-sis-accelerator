"""DM4: per-table relationship queries for a sheet that USES a related table the flatten left out. Written BEFORE the
query was wired (tests first). Corpus: cloud R1_Test_Upload and its V2 copy -- 'Sample - Superstore' = Orders (10,194
lines, 5,111 orders) + People + Returns (800 rows for 296 orders: the key repeats). Tableau's relationship semantics:
each measure from its own table's rows at the view's dimensions; the other table only as DISTINCT (key, needed
columns). Expected values come from pandas on the decoded rows."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

CAP = 'Sample - Superstore'
REL = CAP + ' [Returns]'


def load(name):
    import plan_census as PC
    r = PC.intake(ROOT / 'Workbooks' / 'cloud_20261010' / name)
    ir = r['ir']
    did = next(d for d, s in ir['field_scopes'].items() if s.get('caption') == CAP)
    sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s.get('datasource_id') == did)
    fid = lambda pred: next(k for k, e in ir['fields'].items() if e.get('datasource_id') == did and pred(k, e))
    return r, ir, dict(sheet, applied_filters=[]), fid


def field(caption, fid, kind, agg, channel):
    return {'caption': caption, 'field_id': fid, 'kind': kind, 'agg': agg, 'channel': channel, 'date_part': 'none',
            'discrete': kind == 'dim'}


def run(r, ir, sheet, fields):
    import engine
    import v3_sheet_plan as P
    from v3_local import preview_context
    plan = dict(P.from_sheet(sheet, ir), fields=fields, measure_values=[], sort=[])
    with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
        return P.execute(plan, sheet, engine)


class RelationshipQuery(unittest.TestCase):
    def frames(self, r):
        orders = pd.read_csv(r['bindings'][CAP])
        returns = pd.read_csv(r['bindings'][REL])
        return orders, returns

    def check_workbook(self, name):
        r, ir, sheet, fid = load(name)
        orders, returns = self.frames(r)
        self.assertEqual((len(returns), returns['Order ID'].nunique()), (800, 296))
        region = fid(lambda k, e: e.get('caption') == 'Region' and not e.get('related'))
        sales = fid(lambda k, e: e.get('caption') == 'Sales')
        count = fid(lambda k, e: (e.get('related') or {}).get('count'))
        ret_id = fid(lambda k, e: e.get('caption') == 'Order ID (Returns)')

        # 1. a measure from each table at a parent dimension
        df = run(r, ir, sheet, [field('Region', region, 'dim', 'none', 'rows'),
                                field('Sales', sales, 'measure', 'sum', 'text'),
                                field('Returns', count, 'measure', 'cnt', 'text')])
        got = df.set_index('D|none|Region')
        want_sales = orders.groupby('Region')['Sales'].sum()
        bridge = orders[['Order ID', 'Region']].drop_duplicates()
        want_ret = returns.merge(bridge, on='Order ID', how='left').groupby('Region').size()
        self.assertEqual(set(got.index), set(want_sales.index))
        for reg in want_sales.index:
            self.assertAlmostEqual(got.loc[reg, 'M|sum|Sales'], want_sales[reg], places=6)
            self.assertEqual(int(got.loc[reg, 'M|cnt|Returns']), int(want_ret.get(reg, 0)))
        self.assertEqual(int(got['M|cnt|Returns'].sum()), 800)           # every return counted once
        self.assertAlmostEqual(got['M|sum|Sales'].sum(), orders['Sales'].sum(), places=4)   # nothing inflated

        # 2. a related table's dimension with a parent measure: distinct keys, never a row per return line
        df = run(r, ir, sheet, [field('Order ID (Returns)', ret_id, 'dim', 'none', 'rows'),
                                field('Sales', sales, 'measure', 'sum', 'text')])
        flag = orders.merge(returns[['Order ID']].drop_duplicates().assign(R=lambda x: x['Order ID']),
                            on='Order ID', how='left')
        want = flag.groupby(flag['R'].fillna('<null>'))['Sales'].sum()
        got = df.set_index(df['D|none|Order ID (Returns)'].fillna('<null>'))['M|sum|Sales']
        self.assertEqual(len(got), 297)                                   # 296 returned orders + not returned
        for k, v in want.items():
            self.assertAlmostEqual(got[k], v, places=6)
        self.assertAlmostEqual(got.sum(), orders['Sales'].sum(), places=4)
        return float(orders['Sales'].sum())

    def test_r1_and_v2(self):
        a = self.check_workbook('R1_Test_Upload_-_Superstore.twbx')
        b = self.check_workbook('R1_Test_Upload_-_Superstore-V2.twbx')
        self.assertAlmostEqual(a, b, places=4)

    def test_filter_on_a_related_field_applies(self):
        """C12 (was refused): a filter on a related table's field keeps that table's matching rows and the parent
        rows they relate to; each measure still from its own table's rows."""
        r, ir, sheet, fid = load('R1_Test_Upload_-_Superstore.twbx')
        orders, returns = self.frames(r)
        ret_id = fid(lambda k, e: e.get('caption') == 'Order ID (Returns)')
        sales = fid(lambda k, e: e.get('caption') == 'Sales')
        pick = sorted(returns['Order ID'].unique())[:3]
        s = dict(sheet, applied_filters=[{'caption': 'Order ID (Returns)', 'kind': 'in', 'values': pick,
                                          'field_id': ret_id}])
        df = run(r, ir, s, [field('Order ID (Returns)', ret_id, 'dim', 'none', 'rows'),
                            field('Sales', sales, 'measure', 'sum', 'text')])
        got = df.set_index('D|none|Order ID (Returns)')['M|sum|Sales']
        want = orders[orders['Order ID'].isin(pick)].groupby('Order ID')['Sales'].sum()
        self.assertEqual(set(got.index), set(pick))
        for k in pick:
            self.assertAlmostEqual(float(got[k]), float(want[k]), 6)

    def test_sheets_using_related_fields_are_planned(self):
        """A sheet the dashboard draws directly is handed to its exact plan once it uses a related table's field."""
        import v3_local
        r, ir, _sheet, fid = load('R1_Test_Upload_-_Superstore.twbx')
        ret_id = fid(lambda k, e: e.get('caption') == 'Order ID (Returns)')
        did = next(d for d, sc in ir['field_scopes'].items() if sc.get('caption') == CAP)
        s = next(s for d in ir['dashboards'] for s in d['sheets']
                 if s.get('datasource_id') == did and s.get('kind') != 'planned')         # the KPI card
        occ = s['worksheet_semantics']['occurrences']
        occ.append(dict(occ[0], caption='Order ID (Returns)', field_id=ret_id, channel='rows', aggregation='none',
                        discrete=True))
        from unittest.mock import patch
        with patch('v3_exact_loop.attach_plans') as attach:          # attach_plans itself is tested elsewhere
            v3_local._plan_related_sheets(ir, 'workbook.twbx')
        self.assertEqual(s['support']['status'], 'UNSUPPORTED')
        self.assertIn('related table', s['support']['reason'])
        attach.assert_called_once()

if __name__ == '__main__':
    unittest.main()

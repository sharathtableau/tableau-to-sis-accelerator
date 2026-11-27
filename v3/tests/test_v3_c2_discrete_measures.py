"""C2: a DISCRETE aggregated measure pill (sum:Sales:ok on Rows -- a header) is computed like a measure at the view's
level of detail and shown as a header; it was planned as a plain dimension, i.e. GROUP BY every raw value. Only
aggregate calculations (usr:) were treated that way. Written BEFORE the fix; expected values from pandas."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


class DiscreteMeasure(unittest.TestCase):
    def test_globalsales_discrete_sum_is_a_header_at_view_lod(self):
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(ROOT / 'Workbooks' / 'Globalsalesdashboard.twbx')
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Details Standard Class')
        occ = s['worksheet_semantics']['occurrences']
        keep = [o for o in occ if o['caption'] in ('REGION', 'STATE')]
        sales = next(fid for fid, e in ir['fields'].items() if e.get('datasource_id') == s['datasource_id']
                     and e.get('caption') == 'SALES' and not e.get('is_calculation'))
        disc = dict(keep[0], caption='SALES', field_id=sales, aggregation='sum', discrete=True, channel='rows',
                    field_definition=dict(keep[0].get('field_definition') or {}, datatype='real', role='measure'))
        s2 = dict(s, applied_filters=[], worksheet_semantics=dict(s['worksheet_semantics'], occurrences=keep + [disc]))
        plan = P.from_sheet(s2, ir)
        f = next(f for f in plan['fields'] if f['caption'] == 'SALES')
        self.assertTrue(f.get('aggregate_dimension'), f)
        b = {k: v for k, v in r['bindings'].items() if v}
        with preview_context(ir, b):
            df = P.execute(plan, s2, engine)
        raw = pd.read_csv(b[ir['field_scopes'][s['datasource_id']]['caption']])
        want = raw.groupby(['REGION', 'STATE'])['SALES'].sum()
        col = next(c for c in df.columns if c.endswith('|SALES'))
        got = df.set_index(['D|none|REGION', 'D|none|STATE'])[col]
        self.assertEqual(len(got), len(want))                       # one row per mark, not per raw Sales value
        for k, v in want.items():
            self.assertAlmostEqual(got[k], v, places=4)


if __name__ == '__main__':
    unittest.main()

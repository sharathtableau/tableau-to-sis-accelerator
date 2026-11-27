"""C9: a sheet reads only the tables its fields come from. Written BEFORE the fix (tests first).

A sheet whose fields (followed through its calculations and filters) all come from ONE related table left out of
the flatten is answered from that table alone -- Tableau queries only the tables a viz uses (Retails Dashboard: a
many-to-many Customer -- Orders model; its KPI / category sheets use Orders only).
Rule (general): when every field of a sheet -- followed through its calculations and filters -- comes from one
related table left out of the flatten (and that table has data), the sheet is answered from that table alone. A sheet
reading both tables keeps the per-table relationship query (DM4) or refuses with its reason. (An earlier draft that
re-oriented 1 -- * relationships was dropped: it multiplied the one side's own measures, which E04 forbids.)
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


class TablesUsed(unittest.TestCase):
    """The tables a sheet reads, through calculations (any depth) and filters."""
    def test_rule(self):
        import v3_sheet_plan as P
        D = 'ds'
        ir = {'fields': {
            f'{D}::Sales': {'related': {'source': 'R', 'column': 'SALES'}},
            f'{D}::Date': {'related': {'source': 'R', 'column': 'DATE'}},
            f'{D}::Name': {},
            f'{D}::Calc_1': {'is_calculation': True, 'formula': 'SUM([Sales]) * [Parameters].[P]'},
            f'{D}::Calc_2': {'is_calculation': True, 'formula': '[Calc_1] / 2 + YEAR([Date])'},
            f'{D}::Mixed': {'is_calculation': True, 'formula': '[Calc_2] + LEN([Name])'}}}
        sheet = lambda **b: {'datasource_id': D, 'field_bindings': {k: f'{D}::{v}' for k, v in b.items()}}
        self.assertEqual(P.tables_used(sheet(a='Calc_2'), ir), {'R'})
        self.assertEqual(P.tables_used(sheet(a='Calc_2', b='Name'), ir), {'R', 'parent'})
        self.assertEqual(P.tables_used(sheet(a='Mixed'), ir), {'R', 'parent'})
        s = dict(sheet(a='Sales'), applied_filters=[{'caption': 'Name', 'field_id': f'{D}::Name'}])
        self.assertEqual(P.tables_used(s, ir), {'R', 'parent'})                   # a filter counts too


class Corpus(unittest.TestCase):
    def test_retails_dashboard_kpis(self):
        import glob
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Retails Dashboard*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        sh = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            for name in ('Total Sales', 'Category', 'Sales By Country', 'Last Updated'):
                self.assertEqual(P.tables_used(sh[name], ir) - {'parent'}, P.tables_used(sh[name], ir), name)
                df = P.execute(P.from_sheet(sh[name], ir), sh[name], engine)
                self.assertGreater(len(df), 0, name)
            # a sheet that reads BOTH tables (Customer Name beside order fields, a filter on State): the per-table
            # relationship query with the related-table filter (C12; values checked in test_v3_c12)
            self.assertGreater(len(P.execute(P.from_sheet(sh['Tabular data'], ir), sh['Tabular data'], engine)), 0)
            src = P._single_related(sh['Total Sales'], engine)
            s = dict(sh['Total Sales'], datasource=src, _related_only=src)
            with engine.field_scope(s):
                n = engine.q(f'SELECT COUNT(*) AS N, SUM(SALES) AS S FROM {engine.tbl(s)}')
        self.assertEqual(int(n['N'].iloc[0]), 37907)                   # Orders' own rows: nothing joined


if __name__ == '__main__':
    unittest.main()

"""DSF: workbook-wide filters. Written BEFORE the fix (tests first).

Tableau has two filters that live outside a worksheet and still apply to it:
  * "Apply to worksheets -> All using this data source": stored once in <shared-views><shared-view name=DS>,
    applied to every worksheet whose data source is DS (an ordinary dimension filter there);
  * data source filters: <datasource><filter> (direct children), applied before everything else in the order
    of operations (before context filters, FIXED LODs, top N) -- carried as context filters.
The parser read neither, so every sheet ignored them (11 Nov sheet census: Fil Test, all 5 sheets differ; its
'Year(Order Date) = 2022' shared-view filter). Rule (general, from the XML): each such filter is appended to the
applied_filters of every worksheet of that data source, unless the worksheet filters the same field itself.
Proof: Fil Test against Tableau's own capture values; second workbooks: Customer Opt-In (shared-view on a
calculation), BDO (data source filters) and Clara Brazil (both)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'
SWEEP = ROOT / 'Workbooks' / '_powerbi_project_sweep'


def sheets(ir):
    return {s['name']: s for d in ir['dashboards'] for s in d['sheets']}


@unittest.skipUnless((CLOUD / 'Fil_Test.twbx').exists(), 'cloud corpus workbook')
class FilTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import plan_census as PC
        cls.r = PC.intake(CLOUD / 'Fil_Test.twbx')

    def test_every_sheet_carries_the_shared_view_filter(self):
        for name, s in sheets(self.r['ir']).items():
            f = [x for x in s.get('applied_filters') or [] if x['caption'] == 'Order Date']
            self.assertEqual(len(f), 1, name)
            self.assertEqual((f[0]['kind'], f[0]['values'], f[0].get('datepart'), f[0].get('scope')),
                             ('in', ['2022'], 'yr', 'all_using_datasource'), name)
            self.assertFalse(f[0]['context'])

    def test_values_equal_tableau(self):
        """Tableau's own numbers (Embedding API capture of the published view, 11 Nov)."""
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        ir, sh = self.r['ir'], sheets(self.r['ir'])
        want = {'Sheet 2': {1: 49.84, 2: 62.75, 3: -809.18, 4: -220.63},
                'Sheet 5': {1: 51122.70, 2: 45465.65, 3: 51406.39, 4: 91290.94}}
        with preview_context(ir, {k: v for k, v in self.r['bindings'].items() if v}):
            for name, values in want.items():
                df = P.execute(P.from_sheet(sh[name], ir), sh[name], engine)
                got = {int(a): round(float(b), 2) for a, b in df.values}
                for k, v in values.items():
                    self.assertAlmostEqual(got[k], v, places=1, msg=f'{name} {k}')
            df = P.execute(P.from_sheet(sh['Sheet 1'], ir), sh['Sheet 1'], engine)
            got = {a: round(float(b), 2) for a, b in df.values}
        for k, v in {'Tables': 11324.53, 'Machines': 14636.39, 'Storage': 16348.36, 'Phones': 21727.98}.items():
            self.assertAlmostEqual(got[k], v, places=1, msg=f'Sheet 1 {k}')
        self.assertEqual(len(got), 5)


class SecondWorkbooks(unittest.TestCase):
    def ir(self, name):
        import tableau_parser as TP
        path = SWEEP / name
        if not path.exists():
            self.skipTest('corpus workbook')
        return TP.build_ir(str(path), scoped=True)

    def test_customer_opt_in_shared_view_filter_on_a_calculation(self):
        ir = self.ir('Customer Opt-In_Out Analysis.twbx')
        tagged = [(n, f['caption']) for n, s in sheets(ir).items() for f in s.get('applied_filters') or []
                  if f.get('scope') == 'all_using_datasource']
        self.assertTrue(tagged)
        # only sheets of the shared view's data source get it
        for n, s in sheets(ir).items():
            for f in s.get('applied_filters') or []:
                if f.get('scope') == 'all_using_datasource':
                    self.assertEqual(f['from_datasource'], s.get('datasource_id'), n)

    def test_bdo_data_source_filters_are_context_filters(self):
        ir = self.ir('BDO Pre-Optimization Dashboard.twbx')
        got = {(f['caption'], f['kind'], f['context']) for s in sheets(ir).values()
               for f in s.get('applied_filters') or [] if f.get('scope') == 'datasource'}
        self.assertIn(('Channel', 'in', True), {(c, k, x) for c, k, x in got})
        my = [f for s in sheets(ir).values() for f in s.get('applied_filters') or []
              if f.get('scope') == 'datasource' and f['caption'] != 'Channel']
        self.assertTrue(my, got)                                            # the Month/Year(rcrd_dt) filter too
        self.assertEqual({(f['kind'], f.get('datepart')) for f in my}, {('not_in', 'my')})

    def test_a_sheet_filtering_the_same_field_keeps_its_own(self):
        import tableau_parser as TP
        import xml.etree.ElementTree as ET
        own = [{'caption': 'Region', 'kind': 'in', 'values': ['East'], 'context': False}]
        wide = [{'caption': 'Region', 'kind': 'in', 'values': ['West'], 'context': False,
                 'scope': 'all_using_datasource', 'from_datasource': 'D'},
                {'caption': 'Segment', 'kind': 'in', 'values': ['Corporate'], 'context': False,
                 'scope': 'all_using_datasource', 'from_datasource': 'D'}]
        spec = {'datasource_id': 'D', 'applied_filters': list(own)}
        TP.add_workbook_filters(spec, {'D': wide})
        self.assertEqual([(f['caption'], f['values']) for f in spec['applied_filters']],
                         [('Region', ['East']), ('Segment', ['Corporate'])])
        other = {'datasource_id': 'E'}
        TP.add_workbook_filters(other, {'D': wide})
        self.assertNotIn('applied_filters', other)


class DatePartPredicates(unittest.TestCase):
    """Found with DSF: Tableau's Month/Year date part ('my', members yyyymm such as 202502) was unknown, and an
    exclude filter on ANY date part compared the raw date with part numbers (date NOT IN ('2022'))."""
    def pred(self, **f):
        import engine
        return engine._value_predicate(dict(caption='D', **f), expr='D')

    def test_exclude_on_a_date_part(self):
        self.assertEqual(self.pred(kind='not_in', datepart='yr', values=['2022']),
                         'EXTRACT(YEAR FROM D) NOT IN (2022)')
        self.assertEqual(self.pred(kind='not_in', datepart='my', values=['%null%', '202502', '202503']),
                         'D IS NOT NULL AND (EXTRACT(YEAR FROM D) * 100 + EXTRACT(MONTH FROM D)) NOT IN (202502, 202503)')

    def test_month_year_members(self):
        self.assertEqual(self.pred(kind='in', datepart='my', values=['202502', '2025-03-01']),
                         '(EXTRACT(YEAR FROM D) * 100 + EXTRACT(MONTH FROM D)) IN (202502, 202503)')

    def test_parser_reads_month_year(self):
        import tableau_parser as TP
        f = TP.parse_field('[d].[my:rcrd_dt:ok]', {})
        self.assertEqual((f['caption'], f['datepart']), ('rcrd_dt', 'my'))

class NeverGoverned(unittest.TestCase):
    """A dashboard control replaces a sheet's saved filter on the same field (one filter in Tableau), but a data
    source filter always applies first -- the control cannot lift it."""
    def test_datasource_filter_survives_a_dashboard_control(self):
        import engine
        sheet = {'name': 'S', 'applied_filters': [
            {'caption': 'Region', 'kind': 'in', 'values': ['East'], 'context': False},
            {'caption': 'Channel', 'kind': 'not_in', 'values': ['all'], 'context': True, 'scope': 'datasource'}]}
        old = (engine.table_columns, engine.px)
        engine.table_columns = lambda T: {'REGION', 'CHANNEL'}
        engine.px = lambda c: c.upper()
        try:
            where = engine._apply_sheet_filters(sheet, '', 'T', governed={'REGION', 'CHANNEL'})
        finally:
            engine.table_columns, engine.px = old
        self.assertIn("CHANNEL NOT IN ('all')", where)
        self.assertNotIn("'East'", where)

class VisualAdapterFilters(unittest.TestCase):
    """The scatter / dual / crosstab adapter compares member values only: a range filter became FALSE (an empty
    chart). It refuses with the reason instead."""
    def test_range_filter_refuses(self):
        import types
        import visual_plan_common as V
        spec = {'worksheet_semantics': {'filter_fields': {'f1': {'physical': 'SALES', 'datatype': 'real'}}},
                'applied_filters': [{'caption': 'Sales', 'kind': 'range', 'min': 1, 'max': 9, 'field_id': 'f1'}]}
        eng = types.SimpleNamespace(to_phys=lambda p: p, CALCS={})
        with self.assertRaises(ValueError) as cm:
            V.apply_filters(eng, spec, '')
        self.assertIn('range', str(cm.exception))

if __name__ == '__main__':
    unittest.main()

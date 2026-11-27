"""DM5 / EN41: data blending as Tableau does it. Written BEFORE the fix.

Tableau's capture of Globalsales 'Details Standard Class' (primary SAMPLE_SUPER_STORE_ORDERS, secondaries SALES and
CALLS) corrects the earlier assumption that such a sheet cannot blend:
  * SALES has a field whose name is the primary's 'REGION' (caption 'REGION') -> Tableau links them automatically;
    SUM(SALES) is the secondary aggregated per Region (838,759 for Central, ...).
  * CALLS' field is 'Region' (no caption) -- a different name, so no automatic link; CALLS has no relationship to
    the primary at all -> SUM(CALLS) is the whole secondary (990,288) on every mark.
Rules: automatic links = fields with the same display name (caption, else name; exact) in the primary and the
secondary, unless the workbook defines custom links for that pair (<datasource-relationship>), which replace them.
A link is active when its primary field is a dimension of the view. A secondary with links but none active
refuses (no capture shows Tableau's result); a secondary with no link at all is aggregated over all its rows.
More than one secondary on a sheet is computed: each is aggregated and LEFT JOINed on its own active links."""
from pathlib import Path
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
GS = ROOT / 'Workbooks' / 'Globalsalesdashboard.twbx'


def gs_sheet():
    import plan_census as PC
    r = PC.intake(GS)
    ir = r['ir']
    s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Details Standard Class')
    return r, ir, s


class Links(unittest.TestCase):
    IR = {'blends': [], 'blend_fields': {
        'p': {'REGION': 'REGION', 'Product Name': 'PRODUCT_NAME'},
        's1': {'REGION': 'Region', 'Product': 'Product'},
        's2': {'Region': 'Region', 'Product': 'Product'}},
        'field_scopes': {'s1': {'caption': 'One'}, 's2': {'caption': 'Two'}}}

    def test_automatic_links_need_the_same_name(self):
        import v3_sheet_plan as P
        b = P._blend({'datasource_id': 'p'}, self.IR,
                     [{'caption': 'A', 'datasource_id': 's1'}, {'caption': 'B', 'datasource_id': 's2'}])
        by = {s['secondary']: s for s in b['secondaries']}
        self.assertEqual(by['One']['links'], [{'primary': 'REGION', 'secondary': 'REGION'}])
        self.assertEqual(by['Two']['links'], [])                    # 'Region' is not 'REGION'

    def test_custom_links_replace_automatic_ones(self):
        import v3_sheet_plan as P
        ir = dict(self.IR, blends=[{'primary_name': 'p', 'secondary_name': 's1', 'secondary': 'One',
                                    'links': [{'primary_field': 'Product Name', 'secondary_field': 'Product'}]}])
        b = P._blend({'datasource_id': 'p'}, ir, [{'caption': 'A', 'datasource_id': 's1'}])
        self.assertEqual(b['secondaries'][0]['links'], [{'primary': 'Product Name', 'secondary': 'Product'}])
        self.assertEqual(b['secondary_id'], 's1')                   # one secondary: the old keys stay

    def test_blend_fields_from_the_workbook(self):
        import tableau_parser as TP
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(GS) as z:
            root = ET.fromstring(z.read(next(n for n in z.namelist() if n.endswith('.twb'))))
        f = TP.blend_fields(root)
        self.assertEqual(f['federated.1y60yj003qpac01ahh39d193zc2k'].get('REGION'), 'Region')     # caption
        self.assertEqual(f['federated.1m55rp41nd390z10nolur1o8crmp'].get('Region'), 'Region')     # metadata only
        self.assertNotIn('REGION', f['federated.1m55rp41nd390z10nolur1o8crmp'])
        self.assertEqual(f['federated.0u108uq03n2oke1eelv4a121az4v'].get('REGION'), 'REGION')


class Globalsales(unittest.TestCase):
    def test_details_standard_class_equals_tableau(self):
        """Values checked against the workbook's own source files (independent of the engine) and Tableau's capture:
        SUM(SALES) per Region from the SALES secondary, SUM(CALLS) = all of CALLS on every mark, 49 primary marks."""
        import pandas as pd
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        with zipfile.ZipFile(GS) as z:
            calls = pd.read_excel(z.open(next(n for n in z.namelist() if n.endswith('Calls Data.xlsx'))))
            sales = pd.read_excel(z.open(next(n for n in z.namelist() if n.endswith('Sales Data.xlsx'))))
        r, ir, s = gs_sheet()
        plan = P.from_sheet(s, ir)
        self.assertNotIn('unlinked', plan)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(plan, s, engine)
        self.assertEqual(len(df), 49)
        cols = {c.split('|')[-1].strip(): c for c in df.columns}
        region = next(c for c in df.columns if c.split('|')[-1] == 'REGION')
        calls_col = next(c for c in df.columns if c.startswith('M|') and 'CALLS' in c.upper())
        sales_col = next(c for c in df.columns if c.startswith('M|') and c.split('|')[-1].strip() == 'SALES')
        self.assertTrue((df[calls_col].astype(float) == float(calls['Calls'].sum())).all(), cols)
        want = sales.groupby('Region')['Sales'].sum()
        for reg, v in zip(df[region], df[sales_col]):
            self.assertAlmostEqual(float(v), float(want[reg]), 2)
        self.assertEqual(round(float(want['Central'])), 838759)         # Tableau's capture
        self.assertEqual(int(calls['Calls'].sum()), 990288)             # Tableau's capture

    def test_secondary_with_inactive_links_refuses(self):
        import v3_sheet_plan as P
        plan = {'fields': [{'caption': 'Segment', 'kind': 'dim', 'channel': 'rows', 'date_part': 'none',
                            'discrete': True}],
                'blend': {'secondaries': [{'secondary': 'S', 'secondary_id': 's', 'links': [
                    {'primary': 'REGION', 'secondary': 'REGION'}]}]}}
        with self.assertRaises(P.PlanError) as cm:
            P.compile_blend(plan, {'name': 'x'}, None)
        self.assertIn('no active link', str(cm.exception))


class Refusals(unittest.TestCase):
    def test_untranslated_calculation_dimension_refuses(self):
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        r, ir, s = gs_sheet()
        # C1 now translates the workbook's nested LOD; keep proving the refusal with a calculation marked untranslated
        fid = next(k for k, e in ir['fields'].items() if e.get('caption') == 'AVG_SALES_REGION')
        ir['fields'][fid] = dict(ir['fields'][fid], calculation=None)
        pl = P.from_sheet(s, ir)
        pl['fields'] = [f for f in pl['fields'] if f.get('datasource_id') == s['datasource_id']]
        pl.pop('blend', None)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            with self.assertRaises(P.PlanError) as cm:
                P.execute(pl, s, engine)
        self.assertIn('not translated', str(cm.exception))



class SetHeader(unittest.TestCase):
    def test_a_set_in_in_out_mode_is_headed_like_tableau(self):
        import v3_sheet_plan as P
        import v3_exact_loop as X
        ws = {'datasource_xml': "<datasource><group caption='STATE_SET' name='[State Set]' /></datasource>",
              'occurrences': [{'id': 'a', 'channel': 'rows', 'caption': 'State Set', 'role': 'dimension',
                               'discrete': True, 'token': '[d].[io:State Set:nk]'}]}
        plan = P.from_sheet({'name': 'S', 'worksheet_semantics': ws})
        f = next(f for f in plan['fields'] if f['caption'] == 'State Set')
        self.assertEqual(X._dim_header(f), 'IN/OUT(STATE_SET)')


if __name__ == '__main__':
    unittest.main()

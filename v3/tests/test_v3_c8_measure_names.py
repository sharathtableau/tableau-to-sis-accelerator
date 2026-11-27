"""C8: Measure Values members whose names contain '%'. Written BEFORE the fix (tests first).

Tableau escapes '%' as '\\%' (and '\\' as '\\\\') inside Measure Names member strings; the plan kept the escaped
text, so the measure never matched its calculation ('\\% Open Rate  (copy)_...' could not be translated): GOLD CRM
Cohort Analysis / Movement, Email Campaign Analytics, BDO (11 Nov corpus). Rule: unescape member names exactly as the
parser's member cleaner does, for the Measure Names filter and its manual order."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


class Unescape(unittest.TestCase):
    def test_measure_values_members(self):
        import v3_sheet_plan as P
        xml = ('<filter class="categorical" column="[:Measure Names]"><groupfilter function="union">'
               '<groupfilter function="member" level="[:Measure Names]" '
               'member="&quot;[federated.x].[usr:\\% Open Rate  (copy)_955:qk]&quot;" />'
               '<groupfilter function="member" level="[:Measure Names]" '
               'member="&quot;[federated.x].[sum:Sales:qk]&quot;" /></groupfilter></filter>')
        got = P._measure_values({'worksheet_semantics': {'source_xml': xml}})
        self.assertEqual([m['caption'] for m in got], ['% Open Rate  (copy)_955', 'Sales'])


def run(workbook, sheet):
    import glob
    import engine
    import plan_census as PC
    import v3_sheet_plan as P
    from v3_local import preview_context
    paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / f'{workbook}*.twbx'), recursive=True))
    if not paths:
        raise unittest.SkipTest('corpus workbook')
    r = PC.intake(Path(paths[0]))
    ir = r['ir']
    s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet)
    with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
        return P.execute(P.from_sheet(s, ir), s, engine)


class Corpus(unittest.TestCase):
    def test_gold_crm_open_rate(self):
        df = run('GOLD CRM Scorecard Cohort Analysis', 'Eff')
        self.assertGreater(len(df), 0)

    def test_campaign_incrementality_kpis(self):
        """FIXED bodies that reference aggregate calculations, with parameters inside (Claims_Swap): KPI A runs and
        KPI C keeps its release32 numbers."""
        self.assertEqual(len(run('Campaign Incrementatlity vs Holdout Group', 'KPI A')), 1)
        c = run('Campaign Incrementatlity vs Holdout Group', 'KPI C')
        self.assertEqual(round(float(c.select_dtypes('number').sum().iloc[0])), 142528)

    def test_world_indicators_escaped_member_is_not_a_fifth_measure(self):
        import glob
        import plan_census as PC
        import v3_sheet_plan as P
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3*.twbx')))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        s = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == 'Health Indicators')
        mv = [m['caption'] for m in P.from_sheet(s, r['ir'])['measure_values']]
        self.assertEqual(mv, ['Birth Rate', 'Infant Mortality Rate', 'Health Exp % GDP', 'Life Expectancy'])

    def test_second_workbook_email(self):
        df = run('Email Campaign Analytics', 'Campaign Metrics Overview')
        self.assertGreater(len(df), 0)


if __name__ == '__main__':
    unittest.main()

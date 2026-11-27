"""EN26 / V8: nested table calculations keep their own addressing. Written with the code; the GOLD CRM Sankey
ribbons were flat (one value per ribbon) because every window inside a calculation used the OUTER pill's
addressing. Tableau stores one <table-calc field='[Inner]' ...> per nested calculation on the pill.
Rule (generic):
  * the translator's second, tagged pass marks each nested calculation's windows with its internal name
    (calc 'sql_tagged'; 'sql' and every other translation are unchanged);
  * the plan reads the pill's nested <table-calc> list and fills each tagged window with that calculation's
    addressing (a nested calculation the pill does not address follows the outer one);
  * two generic fixes on the way: an addressing field written without its datasource prefix
    ('[none:Path Frame (bin):ok]') and url-encoded field ids ('Path%20Frame%20%28bin%29') now match their
    dimension (before, Specific Dimensions on such fields silently addressed nothing).
Proof: GOLD CRM Cohort Movement 'Sankey' (the only corpus sheet with nested windows; its values follow Tableau's
documented rules -- a Tableau capture of the sheet is still needed to call them equal) and the synthetic fill
below. Regression: every captured sheet of Superstore 2024.3 and R1 V2 stays Verified against Tableau
(33 sheets; test_v3_cf05_first_translator / test_v3_cx3_calc and the exact-loop proof)."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import calc_translator as CT  # noqa: E402
import v3_sheet_plan as P  # noqa: E402


class Translator(unittest.TestCase):
    DEFS = {'Inner': 'RUNNING_SUM(SUM([Sales]))', 'Outer': 'WINDOW_SUM([Inner])', 'Plain': 'SUM([Sales])'}

    def test_tags_only_nested_windows(self):
        plain, _ = CT.translate_formula(self.DEFS['Outer'], {}, self.DEFS)
        tagged, _ = CT.translate_tagged(self.DEFS['Outer'], {}, self.DEFS)
        self.assertNotIn('@', plain)
        self.assertIn('__TC_ORDER@Inner__', tagged)
        self.assertRegex(tagged, r'\) OVER \(\s*\)')                    # the outer's own window stays untagged
        self.assertEqual(CT.translate_formula(self.DEFS['Plain'], {}, self.DEFS),
                         CT.translate_tagged(self.DEFS['Plain'], {}, self.DEFS))
        self.assertEqual(CT.tag_name('Max Position 1 (copy)_33'), 'Max~20~Position~20~1~20~~28~copy~29~_33')

    def test_pill_list(self):
        xml = ["<table-calc ordering-type='Rows' />",
               "<table-calc field='[ds].[Inner]' ordering-field='[ds].[K]' ordering-type='Field' />",
               "<table-calc field='[ds].[Other (copy)_1]' ordering-type='Field'><order field='[none:A:nk]'/>"
               "</table-calc>"]
        got = P.nested_table_calcs(xml)
        self.assertEqual(sorted(got), ['Inner', 'Other~20~~28~copy~29~_1'])
        self.assertEqual(got['Inner']['ordering_field'], '[ds].[K]')
        self.assertEqual(P._field_ordering('[none:Path Frame (bin):ok]'), 'Path Frame (bin)')
        self.assertEqual(P._field_ordering('[ds].[none:X:nk]'), 'X')


class Fill(unittest.TestCase):
    def test_each_tag_gets_its_own_partition(self):
        class E:
            CALCS = {}

            @staticmethod
            def rdim(c):
                return c.upper()
        plan = {'fields': [{'id': 'a', 'caption': 'A', 'channel': 'color', 'kind': 'dim', 'discrete': True,
                            'date_part': 'none', 'field_id': 'd::A'},
                           {'id': 'k', 'caption': 'K', 'channel': 'path', 'kind': 'dim', 'discrete': True,
                            'date_part': 'none', 'field_id': 'd::K%20(bin)'}], 'sort': []}
        f = {'caption': 'Outer', 'window': {'addressing': 'table', 'type': 'Window'},
             'nested_windows': {'Inner': {'addressing': 'field', 'ordering_field': '[none:K (bin):ok]',
                                          'ordering_fields': [], 'type': 'Window'}}}
        expr = 'SUM((SUM(SUM(S)) OVER (__TC_ORDER@Inner__ ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW))) OVER ()'
        out = P.window_sql(expr, plan, f, E, ['A', 'K'])
        self.assertIn('OVER (PARTITION BY A ORDER BY K ROWS BETWEEN', out)       # the inner: along K, per A
        self.assertTrue(out.endswith('OVER ()'))                                  # the outer: the whole table
        self.assertNotIn('@', out)


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


GOLD = wb('GOLD CRM Scorecard Cohort Movement.twbx')


@unittest.skipUnless(GOLD, 'corpus workbook')
class Sankey(unittest.TestCase):
    def test_ribbons_follow_the_sigmoid(self):
        import engine
        import plan_census as PC
        from v3_local import preview_context
        r = PC.intake(GOLD)
        s = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == 'Sankey')
        plan = P.from_sheet(s, r['ir'])
        f = next(f for f in plan['fields'] if f['caption'] == 'Sankey Polygons')
        self.assertEqual(len(f['nested_windows']), 14)
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(plan, s, engine)
        g = df.groupby(['D|none|Month Start Cohort', 'D|none|Month End Cohort'])['M|usr|Sankey Polygons'].nunique()
        self.assertEqual(len(df), 17 * 98)                                     # 17 ribbons x 98 path frames
        self.assertGreaterEqual((g == 98).sum(), 15)                           # was 1-2 values: flat ribbons
        # the sigmoid runs between each ribbon's two ends: monotone along the first half of the path
        one = df[(df['D|none|Month Start Cohort'] == 'Member') & (df['D|none|Month End Cohort'] == 'Active')]
        half = one.sort_values('D|none|Path Frame (bin)')['M|usr|Sankey Polygons'].tolist()[:49]
        self.assertTrue(all(a <= b for a, b in zip(half, half[1:])) or all(a >= b for a, b in zip(half, half[1:])))


if __name__ == '__main__':
    unittest.main()

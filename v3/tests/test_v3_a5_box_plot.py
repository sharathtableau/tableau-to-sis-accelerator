"""A5: box plots. Written with the code. No corpus workbook has a box plot and Tableau does not document how one is
stored in the workbook, so the XML hook-up waits for a sample. Until then (generic):
  * a reference object the parser does not recognise -- an unknown formula, a box / whisker attribute, a box
    element -- is never silently dropped or approximated: the plan marks it unsupported, the sheet says
    'Not shown: box plot ...' and a WARNING finding is recorded; the rest of the sheet (marks, other lines) draws;
  * the box itself is ready: v3_reflines.box_stats computes Tableau's box from its documented rules (quartiles by
    R type 7, whiskers 'Data within 1.5 times the IQR' or 'Maximum extent of the data', outliers beyond them).
Proof: the rules on fixed numbers (checked against numpy's type-7 percentiles), and an injected box element on two
corpus sheets with reference lines: Superstore 'Performance' and World Indicators 'Ease of Biz Correlation Sheet'."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import v3_reflines as R  # noqa: E402


class Stats(unittest.TestCase):
    def test_type7_quartiles_and_whiskers(self):
        import numpy as np
        xs = list(range(1, 11))
        b = R.box_stats(xs)
        self.assertEqual((b['q1'], b['median'], b['q3']), (3.25, 5.5, 7.75))
        self.assertEqual([b['q1'], b['median'], b['q3']], list(np.percentile(xs, [25, 50, 75])))
        b = R.box_stats([1, 2, 3, 4, 100, None, float('nan')])
        self.assertEqual((b['q1'], b['q3'], b['lower'], b['upper'], b['outliers']), (2.0, 4.0, 1.0, 4.0, [100.0]))
        e = R.box_stats([1, 2, 3, 4, 100], whiskers='extent')
        self.assertEqual((e['lower'], e['upper'], e['outliers']), (1.0, 100.0, []))
        self.assertIsNone(R.box_stats([None]))


class Parse(unittest.TestCase):
    def test_unknown_objects_are_reported(self):
        xml = ("<worksheet><reference-line axis-column='[d].[sum:S:qk]' formula='average' id='r0' scope='per-cell'/>"
               "<reference-line axis-column='[d].[sum:S:qk]' formula='average' id='r1' scope='per-cell' "
               "box-whisker-type='iqr'/><reference-line formula='trimmed-mean' id='r2'/><boxplot-spec id='b'/>"
               "</worksheet>")
        specs = R.parse(xml)
        self.assertNotIn('unsupported', specs[0])
        self.assertIn('box plot', specs[1]['unsupported'])
        self.assertIn("'trimmed-mean'", specs[2]['unsupported'])
        self.assertIn('<boxplot-spec>', specs[3]['unsupported'])
        self.assertEqual(R.validate(specs), [])


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


def render_with_box(path, name):
    import engine
    import findings
    import plan_census as PC
    import streamlit as st
    import v3_sheet_plan as P
    from v3_local import preview_context
    r = PC.intake(path)
    s = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == name)
    ws = dict(s['worksheet_semantics'])
    ws['source_xml'] = ws['source_xml'].replace('</worksheet>', "<box-plot id='bx0' whiskers='iqr' /></worksheet>")
    s = dict(s, worksheet_semantics=ws)
    caps, charts = [], []
    real_c, real_a = st.caption, st.altair_chart
    st.caption = lambda body, **k: caps.append(body)
    st.altair_chart = lambda chart, **k: charts.append(chart)
    try:
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            plan = P.from_sheet(s, r['ir'])
            self_check = P.validate(plan)
            P.render(dict(s, sheet_plan=plan), [], engine)
            recorded = [f for f in findings.FINDINGS if f.get('code') == 'analytics-unsupported'] \
                if hasattr(findings, 'FINDINGS') else None
    finally:
        st.caption, st.altair_chart = real_c, real_a
    return plan, caps, charts, self_check, recorded


class Corpus(unittest.TestCase):
    def check(self, path, name):
        if path is None:
            self.skipTest('corpus workbook')
        plan, caps, charts, problems, _rec = render_with_box(path, name)
        drawn = [x for x in plan['reflines'] if not x.get('unsupported')]
        self.assertTrue(drawn)                                         # the workbook's own lines are kept
        self.assertEqual([x['formula'] for x in plan['reflines'] if x.get('unsupported')], ['box'])
        self.assertTrue(any(c.startswith('Not shown: box plot') for c in caps), caps)
        self.assertTrue(charts)                                        # the sheet still draws
        self.assertFalse([p for p in problems if 'reflines' in str(p)])

    def test_superstore_performance(self):
        self.check(wb('Superstore_Tableau2024_3.twbx'), 'Performance')

    def test_world_indicators_ease_of_business(self):
        self.check(wb('World_Indicators_Tableau2024_3.twbx'), 'Ease of Biz Correlation Sheet')


if __name__ == '__main__':
    unittest.main()

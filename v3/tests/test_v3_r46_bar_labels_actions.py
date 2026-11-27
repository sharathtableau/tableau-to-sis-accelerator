"""R46: Regional Analysis View1 (live report, 26 Nov): three large dark circles over a chart ("Click to view
actions") and its bar labels unreadable.
Cause 1: the fit rules sized every SVG in a chart zone ('.vega-embed svg'), which includes the icon of the chart's
own '...' actions menu -- it grew to the zone's size and covered the bars and their labels. Rule: sizing rules
target the chart's SVG only (svg.marks), and the actions menu is hidden on the canvas (Tableau has none; Streamlit's
element toolbar is already hidden).
Cause 2: a stacked bar's label was placed at the cumulative middle of its stack, cumulated over the axis dimension
only -- across every column pane (Category / Region: the Technology labels landed far above their bars). And a
colour that never splits a bar (Region on Color and on Columns) drew labels as if stacked. Rule: a bar is its axis
dimension plus its row / column headers; a bar of one segment is labelled at its end (vertical: centred just above).
Proof: Regional Analysis (one segment per bar; and the stacked Segment chart keeps centred labels) and GOLD CRM
Scorecard Cohort Movement 'outgoing only' (stacked, in header panes) and Superstore 'QuotaAttainment' (one
colour per bar) as second workbooks."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))
RA = ROOT / 'Workbooks' / 'Regional Analysis.twbx'
CRM = ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'GOLD CRM Scorecard Cohort Movement.twbx'


def text_layers(spec, out=None):
    out = [] if out is None else out
    if isinstance(spec, dict):
        m = spec.get('mark')
        if (m == 'text' or (isinstance(m, dict) and m.get('type') == 'text')):
            out.append(spec)
        for v in spec.values():
            text_layers(v, out)
    elif isinstance(spec, list):
        for v in spec:
            text_layers(v, out)
    return out


def rows_of(spec, layer):
    data = layer.get('data') or spec.get('data') or {}
    if 'values' in data:
        return data['values']
    name = data.get('name')
    return (spec.get('datasets') or {}).get(name) or []


class Css(unittest.TestCase):
    def test_fit_rules_size_the_chart_svg_only(self):
        import engine
        for mode in ('entire-view', 'fit-height', 'fit-width'):
            css = engine._fit_css('k', mode, 400, 300)
            self.assertNotRegex(css, r'\.vega-embed svg(?!\.marks)')
            self.assertIn('svg.marks', css)

    def test_dashboard_page_keeps_narrow_side_padding(self):
        import inspect
        import engine
        self.assertIn(':has(div.st-key-v3canvas){padding-left:1rem', inspect.getsource(engine).replace('"\n        "', ''))

    def test_actions_menu_hidden_on_the_canvas(self):
        import inspect
        import engine
        self.assertIn('div.st-key-v3canvas .vega-embed details{display:none', inspect.getsource(engine))


def spec_for(path, name):
    from test_v3_en25_a10_marks import chart_for
    plan, chart = chart_for(path, name)
    return plan, chart.to_dict()


@unittest.skipUnless(RA.exists(), 'corpus workbook')
class RegionalAnalysis(unittest.TestCase):
    def test_one_segment_bars_are_labelled_at_their_end(self):
        _plan, spec = spec_for(RA, 'Category wise Sales by Region')
        texts = text_layers(spec)
        self.assertTrue(texts)
        for t in texts:
            enc = t.get('encoding') or {}
            self.assertNotEqual((enc.get('y') or {}).get('field'), '__mid')
            self.assertEqual(t['mark'].get('baseline'), 'bottom')

    def test_stacked_bars_keep_centred_labels(self):
        _plan, spec = spec_for(RA, 'Segment wise Profit by Region')
        assert_labels_inside_bars(self, spec)


@unittest.skipUnless(CRM.exists(), 'corpus workbook')
class SecondWorkbook(unittest.TestCase):
    def test_superstore_quota_one_segment_bars(self):
        ss = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'
        if not ss.exists():
            self.skipTest('corpus workbook')
        _plan, spec = spec_for(ss, 'QuotaAttainment')       # Achieved Quota on Color: one colour per bar
        texts = text_layers(spec)
        self.assertTrue(texts)
        for t in texts:
            enc = t.get('encoding') or {}
            self.assertNotIn('__mid', [(enc.get(c) or {}).get('field') for c in ('x', 'y')])

    def test_stacked_labels_inside_their_own_pane_bar(self):
        _plan, spec = spec_for(CRM, 'outgoing only')
        assert_labels_inside_bars(self, spec)


def facet_fields(spec, out=None):
    out = [] if out is None else out
    if isinstance(spec, dict):
        for k in ('row', 'column', 'facet'):
            v = (spec.get('facet') or {}).get(k) if isinstance(spec.get('facet'), dict) else None
            if isinstance(v, dict) and v.get('field'):
                out.append(v['field'])
        for v in spec.values():
            facet_fields(v, out)
    elif isinstance(spec, list):
        for v in spec:
            facet_fields(v, out)
    return list(dict.fromkeys(out))


def bar_layers(spec, out=None):
    out = [] if out is None else out
    if isinstance(spec, dict):
        m = spec.get('mark')
        if m == 'bar' or (isinstance(m, dict) and m.get('type') == 'bar'):
            out.append(spec)
        for v in spec.values():
            bar_layers(v, out)
    elif isinstance(spec, list):
        for v in spec:
            bar_layers(v, out)
    return out


def assert_labels_inside_bars(case, spec):
    """Every centred label lies inside its own bar: 0 <= middle <= the bar's total, the bar being one value of the
    other axis in one header pane."""
    heads = facet_fields(spec)
    checked = 0
    for t in text_layers(spec):
        enc = t.get('encoding') or {}
        ch = next((c for c in ('y', 'x') if (enc.get(c) or {}).get('field') == '__mid'), None)
        if not ch:
            continue
        other = (enc.get('x' if ch == 'y' else 'y') or {}).get('field')
        meas = next(((b.get('encoding') or {}).get(ch) or {}).get('field') for b in bar_layers(spec))
        totals = {}
        rows = rows_of(spec, t)
        for r in rows:
            k = tuple(r.get(f) for f in [other] + heads)
            totals[k] = totals.get(k, 0.0) + max(0.0, float(r.get(meas) or 0))
        for r in rows:
            if r.get('__mid') is None:
                continue
            k = tuple(r.get(f) for f in [other] + heads)
            case.assertLessEqual(float(r['__mid']), totals[k] + 1e-6, k)
            checked += 1
    case.assertGreater(checked, 0)

if __name__ == '__main__':
    unittest.main()

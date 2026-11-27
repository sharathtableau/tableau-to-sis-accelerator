"""E20: contrast. Written with the code. A chart drawn on a dark background (its own shading, or a dark Tableau
container) kept the chart library's dark grey axis, legend and title text, so the labels were invisible -- corpus:
the Gross vs. Net card of the E-Commerce dashboards (3 workbooks, 12 sheet placements on black).
Rule (generic): every sheet's effective background = its own shading, else the container it is drawn in; on a dark
one the chart's GENERATED ink (axis / legend / header / title text, gridlines, generated mark labels) turns light,
while any colour the workbook or the renderer sets is kept. Authored text colours drawn on the background are
checked against WCAG (3:1 for large text and graphics) and a shortfall is a finding, not a silent change.
Corpus audit: 742 sheet placements, 12 on a dark background, 0 authored colours below 3:1.
Proof: E-Commerce VOTD 'Gross vs. Net' (engine renderer, dark container) and Superstore 'CustomerScatter' given a
dark worksheet shading (the plan renderer)."""
import glob
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import v3_sheet_plan as P  # noqa: E402


class Rules(unittest.TestCase):
    def test_ratio(self):
        self.assertEqual(P.contrast_ratio('#000000', '#ffffff'), 21.0)
        self.assertEqual(P.contrast_ratio('#ffffff', '#ffffff'), 1.0)
        self.assertAlmostEqual(P.contrast_ratio('#777777', '#ffffff'), 4.48, places=1)
        self.assertIsNone(P.contrast_ratio('red', '#ffffff'))
        self.assertTrue(P.is_dark('#002960') and P.is_dark('#000000'))
        self.assertFalse(P.is_dark('#f2f6f7') or P.is_dark(None))

    def test_effective_background(self):
        own = {'ws_style': {'table': {'background-color': '#123456'}}, '_bg': '#ffffff'}
        none = {'ws_style': {'table': {'background-color': '#12345600'}}, '_bg': '#000000'}
        self.assertEqual(P.effective_background(own), '#123456')       # the sheet's own shading wins
        self.assertEqual(P.effective_background(none), '#000000')      # 'None' shows the container
        self.assertTrue(P.is_dark(P.effective_background({'_dark': True})))
        self.assertEqual(P.effective_background({}), '#ffffff')

    def test_low_contrast_is_reported_for_background_text_only(self):
        style = {'axis': {'color': '#eeeeee'}, 'label': {'color': '#ffffff'}, 'header': {'color': '#333333'}}
        self.assertEqual(P.low_contrast(style, '#ffffff'), [('axis', '#eeeeee', 1.16)])

    def test_dark_ink_keeps_set_colours(self):
        import altair as alt
        import engine
        import pandas as pd
        c = alt.Chart(pd.DataFrame({'a': [1]})).mark_bar().encode(x='a:Q').configure_axis(labelColor='#ff0000')
        cfg = engine.dark_ink(c, bg='#000000').to_dict()['config']
        self.assertEqual(cfg['axis']['labelColor'], '#ff0000')
        self.assertEqual(cfg['axis']['titleColor'], '#e8e8e8')
        self.assertEqual(cfg['legend']['labelColor'], '#e8e8e8')
        self.assertNotIn('legend', engine.dark_ink(c, bg='#ffffff').to_dict()['config'])


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


def captured(path, sheet_name, **extra):
    import headless_render as H
    import plan_census as PC
    from v3_local import preview_context
    r = PC.intake(path)
    s = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == sheet_name)
    with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
        cap = H._capture_all(dict(s, **extra))
    return [c.to_dict() for c in cap['altair']]


EC, SS = wb('E-Commerce (Software) Sales Dashboard VOTD.twbx'), wb('Superstore_Tableau2024_3.twbx')


@unittest.skipUnless(EC, 'corpus workbook')
class DarkContainer(unittest.TestCase):
    def test_gross_vs_net_labels_are_light(self):
        specs = captured(EC, 'Gross vs. Net', _bg='#000000', _dark=True)
        self.assertTrue(specs)
        self.assertEqual(specs[0]['config']['axis']['labelColor'], '#e8e8e8')
        light = captured(EC, 'Gross vs. Net')
        self.assertNotIn('#e8e8e8', json.dumps(light))                  # a white container keeps dark ink


@unittest.skipUnless(SS, 'corpus workbook')
class DarkShading(unittest.TestCase):
    def test_plan_sheet_on_its_own_dark_shading(self):
        import tableau_parser as TP
        ir = TP.build_ir(str(SS), scoped=True)
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'CustomerScatter')
        style = dict(s.get('ws_style') or {}, table=dict((s.get('ws_style') or {}).get('table') or {},
                                                         **{'background-color': '#1b1b1b'}))
        specs = captured(SS, 'CustomerScatter', ws_style=style)
        self.assertTrue(specs)
        cfg = specs[0]['config']
        self.assertEqual((cfg['axis']['labelColor'], cfg['axis']['gridColor']), ('#e8e8e8', '#444444'))


if __name__ == '__main__':
    unittest.main()

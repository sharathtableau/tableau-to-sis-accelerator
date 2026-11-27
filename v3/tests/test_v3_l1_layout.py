"""L1: dashboards laid out as Tableau lays them out in a browser. Written with the code, from the R1 Superstore
'Product' dashboard as deployed (26 Nov): an empty gap beside the canvas, scroll bars Tableau does not show, the
heatmap narrower than its zone, the Region list cut off and the bottom chart not drawn at all. Measured in a real
browser (tools/layout_audit.py), the causes were generic:
  * a chart rebuilt from a spec kept its rows in 'datasets', which st.altair_chart replaces -> no marks; and a
    concatenated / faceted chart given the container width was drawn 0 px wide -> empty zone;
  * an Automatic / Range dashboard was drawn at a fixed 1,100 px -> gap; it now scales to the page width
    (container queries, transform; a fixed size stays exact) and every chart / table takes its zone's laid-out
    width itself (the browser reports the scaled width);
  * Entire View / Fit Height charts were sized by estimates (fonts differ per browser) and the zone scrolled ->
    the chart's SVG is now fitted to the space the title leaves; Fit Width fits the width and may scroll down;
    Standard scrolls, as in Tableau;
  * a chart's inline-block line box, multi-line titles drawn taller than reserved, list rows taller than
    Tableau's (16 px), a horizontal legend clipped by its zone, a custom control title hiding the labels of a
    checkbox list's members, and a device-layout picker Tableau does not show.
Proof: R1 Superstore (Product, Customers, Commission Model) and World Indicators (Business, Tourism) at 1,100 /
1,500 / 1,920 px: no problem reported."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import engine  # noqa: E402


class Css(unittest.TestCase):
    def test_canvas_scales_only_when_not_fixed(self):
        self.assertEqual(engine.canvas_zoom_css('div.x', 1100, {'minwidth': 900, 'maxwidth': 900}, 600), '')
        css = engine.canvas_zoom_css('div.x', 1100, {'minwidth': 1000, 'minheight': 620}, 682)
        self.assertIn('@container (min-width:1124px){div.x{transform:scale(1.02);', css)
        self.assertIn('transform:scale(1.6);', css)
        self.assertNotIn('zoom:', css)

    def test_fit_modes(self):
        e = engine._fit_css('k', 'entire-view', 960, 270)
        self.assertIn('width:960px !important;height:270px !important;', e)
        self.assertIn('flex-direction:column', e)
        self.assertIn('max-width:500px', engine._fit_css('k', 'fit-width', 500, 200))
        self.assertNotIn('flex-direction', engine._fit_css('k', 'fit-width', 500, 200))
        for fit, want in (('entire-view', 'hidden'), ('fit-height', 'hidden'), ('fit-width', 'hidden auto'),
                          ('standard', 'hidden auto'), (None, 'hidden auto')):
            self.assertEqual(engine._zone_overflow({'kind': 'sheet'}, fit), want, fit)

    def test_title_hiding_spares_option_labels(self):
        self.assertIn(':not(.stCheckbox *)', engine._WIDGET_TITLE_ONLY)
        self.assertIn(':not([role=radiogroup] *)', engine._WIDGET_TITLE_ONLY)
        src = (ROOT / 'engine.py').read_text(encoding='utf-8')
        self.assertNotIn('[data-testid=stWidgetLabel]{{display:none;}}', src)

    def test_title_lines_take_their_reserved_height(self):
        runs = [{'text': 'Sales', 'size': '15'}, {'br': True}, {'text': ' ', 'size': '9'}]
        html, _a = engine._runs_lines_html(runs, 12)
        self.assertEqual(html.count("<div style='font-size:"), 2)
        self.assertIn('min-height:24.0px', html)                      # 15 pt x 4/3 x 1.2
        self.assertIn('min-height:14.4px', html)                      # 9 pt


class Charts(unittest.TestCase):
    def frame(self):
        import pandas as pd
        return pd.DataFrame({'c': ['a', 'b'], 'v': [1.0, 2.0]})

    def test_rows_survive_streamlit(self):
        import altair as alt
        import streamlit.elements.vega_charts as VC
        base = alt.Chart(self.frame()).mark_bar().encode(x='v:Q', y='c:N')
        rebuilt = alt.VConcatChart.from_dict(alt.vconcat(base, base).to_dict())
        # some Streamlit versions drop the rows of such a chart (the deployed one did); with _keep_rows every
        # version receives them
        kept = VC._convert_altair_to_vega_lite_spec(engine._keep_rows(rebuilt))
        self.assertTrue(kept.get('datasets'))

    def test_compound_and_zone_width(self):
        import altair as alt
        c = alt.Chart(self.frame()).mark_bar().encode(x='v:Q', y='c:N')
        self.assertFalse(engine._compound(c) or engine._compound(c + c))
        self.assertTrue(engine._compound(alt.vconcat(c, c)) and engine._compound(c.encode(row='c:N')))
        z = engine._zone_width(c, 400).to_dict()
        self.assertEqual((z['width'], z['autosize']['type']), (398, 'fit-x'))
        fit = engine._zone_width(c.properties(autosize=alt.AutoSizeParams(type='fit')), 400).to_dict()
        self.assertEqual(fit['autosize']['type'], 'fit')

    def test_no_layout_picker(self):
        import inspect
        self.assertNotIn('selectbox', inspect.getsource(engine._device_choice))


def browser_python():
    """A Python whose Playwright can start Chromium (this one, or the system python3), else None."""
    import shutil
    import subprocess
    probe = 'from playwright.sync_api import sync_playwright\nwith sync_playwright() as p: p.chromium.launch().close()'
    for py in (sys.executable, shutil.which('python3')):
        if py and subprocess.run([py, '-c', probe], capture_output=True, timeout=120).returncode == 0:
            return py
    return None


BROWSER = browser_python()


@unittest.skipUnless(BROWSER, 'Playwright with Chromium')
class InTheBrowser(unittest.TestCase):
    CASES = (('R1_Test_Upload_-_Superstore.twbx', 'Product'), ('R1_Test_Upload_-_Superstore.twbx', 'Customers'),
             ('World_Indicators_Tableau2024_3.twbx', 'Tourism'), ('World_Indicators_Tableau2024_3.twbx', 'Business'))

    def test_no_layout_problems(self):
        import json
        import subprocess
        for wb, dash in self.CASES:
            path = next((ROOT / 'Workbooks').rglob(wb), None)
            if path is None:
                continue
            out = subprocess.run([BROWSER, str(ROOT / 'tools' / 'layout_audit.py'), str(path), dash, '1500',
                                  '--python', sys.executable], capture_output=True, text=True, timeout=300)
            res = json.loads(out.stdout)
            self.assertEqual(res['problems'], [], (wb, dash))
            self.assertGreaterEqual(res['canvas'], 0.95 * res['page'])


if __name__ == '__main__':
    unittest.main()

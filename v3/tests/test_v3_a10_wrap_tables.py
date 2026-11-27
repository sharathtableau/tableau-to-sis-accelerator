"""A10: text tables whose workbook sets Format > Cells 'Wrap' on. Written with the code. Streamlit's data grid
cannot wrap a cell, so long text was cut off. Rule (generic): a text table with cell wrap on (and at most
WRAP_TABLE_MAX_ROWS rows) is drawn as an HTML table -- the same rows, columns and formatted values the grid shows,
escaped, cells wrapping on word boundaries, scrolling inside the zone height; the snapshot captures it as a table.
No corpus text table sets wrap on (BDO 'Data Extract' sets it on a bar chart: its headers wrap, see EN25 / A10
marks), so the proof sets the workbook formatting on two corpus text tables: Superstore 'Product Detail Sheet'
and World Indicators 'Tourism By Country/Region'. Above the cap the grid is kept and a finding says so."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import engine  # noqa: E402


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


def draw(path, name, wrap):
    import plan_census as PC
    from v3_local import preview_context
    r = PC.intake(path)
    s = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == name)
    if wrap:
        style = dict(s.get('ws_style') or {})
        style['cell'] = dict(style.get('cell') or {}, wrap='on')
        s = dict(s, ws_style=style, sheet_plan=dict(s['sheet_plan'], ws_style_wrap='on'))
    seen = {'wrapped': [], 'grid': []}
    real_w, real_g = engine._wrapped_table, engine._safe_dataframe
    engine._wrapped_table = lambda df, **k: seen['wrapped'].append(df)
    engine._safe_dataframe = lambda df, **k: seen['grid'].append(df)
    try:
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            engine.render_sheet(s, [])
    finally:
        engine._wrapped_table, engine._safe_dataframe = real_w, real_g
    return seen


class Html(unittest.TestCase):
    def test_escaped_and_wrapping(self):
        import pandas as pd
        import streamlit as st
        out = []
        real = st.markdown
        st.markdown = lambda body, **k: out.append(body)
        try:
            engine._wrapped_table(pd.DataFrame({'Name': ['<b>x</b> & y', None], 'N': ['1', '2']}), height=200,
                                  style={'cell': {'color': '#790009', 'font-size': '9'}})
        finally:
            st.markdown = real
        html = out[0]
        self.assertIn('&lt;b&gt;x&lt;/b&gt; &amp; y', html)
        self.assertNotIn('<b>x</b>', html)
        self.assertIn('white-space:normal', html)
        self.assertIn('max-height:200px', html)
        self.assertIn('color:#790009', html)


class Corpus(unittest.TestCase):
    def check(self, path, name):
        if path is None:
            self.skipTest('corpus workbook')
        plain = draw(path, name, wrap=False)
        wrapped = draw(path, name, wrap=True)
        self.assertEqual((len(plain['grid']), len(plain['wrapped'])), (1, 0))
        self.assertEqual((len(wrapped['grid']), len(wrapped['wrapped'])), (0, 1))
        a, b = plain['grid'][0], wrapped['wrapped'][0]
        self.assertEqual(list(a.columns), list(b.columns))
        self.assertTrue(a.astype(str).reset_index(drop=True).equals(b.astype(str).reset_index(drop=True)))

    def test_superstore_product_detail_over_the_cap(self):
        path = wb('Superstore_Tableau2024_3.twbx')
        if path is None:
            self.skipTest('corpus workbook')
        wrapped = draw(path, 'Product Detail Sheet', wrap=True)          # 5,111 rows: the grid, with a finding
        self.assertEqual((len(wrapped['grid']), len(wrapped['wrapped'])), (1, 0))
        self.assertGreater(len(wrapped['grid'][0]), engine.WRAP_TABLE_MAX_ROWS)
        old = engine.WRAP_TABLE_MAX_ROWS
        engine.WRAP_TABLE_MAX_ROWS = 10_000
        try:
            self.check(path, 'Product Detail Sheet')
        finally:
            engine.WRAP_TABLE_MAX_ROWS = old

    def test_world_indicators_tourism(self):
        self.check(wb('World_Indicators_Tableau2024_3.twbx'), 'Tourism By Country/Region')


if __name__ == '__main__':
    unittest.main()

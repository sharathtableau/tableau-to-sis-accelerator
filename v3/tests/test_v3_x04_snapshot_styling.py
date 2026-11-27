"""X04: the app-side snapshot (headless_render, read by people and by the Cortex image comparison) carries the
dashboard's colours. Written with the code; the snapshot used to draw every zone white with dark text, so dark
cards and shaded KPI sheets looked unlike both Tableau and the live app, and text-only sheets were missing.
Rule (generic): a layout container's own colour ('bg') fills its zones; a sheet's worksheet shading (Format >
Shading, axis_xml.table_background) wins over it, 'None' lets the container show through; KPI tiles and text are
drawn in a colour readable on that background; Altair charts get it as their background; a sheet that draws only
HTML text (titles, labels, notes) is drawn as that text. Proof: E-Commerce (Software) Sales Dashboard VOTD (black
Gross / Net Revenue card) and Retails Dashboard (KPI sheets shaded #f2f6f7)."""
import glob
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import headless_render as H  # noqa: E402


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


class Rules(unittest.TestCase):
    def test_ink_and_background(self):
        self.assertEqual(H.ink_for('#000000')[0], '#f5f5f5')
        self.assertEqual(H.ink_for('#f2f6f7')[0], '#1a1a1a')
        self.assertEqual(H.ink_for(None)[0], '#1a1a1a')
        shaded = {'ws_style': {'table': {'background-color': '#002960'}}}
        clear = {'ws_style': {'table': {'background-color': '#ffffff00'}}}
        self.assertEqual(H.sheet_background(shaded, '#000000'), '#002960')
        self.assertEqual(H.sheet_background(clear, '#000000'), '#000000')
        self.assertEqual(H.sheet_background({}, 'white'), 'white')

    def test_html_text(self):
        html = "<style>.x{a:b}</style><div><span style='c'>Total&nbsp;Revenue</span></div><div>$1,201</div>"
        self.assertEqual([t.replace('\xa0', ' ') for t in H._html_text(html)], ['Total Revenue', '$1,201'])
        png, why = H._text_to_png(['Hello'], width=200, bg='#000000')
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert('RGB')
        self.assertEqual(im.getpixel((1, 1)), (0, 0, 0))


def most_common(png):
    from collections import Counter
    from PIL import Image
    return Counter(Image.open(io.BytesIO(png)).convert('RGB').getdata()).most_common(1)[0][0]


EC, RT = wb('E-Commerce (Software) Sales Dashboard VOTD.twbx'), wb('Retails Dashboard.twbx')


@unittest.skipUnless(EC, 'corpus workbook')
class DarkCard(unittest.TestCase):
    def test_black_card_and_text_sheets(self):
        import plan_census as PC
        from collections import Counter
        from PIL import Image
        from v3_local import preview_context
        r = PC.intake(EC)
        d = next(x for x in r['ir']['dashboards'] if x['name'] == 'Dashboard')
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            png, notes = H.render_dashboard_to_png(d)
        im = Image.open(io.BytesIO(png)).convert('RGB')
        black = Counter(im.getdata())[(0, 0, 0)]
        self.assertGreater(black / (im.width * im.height), 0.05)         # the black card is drawn black
        self.assertTrue(all(n['rendered'] for n in notes if n['sheet'] in ('Revenue', 'info')))


@unittest.skipUnless(RT, 'corpus workbook')
class ShadedKpis(unittest.TestCase):
    def test_kpi_sheets_keep_their_shading(self):
        import plan_census as PC
        from v3_local import preview_context
        r = PC.intake(RT)
        sheets = {s['name']: s for d in r['ir']['dashboards'] for s in d['sheets']}
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            for name in ('Total Quantity ', 'Total Customer'):
                s = sheets.get(name) or sheets[name.strip()]
                png, _why = H.render_sheet_to_png(s, width=500)
                self.assertEqual(most_common(png), (242, 246, 247), name)


if __name__ == '__main__':
    unittest.main()

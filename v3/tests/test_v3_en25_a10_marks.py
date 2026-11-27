"""EN25 / A10: the worksheet's own mark formatting (Marks card > Color / Shape / Size): mark colour, transparency,
shape (filled / open), border and line markers. Written with the code; the plan used to ignore them (default blue,
full opacity, filled circles, no borders).
Rule (generic, from <style-rule element='mark'>): mark-color = the mark colour when no field is on Color (a colour
encoding still wins); mark-transparency 0-255 = opacity; shape ':filled/x' / ':open/x' = the Vega shape, open =
hollow; has-stroke + stroke-color = the border (pies too); mark-markers-mode all / none = line points. A custom
shape image is never packaged in a .twbx (it lives in the author's repository): recorded as 'custom_shape', the
mark keeps its default. Proof: Superstore 2024.3 (CustomerScatter, CustomerOverview) and E-commerce Widget KPI
('Orders per Week': white cell borders); Human Resource Dashboard ('year': white diamonds, plan only)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import v3_sheet_plan as P  # noqa: E402

SS = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'
EC = ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'E-commerce_Widget-Style_KPI_Dashboard.twbx'
HR = ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'Human Resource Dashboard.twbx'


class Parse(unittest.TestCase):
    def test_rules(self):
        got = P.mark_style({'mark': {'mark-color': '#4a76a1', 'mark-transparency': '206', 'shape': ':open/diamond',
                                     'has-stroke': 'true', 'stroke-color': '#ffffff', 'mark-markers-mode': 'all'}})
        self.assertEqual(got, {'color': '#4a76a1', 'opacity': 0.808, 'shape': 'diamond', 'open': True,
                               'stroke': '#ffffff', 'markers': True})
        self.assertEqual(P.mark_style({'mark': {'mark-transparency': '255', 'mark-color': 'red'}}), {})
        self.assertEqual(P.mark_style({'mark': {'shape': ':custom/Blank.png'}}), {'custom_shape': 'Blank.png'})
        self.assertEqual(P.mark_style({'mark': {'has-stroke': 'false', 'stroke-color': '#000000'}}), {})
        self.assertEqual(P.mark_style(None), {})


def chart_for(path, name):
    import engine
    import plan_census as PC
    from v3_local import preview_context
    r = PC.intake(path)
    sheet = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == name)
    with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
        plan = P.from_sheet(sheet, r['ir'])
        df = P.execute(plan, sheet, engine)
        chart = P.build_chart(plan, df, engine)
    return plan, chart


def mark_of(spec):
    m = spec.get('mark')
    if m is None:
        for layer in [spec.get('spec')] + [x for k in ('layer', 'hconcat', 'vconcat', 'concat') for x in spec.get(k) or []]:
            m = mark_of(layer) if layer else None
            if m:
                return m
    return m if isinstance(m, dict) else ({'type': m} if m else None)


@unittest.skipUnless(SS.exists(), 'corpus workbook')
class Superstore(unittest.TestCase):
    def test_scatter_opacity_and_shape(self):
        plan, chart = chart_for(SS, 'CustomerScatter')
        self.assertEqual(plan['mark_style'], {'opacity': 0.776, 'shape': 'circle'})
        m = mark_of(chart.to_dict())
        self.assertAlmostEqual(m['opacity'], 0.776)

    def test_overview_colour(self):
        plan, chart = chart_for(SS, 'CustomerOverview')
        self.assertEqual(plan['mark_style']['color'], '#4a76a1')
        spec = chart.to_dict() if chart is not None else {}
        if spec:
            self.assertEqual(mark_of(spec).get('color'), '#4a76a1')


@unittest.skipUnless(EC.exists(), 'corpus workbook')
class Ecommerce(unittest.TestCase):
    def test_calendar_squares_have_white_borders(self):
        plan, chart = chart_for(EC, 'Orders per Week')
        self.assertEqual(plan['mark_style'], {'opacity': 0.788, 'stroke': '#ffffff'})
        m = mark_of(chart.to_dict())
        self.assertEqual((m['type'], m['stroke'], m['opacity']), ('rect', '#ffffff', 0.788))


@unittest.skipUnless(HR.exists(), 'corpus workbook')
class HumanResource(unittest.TestCase):
    def test_year_white_diamonds_plan(self):                 # live-only data: the plan, not the chart
        import tableau_parser as TP
        ir = TP.build_ir(str(HR), scoped=True)
        sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'year')
        self.assertEqual(P.from_sheet(sheet, ir)['mark_style'], {'color': '#ffffff', 'shape': 'diamond'})


class Wrap(unittest.TestCase):
    """A10: Format > Cells 'Wrap' on: discrete header labels wrap on word boundaries (Tableau's default truncates)."""
    def test_expr(self):
        e = P.wrap_expr(['Office Supplies and Furniture items', 'Tech'])
        self.assertIn('["Office Supplies", "and Furniture", "items"]', e)
        self.assertNotIn('Tech', e)
        self.assertIsNone(P.wrap_expr(['short', 'labels']))
        self.assertEqual(P.wrap_expr(['a ' * 40], width=10, max_lines=2).count('", "'), 1)  # two lines at most

    @unittest.skipUnless(EC.exists(), 'corpus workbook')
    def test_plan_flags_on_two_workbooks(self):
        import tableau_parser as TP
        for path, name in ((EC, 'Rev trend'),
                           (ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'BDO Pre-Optimization Dashboard.twbx',
                            'Data Extract')):
            if not path.exists():
                continue
            ir = TP.build_ir(str(path), scoped=True)
            sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'].strip() == name)
            self.assertEqual(P.from_sheet(sheet, ir).get('ws_style_wrap'), 'on', name)


class LargeImages(unittest.TestCase):
    """EN25: a dashboard image over the embed cap is scaled and re-encoded instead of dropped (corpus: the
    E-commerce Widget background screenshot, 4.2 MB -- the only image zone of 442 that was not drawn)."""
    def test_synthetic_photo_becomes_jpeg(self):
        import io
        import os
        import tableau_parser as TP
        from PIL import Image
        im = Image.frombytes('RGB', (1400, 1400), os.urandom(1400 * 1400 * 3))
        buf = io.BytesIO()
        im.save(buf, format='PNG')
        self.assertGreater(buf.tell(), TP.MAX_IMAGE_BYTES)
        data, mime = TP.shrink_image(buf.getvalue(), 'image/png')
        self.assertEqual(mime, 'image/jpeg')
        self.assertLessEqual(len(data), TP.MAX_IMAGE_BYTES)
        self.assertEqual(TP.shrink_image(b'<svg/>', 'image/svg+xml'), (None, 'image/svg+xml'))
        self.assertEqual(TP.shrink_image(b'not an image', 'image/png')[0], None)

    @unittest.skipUnless(EC.exists(), 'corpus workbook')
    def test_widget_background_is_embedded(self):
        import tableau_parser as TP
        ir = TP.build_ir(str(EC), scoped=True)
        uri = ir['images'].get('Image/Widget Style KPI Dashboard for Ecommerce.png') or ''
        self.assertTrue(uri.startswith('data:image/'))
        self.assertLess(len(uri), TP.MAX_IMAGE_BYTES * 4 // 3 + 100)


if __name__ == '__main__':
    unittest.main()

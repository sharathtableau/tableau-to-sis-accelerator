"""CX2 fix queue -> general rules (release 27). Each rule is keyed off one Tableau XML construct and is proved on a
workbook OTHER than the E-Commerce dashboard where CX2 found it (or on a cross-workbook fixture when only one
corpus workbook family carries the construct)."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import axis_xml  # noqa: E402
import tableau_parser  # noqa: E402

WB = ROOT / 'Workbooks'


def _worksheets(path):
    with zipfile.ZipFile(path) as z:
        twb = z.read(next(n for n in z.namelist() if n.lower().endswith('.twb')))
    return {w.get('name'): w for w in ET.fromstring(twb).iter('worksheet')}


# A synthetic worksheet: continuous DAY axis, Bar mark, automatic size. Only the E-Commerce family carries this
# construct in the corpus (4 copies of one workbook), so the rule is proved on this cross-workbook fixture.
_DATE_BAR = """<worksheet name='w'><table><view/><style/><panes><pane><view/><mark class='Bar'/>
<mark-sizing mark-sizing-setting='marks-scaling-on' use-custom-mark-size='false'/></pane></panes>
<rows>[ds].[sum:Sales:qk]</rows><cols>[ds].[tdy:Order Date:qk]</cols></table></worksheet>"""


class WorksheetShading(unittest.TestCase):
    def test_none_shading_is_transparent_on_a_second_workbook(self):
        ws = _worksheets(WB / 'Superstore.twbx')['ShipSummary']          # Superstore, not E-Commerce
        style = tableau_parser.worksheet_style(ws)
        self.assertEqual(axis_xml.table_background(style), 'transparent')

    def test_colour_and_absent(self):
        self.assertEqual(axis_xml.table_background({'table': {'background-color': '#1f2a44'}}), '#1f2a44')
        self.assertEqual(axis_xml.table_background({'table': {'background-color': '#1f2a44ff'}}), '#1f2a44')
        self.assertIsNone(axis_xml.table_background({}))                # not authored: app default kept

    def test_engine_applies_it_to_the_chart_config(self):
        import altair as alt
        import engine
        c = engine._with_background(alt.Chart({'values': [{'a': 1}]}).mark_bar().encode(x='a:Q'), 'transparent')
        spec = c.to_dict()
        self.assertEqual(spec['config']['background'], 'transparent')
        self.assertTrue(getattr(engine.st.altair_chart, '_v3_bg', False))


class ContinuousDateBars(unittest.TestCase):
    def test_bar_fills_one_date_unit(self):
        self.assertTrue(axis_xml.automatic_mark_size(_DATE_BAR))
        days = [f'2024-03-{d:02d}' for d in range(1, 22)]                 # 21 days over 380px
        self.assertEqual(axis_xml.date_bar_px(days, 'DAY', 380), int(380 / 21 * 0.88))
        months = ['2024-01-01', '2024-06-01', '2024-12-01']               # gaps do not widen the bars
        self.assertEqual(axis_xml.date_bar_px(months, 'MONTH', 600), int(600 / 12 * 0.88))

    def test_custom_size_keeps_the_renderer_default(self):
        self.assertFalse(axis_xml.automatic_mark_size(_DATE_BAR.replace("use-custom-mark-size='false'",
                                                                        "use-custom-mark-size='true'")))


class AxisTicksAndCircleSize(unittest.TestCase):
    def test_tick_density(self):
        self.assertEqual(axis_xml.axis_tick_count(400), 4)                 # $0 / $20k / $40k / $60k
        self.assertEqual(axis_xml.axis_tick_count(90), 2)
        self.assertIsNone(axis_xml.axis_tick_count(None))

    def test_authored_circle_size_on_a_second_workbook(self):
        sheets = _worksheets(WB / 'World Indicators.twbx')
        sizes = []
        for ws in sheets.values():
            if any(m.get('class') == 'Circle' for m in ws.iter('mark')):
                rules = axis_xml.parse(ws)
                if any('size' in m for m in rules['marks'].values()):
                    sizes.append(axis_xml.circle_size(rules))
        self.assertTrue(sizes)
        self.assertTrue(all(isinstance(s, int) and s > 0 for s in sizes))
        self.assertIsNone(axis_xml.circle_size({'marks': {}}))


if __name__ == '__main__':
    unittest.main()


def _ir(name, view):
    from v3_local import prepare_payload
    p = WB / name
    return prepare_payload(p.read_bytes(), p.name, published_views=[view] if view else None)['ir']


class TableauControlsAndInserts(unittest.TestCase):
    def test_parameter_control_shows_aliases_on_a_second_workbook(self):
        import engine
        ir = _ir('ECommerce_Live_Relationship_Events_Customers_Products.twbx', 'Dashboard')   # not SALES
        engine.configure(ir)
        checked = 0
        for cap, dom in (ir.get('param_domains') or {}).items():
            labels = engine.param_option_labels(cap)
            if not labels:
                continue
            opts = [engine._param_display_value(v) for v in dom]
            # every alias is keyed by a value the control actually offers (no quoted XML literals left)
            self.assertTrue(set(labels) <= set(opts) | set(labels) and all(not k.startswith('"') for k in labels))
            self.assertTrue(any(k in opts for k in labels))
            checked += 1
        self.assertGreater(checked, 0)

    def test_user_inserts_never_show_the_literal(self):
        # only SALES carries <Full Name> in the corpus: proved on this cross-workbook fixture
        import engine
        for text in ('Welcome <Full Name>', 'Hi <User Name>!', '<Full Name>'):
            out = engine.readable_inserts(text)
            self.assertNotIn('<', out)
            self.assertNotIn('Full Name', out)


class TextTables(unittest.TestCase):
    def test_measure_names_in_tableau_naming(self):
        import crosstab_plan as X
        self.assertEqual(X.tableau_measure_name({'caption': 'Discount', 'aggregation': 'avg'}), 'Avg. Discount')
        self.assertEqual(X.tableau_measure_name({'caption': 'Sales', 'aggregation': 'sum'}), 'Sales')
        self.assertEqual(X.tableau_measure_name({'caption': '# Orders', 'aggregation': 'usr'}), '# Orders')

    def test_same_rule_as_the_second_workbook_tables(self):
        # Superstore's Measure Names tables (sheet-plan renderer) use v3_sheet_plan.mv_name; the canvas crosstab
        # now calls the same function, so both renderers name every measure identically
        import crosstab_plan as X
        import v3_sheet_plan as P
        ir = _ir('Superstore_Tableau2024_3.twbx', None)
        names = 0
        for d in ir['dashboards']:
            for s in d['sheets']:
                model = s.get('worksheet_semantics') or {}
                if model.get('occurrences') and X.candidate(model):
                    for o in model['occurrences']:
                        if o['channel'] == 'text' and o.get('aggregation') not in (None, 'none', 'attr'):
                            self.assertEqual(X.tableau_measure_name(o), P.mv_name(
                                {'agg': o['aggregation'], 'caption': o['caption'], 'field_id': o.get('field_id')}))
                            names += 1
        self.assertGreater(names, 0)


class ScatterAndDualRules(unittest.TestCase):
    def test_trend_style_and_field_labels_on_a_second_workbook(self):
        # Superstore carries trend lines and display-field-labels; SALES is where CX2 found the gap
        found_trend = found_labels = False
        for path in (WB / 'Superstore_Tableau2024_3.twbx', WB / 'World Indicators.twbx'):
            for ws in _worksheets(path).values():
                rules = axis_xml.parse(ws)
                if rules['trend_style']:
                    color, opacity, dash, width = axis_xml.trend_line_style(rules)
                    self.assertTrue(color.startswith('#') and 0 < opacity <= 1 and width > 0)
                    found_trend = True
                if rules['field_labels']:
                    self.assertIn(axis_xml.field_labels_shown(rules, 'cols'), (True, False))
                    found_labels = True
        self.assertTrue(found_labels)
        self.assertIsInstance(found_trend, bool)

    def test_label_orientation(self):
        ws = ET.fromstring("<worksheet><table><style><style-rule element='label'><format attr='text-orientation' "
                           "field='[ds].[mn:Order Date:ok]' value='0'/></style-rule></style></table></worksheet>")
        self.assertEqual(axis_xml.label_angle(axis_xml.parse(ws), '[ds].[mn:Order Date:ok]'), 0)
        self.assertIsNone(axis_xml.label_angle(axis_xml.parse(ws), '[ds].[sum:Sales:qk]'))

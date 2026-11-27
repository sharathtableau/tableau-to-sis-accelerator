"""Release 26c: Tableau's zone model (tiled, coordinate-positioned and floating zones) and the layout check."""
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import engine
import tableau_parser as T
import v3_layout_check as L
from ir_contract import layout_issues


def tree(zones_xml, sheets=('A', 'B', 'C'), windows=None):
    d = ET.fromstring(f"<dashboard name='D'><zones>{zones_xml}</zones></dashboard>")
    return T.layout_tree(d, set(sheets), frozenset(), {}, None, windows)


def check(layout, sheets=('A', 'B', 'C')):
    dash = {'name': 'D', 'layout': layout, 'canvas': {'minwidth': 1000, 'maxwidth': 1000,
                                                      'minheight': 800, 'maxheight': 800},
            'sheets': [{'name': s} for s in sheets]}
    rows = L.check_dashboard(dash)
    return {r['Zone']: r['Status'] for r in rows}, rows


def leaves(node, key='sheet'):
    return [n[key] for n in engine._leaves(node, key)]


class CanvasTests(unittest.TestCase):
    def test_canvas_rule_equals_the_engine(self):
        for canvas in ({}, {'minheight': 800, 'maxheight': 800}, {'minwidth': 1200, 'minheight': 900},
                       {'minwidth': 800, 'minheight': 600, 'maxheight': 1000}):
            self.assertEqual(L.canvas_zone_px(canvas), engine.canvas_zone_px(canvas))


class GeometryTests(unittest.TestCase):
    def test_coordinate_container_stacks_a_column_beside_a_tall_zone(self):
        # A and B stacked on the left, C tall on the right: a row grouping put all three side by side
        layout = tree("<zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
                      "<zone name='A' x='0' y='0' w='60000' h='50000'/>"
                      "<zone name='B' x='0' y='50000' w='60000' h='50000'/>"
                      "<zone name='C' x='60000' y='0' w='40000' h='100000'/></zone>")
        self.assertEqual(layout['dir'], 'horz')
        self.assertEqual(layout['children'][0]['dir'], 'vert')
        self.assertEqual(leaves(layout['children'][0]), ['A', 'B'])
        status, _rows = check(layout)
        self.assertEqual(set(status.values()), {'Placed'})

    def test_gaps_and_narrow_zones_keep_their_position(self):
        layout = tree("<zone type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='20000'>"
                      "<zone name='A' x='0' y='0' w='25000' h='20000'/>"
                      "<zone name='B' x='25000' y='0' w='25000' h='20000'/></zone>")
        self.assertTrue(any(k.get('_spacer') for k in layout['children']))     # the empty half of the row
        status, _rows = check(layout)
        self.assertEqual(set(status.values()), {'Placed'})

    def test_two_rows_with_the_same_background_never_merge(self):
        layout = tree("<zone type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='40000'>"
                      "<zone type-v2='layout-flow' param='horz' x='0' y='0' w='100000' h='20000'>"
                      "<zone-style><format attr='background-color' value='#ffffff'/></zone-style>"
                      "<zone name='A' x='0' y='0' w='50000' h='20000'/><zone name='B' x='50000' y='0' w='50000' h='20000'/></zone>"
                      "<zone type-v2='layout-flow' param='horz' x='0' y='20000' w='100000' h='20000'>"
                      "<zone-style><format attr='background-color' value='#ffffff'/></zone-style>"
                      "<zone name='C' x='0' y='20000' w='50000' h='20000'/><zone type-v2='text' x='50000' y='20000' w='50000' h='20000'>"
                      "<formatted-text><run>Note</run></formatted-text></zone></zone></zone>")
        status, _rows = check(layout)
        self.assertEqual(set(status.values()), {'Placed'})


class FloatingTests(unittest.TestCase):
    def test_a_zone_floating_over_a_chart_is_drawn_on_top_at_its_rectangle(self):
        layout = tree("<zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
                      "<zone name='A' x='0' y='0' w='100000' h='100000'/></zone>"
                      "<zone type-v2='text' x='70000' y='5000' w='20000' h='8000'><formatted-text><run>Overlay</run>"
                      "</formatted-text></zone>")
        self.assertEqual(len(layout.get('floating') or []), 1)
        self.assertEqual(layout_issues(layout, {'A'}), [])
        status, _rows = check(layout)
        self.assertEqual(set(status.values()), {'Placed'})

    def test_a_background_image_becomes_the_backdrop(self):
        layout = tree("<zone type-v2='layout-basic' x='0' y='0' w='100000' h='100000'>"
                      "<zone type-v2='bitmap' param='Image/bg.png' x='0' y='0' w='100000' h='100000'/></zone>"
                      "<zone type-v2='layout-flow' param='vert' x='10000' y='0' w='90000' h='100000'>"
                      "<zone name='A' x='10000' y='0' w='90000' h='100000'/></zone>")
        self.assertEqual(layout['backdrop']['image'], 'Image/bg.png')
        self.assertEqual(leaves(layout), ['A'])
        self.assertEqual(layout_issues(layout, {'A'}), [])

    def test_navigation_and_show_hide_buttons(self):
        uuid = '{8AF81AC7-409F-4857-A1EF-CC04E956EADA}'
        layout = tree("<zone type-v2='layout-flow' param='vert' x='0' y='0' w='100000' h='100000'>"
                      f"<zone type-v2='dashboard-object' x='0' y='0' w='20000' h='8000'>"
                      f"<button action='tabdoc:goto-sheet window-id=&quot;{uuid}&quot;'><button-visual-state>"
                      "<caption>Next</caption></button-visual-state></button></zone>"
                      "<zone type-v2='dashboard-object' x='0' y='8000' w='20000' h='8000'><button action=''>"
                      "<toggle-action>tabdoc:toggle-button-click-action zone-ids=[9]</toggle-action></button></zone>"
                      "<zone id='9' hidden-by-user='true' type-v2='layout-flow' param='vert' x='0' y='16000' w='100000' h='84000'>"
                      "<zone name='A' x='0' y='16000' w='100000' h='84000'/></zone></zone>",
                      windows={uuid: 'Other dashboard'})
        self.assertEqual(engine._leaves(layout, 'nav')[0]['nav'], 'Other dashboard')
        toggle = engine._leaves(layout, 'toggle')[0]
        self.assertEqual((toggle['toggle'], toggle['start_hidden']), (['9'], [True]))
        hidden = [n for n in layout['children'] if n.get('zid') == '9'][0]
        self.assertTrue(hidden['start_hidden'])
        self.assertEqual(layout_issues(layout, {'A'}), [])
        _status, rows = check(layout)
        self.assertTrue(any(r['Tableau'] == 'hidden at open' for r in rows))


class ContractTests(unittest.TestCase):
    def test_zone_leaves_are_validated(self):
        self.assertEqual(layout_issues({'control': 'filter', 'w': 1, 'h': 1}, set()), [])
        self.assertTrue(layout_issues({'control': 'script', 'w': 1}, set()))
        self.assertTrue(layout_issues({'legend': 'Unknown'}, {'A'}))
        bad = {'dir': 'vert', 'children': [], 'floating': [{'child': {'sheet': 'A'}}]}
        self.assertTrue(layout_issues(bad, {'A'}))


class CorpusTests(unittest.TestCase):
    def _all_placed(self, name):
        path = ROOT / 'Workbooks' / name
        if not path.exists():
            self.skipTest('corpus workbook not present')
        _rows, summary = L.check_ir(T.build_ir(str(path), scoped=True))
        self.assertGreater(summary['zones'], 0)
        self.assertEqual(summary['Placed'], summary['zones'], summary)

    def test_sales_dashboard_every_zone_placed(self):
        self._all_placed('Sales Dashboard Ver 01.twbx')

    def test_floating_ecommerce_every_zone_placed(self):
        self._all_placed('E-Commerce (Software) Sales Dashboard VOTD.twbx')

    def test_superstore_every_zone_placed(self):
        self._all_placed('cloud_20261010/Superstore_Tableau2024_3.twbx')


if __name__ == '__main__':
    unittest.main()

"""V8: Tableau's Polygon mark on continuous axes (Sankey ribbons, custom shapes). Written before the code. Tableau
joins the marks of each group (the colour / detail dimensions other than the Path field) in Path order into one
filled polygon at (x, y) = the Columns and Rows measures; marks without a value (densification padding) are skipped.
The plan treated Polygon as a map and returned no chart."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_sheet_plan as P  # noqa: E402


class Eng:
    import altair as alt
    value_labels = staticmethod(lambda cap: {})
    cat_colors = staticmethod(lambda cap, values: (None, None))


def plan():
    return {'schema': P.SCHEMA, 'mark': 'map', 'authored_mark': 'Polygon', 'sort': [], 'aggregate': True, 'fields': [
        {'id': 'x', 'caption': 'X', 'channel': 'cols', 'kind': 'measure', 'agg': 'usr', 'discrete': False,
         'date_part': 'none'},
        {'id': 'y', 'caption': 'Y', 'channel': 'rows', 'kind': 'measure', 'agg': 'usr', 'discrete': False,
         'date_part': 'none'},
        {'id': 'c', 'caption': 'Shape', 'channel': 'color', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
        {'id': 'p', 'caption': 'Point', 'channel': 'path', 'kind': 'dim', 'discrete': True, 'date_part': 'none'}]}


def frame():
    import pandas as pd
    rows = [('a', 2, 1, 0), ('a', 0, 0, 0), ('a', 1, 1, 1), ('a', 3, 0, 1), ('a', 4, None, None),
            ('b', 0, 2, 2), ('b', 1, 3, 2), ('b', 2, 3, 3)]
    return pd.DataFrame(rows, columns=['D|none|Shape', 'D|none|Point', 'M|usr|X', 'M|usr|Y'])


class Polygons(unittest.TestCase):
    def test_one_polygon_per_group_in_path_order(self):
        feats = P._polygon_features(plan(), frame())
        self.assertEqual([f['properties']['__group'] for f in feats], ['a', 'b'])
        ring = feats[0]['geometry']['coordinates'][0]
        self.assertEqual(ring, [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]])      # path 0,1,2,3; padded mark skipped

    def test_drawn_as_filled_shapes(self):
        d = str(P.build_chart(plan(), frame(), Eng).to_dict())
        self.assertIn("'geoshape'", d)
        self.assertIn("'identity'", d)

    def test_gold_crm_sankey(self):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / 'GOLD CRM Scorecard Cohort Movement.twbx'), recursive=True)[0]
        r = PC.intake(path)
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Sankey')
        pl = P.from_sheet(s, ir)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(pl, s, engine)
            self.assertGreater(len(P._polygon_features(pl, df)), 1)
            self.assertIsNotNone(P.build_chart(pl, df, engine, {'w': 700, 'h': 400}))


if __name__ == '__main__':
    unittest.main()

"""V5: maps on generated latitude / longitude without a Tableau capture. Written before the code.
A verified plan draws each location at Tableau's own coordinates (from the capture). Without them the plan returned no
chart and the app showed a table (16 corpus maps). Now locations are placed at the centroid of their country or US
state from the bundled base maps (v3_basemaps), labelled approximate placement; rows are never dropped (values are
unchanged), a location the base maps do not know is simply not drawn."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_geocode as G  # noqa: E402
import v3_sheet_plan as P  # noqa: E402


class Centroids(unittest.TestCase):
    def test_countries_and_states(self):
        lat, lon = G.locate('France', 'country')
        self.assertTrue(42 < lat < 50 and -2 < lon < 8, (lat, lon))
        lat, lon = G.locate('Texas', 'state')
        self.assertTrue(28 < lat < 34 and -103 < lon < -95, (lat, lon))
        lat, lon = G.locate('United States', 'country')           # Tableau's name, the base map's alias
        self.assertTrue(30 < lat < 50 and -110 < lon < -85, (lat, lon))
        self.assertIsNone(G.locate('Atlantis', 'country'))


class Plan(unittest.TestCase):
    def plan(self):
        return {'schema': P.SCHEMA, 'mark': 'circle', 'sort': [], 'aggregate': True, 'fields': [
            {'id': 'x', 'caption': 'Longitude (generated)', 'channel': 'cols', 'kind': 'geo', 'discrete': False,
             'date_part': 'none', 'agg': 'none'},
            {'id': 'y', 'caption': 'Latitude (generated)', 'channel': 'rows', 'kind': 'geo', 'discrete': False,
             'date_part': 'none', 'agg': 'none'},
            {'id': 'c', 'caption': 'Country', 'channel': 'lod', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
            {'id': 's', 'caption': 'Sales', 'channel': 'size', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
             'date_part': 'none'}]}

    def test_rows_get_coordinates_and_none_are_dropped(self):
        import pandas as pd
        df = pd.DataFrame({'D|none|Country': ['France', 'Atlantis', 'Brazil'], 'M|sum|Sales': [1.0, 2.0, 3.0]})
        out = G.place(self.plan(), df)
        self.assertEqual(len(out), 3)
        self.assertTrue(out['__lat'].notna().tolist() == [True, False, True])
        self.assertEqual(out.attrs.get('placement'), 'approximate')

    def test_the_map_is_drawn(self):
        import altair
        import pandas as pd

        class Eng:
            alt = altair
            value_labels = staticmethod(lambda cap: {})
            cat_colors = staticmethod(lambda cap, values: (None, None))
        plan = self.plan()
        plan['mark'] = 'map'
        df = G.place(plan, pd.DataFrame({'D|none|Country': ['France', 'Brazil'], 'M|sum|Sales': [1.0, 3.0]}))
        self.assertIsNotNone(P.build_chart(plan, df, Eng))


class Corpus(unittest.TestCase):
    """Superstore 'Sale Map' (US states, filled) and Online Retail 'Location.' (countries) draw from the plan."""
    def chart(self, wb, sheet):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True)[0]
        r = PC.intake(path)
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet)
        plan = P.from_sheet(s, ir)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(plan, s, engine)
            return P.build_chart(plan, df, engine, {'w': 700, 'h': 400})

    def test_superstore_sale_map(self):
        self.assertIsNotNone(self.chart('Superstore_Tableau2024_3.twbx', 'Sale Map'))

    def test_online_retail_location(self):
        self.assertIsNotNone(self.chart('Online Retail Dashboard.twbx', 'Location.'))


if __name__ == '__main__':
    unittest.main()

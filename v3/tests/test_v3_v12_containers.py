"""V12: the dashboard's container formatting. Written before the code. Tableau's Format Dashboard > containers
(<style-rule element='dash-container'>: background-color, border-style, border-color) applies to every layout container
that does not format itself; the canvas drew containers without it (Superstore Overview: #f7f7f7 cards with a #d7d7d7
border)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]


class Containers(unittest.TestCase):
    def test_default_applies_to_an_unformatted_container(self):
        import engine
        ds = {'dash-container': {'background-color': '#f7f7f7', 'border-style': 'solid', 'border-color': '#d7d7d7'}}
        self.assertEqual(engine._container_default({}, ds), {'bg': '#f7f7f7', 'border': {'width': 1, 'color': '#d7d7d7'}})

    def test_own_formatting_wins_and_transparent_is_no_fill(self):
        import engine
        ds = {'dash-container': {'background-color': '#00000000', 'border-style': 'solid'}}
        got = engine._container_default({'bg': '#ffffff'}, ds)
        self.assertEqual(got['bg'], '#ffffff')
        self.assertEqual(engine._container_default({}, ds).get('bg'), None)
        self.assertEqual(engine._container_default({}, {'dash-container': {'border-style': 'none'}}), {})


if __name__ == '__main__':
    unittest.main()

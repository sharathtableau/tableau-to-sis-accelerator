"""V13: Tableau stories. Written before the code. A story (dashboard type='storyboard') is a sequence of story points;
each shows a dashboard or sheet (captured-sheet) with the filters it captured (<capturedDeltas>). The app reported
stories as not converted; now each story is a tab with one tab per story point (its caption) showing that dashboard
with the captured categorical filters. Corpus: World Indicators (two workbooks)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]


def ir_of(wb):
    import glob
    import plan_census as PC
    return PC.intake(glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True)[0])['ir']


class Stories(unittest.TestCase):
    def test_points_and_captured_filters(self):
        ir = ir_of('World_Indicators_Tableau2024_3.twbx')
        story = ir['story_boards'][0]
        self.assertEqual(story['title'], 'Indicators of Global Growth and Development')
        self.assertEqual([p['sheet'] for p in story['points']],
                         ['Population', 'Care Spend', 'Technology', 'Economy', 'Tourism', 'Business'])
        tech = story['points'][2]
        self.assertEqual(tech['filters'], [{'worksheet': 'Technology', 'caption': 'Region', 'values': ['The Americas']}])

    def test_the_point_shows_its_dashboard_with_the_filter(self):
        import engine
        ir = ir_of('World_Indicators_Tableau2024_3.twbx')
        point = ir['story_boards'][0]['points'][2]
        dash = engine.story_dashboard(ir, point)
        self.assertEqual(dash['name'], 'Technology')
        sheet = next(s for s in dash['sheets'] if s['name'] == 'Technology')
        self.assertIn({'caption': 'Region', 'kind': 'in', 'values': ['The Americas'], 'source': 'story-point'},
                      sheet['applied_filters'])
        original = next(d for d in ir['dashboards'] if d['name'] == 'Technology')
        self.assertFalse(any(f.get('source') == 'story-point' for s in original['sheets']
                             for f in s.get('applied_filters') or []))           # a copy

    def test_second_workbook(self):
        ir = ir_of('World Indicators.twbx')
        self.assertEqual(len(ir['story_boards']), 1)
        self.assertGreaterEqual(len(ir['story_boards'][0]['points']), 5)


if __name__ == '__main__':
    unittest.main()

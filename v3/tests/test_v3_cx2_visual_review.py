"""CX2: Cortex proposes, code verifies -- the guard rails, offline (no Snowflake, no Tableau)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cx2_visual_review as cx2


class CX2Tests(unittest.TestCase):
    def test_parse_is_strict(self):
        text = ('{"findings":[{"category":"colour","severity":"high","tableau":"blue","app":"teal"},'
                '{"category":"made_up","severity":"high","tableau":"x","app":"y"},'
                '{"category":"axis","severity":"urgent","tableau":"x","app":"y"}]}')
        got = cx2.parse_reply(text)
        self.assertEqual([f['category'] for f in got], ['colour'])
        self.assertEqual(cx2.parse_reply('"' + text.replace('"', '\\"') + '"')[0]['app'], 'teal')
        self.assertEqual(cx2.parse_reply('the model refused'), [])
        self.assertEqual(cx2.parse_reply(None), [])

    def test_model_name_allowlist(self):
        self.assertEqual(cx2.safe_model('claude-sonnet-4-5'), 'claude-sonnet-4-5')
        for bad in ["x'); DROP TABLE t; --", '', 'A' * 80, None]:
            with self.assertRaises(ValueError):
                cx2.safe_model(bad)

    def test_verdicts_come_from_evidence_not_the_model(self):
        f = {'category': 'number_format', 'severity': 'high', 'tableau': '13,400', 'app': '13,600', 'xml_hint': ''}
        zone = {'issues': [], 'dom_text': '13,600 13,700', 'xml': "<format attr='text-format' value='#,##0'/>"}
        self.assertEqual(cx2.verify(f, zone)[0], 'CONFIRMED_DOM')
        zone['dom_text'] = '13,400 13,600'                        # Tableau's text IS in the app -> not confirmed
        self.assertEqual(cx2.verify(f, zone)[:2], ('XML_BACKED', 'format.text-format'))
        zone['xml'] = ''
        self.assertEqual(cx2.verify(f, zone)[0], 'ADVISORY')
        clip = {'category': 'missing_or_clipped', 'severity': 'medium', 'tableau': 'a', 'app': 'b', 'xml_hint': ''}
        self.assertEqual(cx2.verify(clip, {'issues': ['spills 7px below its zone'], 'dom_text': '', 'xml': ''})[0],
                         'CONFIRMED_GEOMETRY')

    def test_fix_queue_flags_single_workbook_rules(self):
        fs = [{'category': 'colour', 'construct': 'palette', 'verdict': 'XML_BACKED', 'severity': 'high', 'zone': 'v3cz1',
               'tableau': 'a', 'app': 'b'},
              {'category': 'axis', 'construct': 'encoding.space', 'verdict': 'CONFIRMED_DOM', 'severity': 'low',
               'zone': 'v3cz2', 'tableau': 'a', 'app': 'b'},
              {'category': 'font', 'construct': None, 'verdict': 'ADVISORY', 'severity': 'high', 'zone': 'v3cz3',
               'tableau': 'a', 'app': 'b'}]
        q = cx2.fix_queue(fs, {'palette': 1, 'encoding.space': 12}, 35)
        self.assertEqual(len(q), 2)                                # advisory never enters the queue
        by = {g['construct']: g for g in q}
        self.assertFalse(by['palette']['general']); self.assertIn('fixture', by['palette']['note'])
        self.assertTrue(by['encoding.space']['general'])

    def test_no_difference_claims_are_dropped(self):
        same = {'category': 'axis', 'severity': 'medium', 'tableau': 'Y-axis tick marks at 0, 10, 20, 30',
                'app': 'Y-axis tick marks at 0, 10, 20, 30', 'xml_hint': ''}
        zone = {'issues': [], 'dom_text': '0 10 20', 'xml': "element='axis'"}
        self.assertEqual(cx2.verify(same, zone)[0], 'NO_DIFFERENCE')
        diff = dict(same, app='Y-axis tick marks at 0, 5, 10, 15')
        self.assertNotEqual(cx2.verify(diff, zone)[0], 'NO_DIFFERENCE')
        q = cx2.fix_queue([{**same, 'zone': 'v3cz1', 'verdict': 'NO_DIFFERENCE', 'construct': 'style-rule.axis'}],
                          {'style-rule.axis': 30}, 35)
        self.assertEqual(q, [])

    def test_boxes_holding_a_sheet_are_skipped(self):
        zones = [{'key': 'b1', 'kind': 'box', 'rect': [0, 0, 300, 200]},
                 {'key': 's1', 'kind': 'sheet', 'rect': [5, 5, 290, 190]},
                 {'key': 'b2', 'kind': 'box', 'rect': [400, 0, 100, 100]}]     # a box with no sheet stays
        self.assertEqual([z['key'] for z in cx2.drop_container_boxes(zones)], ['s1', 'b2'])


if __name__ == '__main__':
    unittest.main()

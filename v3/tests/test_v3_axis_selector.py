"""Corpus-wide fix (6 Nov census: 118 of 435 sheets, 5 workbooks, refused by the sheet plan with "Unknown shelf
'x-axis-name' / 'y-axis-name'"). A pane's x-axis-name / y-axis-name attribute SELECTS an existing Cols / Rows field
(worksheet_semantics marks it as a selector); it is not a field of its own. The plan skips a selector whose field is on
the matching shelf and refuses one that points at nothing. Written BEFORE the fix (tests first)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_sheet_plan as P  # noqa: E402


def occ(i, channel, caption, token, role='measure', agg='sum', selector=False):
    return {'id': i, 'channel': channel, 'caption': caption, 'token': token, 'role': role, 'aggregation': agg,
            'discrete': False, 'datepart': None, 'field_id': 'ds::' + caption, 'datasource_id': 'ds',
            'field_definition': {'datatype': 'real'}, 'selector': selector, 'level': 'aggregate', 'pane_id': None}


def sheet(occurrences):
    return {'name': 'S', 'datasource_id': 'ds', 'mark': 'Bar',
            'worksheet_semantics': {'occurrences': occurrences, 'marks': {'mark': 'Bar'}}}


class AxisSelector(unittest.TestCase):
    def test_selector_on_its_shelf_is_not_a_field(self):
        base = [occ('o1', 'rows', 'Channel', '[ds].[none:Channel:nk]', role='dimension', agg='none'),
                occ('o2', 'cols', 'Revenue', '[ds].[sum:Revenue:qk]')]
        plain = P.from_sheet(sheet(base))
        with_sel = P.from_sheet(sheet(base + [occ('o3', 'x-axis-name', 'Revenue', '[ds].[sum:Revenue:qk]',
                                                  selector=True)]))
        self.assertEqual([(f['caption'], f['channel']) for f in with_sel['fields']],
                         [(f['caption'], f['channel']) for f in plain['fields']])

    def test_selector_without_its_shelf_field_is_refused(self):
        bad = [occ('o1', 'rows', 'Channel', '[ds].[none:Channel:nk]', role='dimension', agg='none'),
               occ('o3', 'y-axis-name', 'Profit', '[ds].[sum:Profit:qk]', selector=True)]
        with self.assertRaises(P.PlanError) as cm:
            P.from_sheet(sheet(bad))
        self.assertIn('axis', str(cm.exception).lower())

    def test_corpus_sheet_now_plans(self):
        from v3_local import prepare_payload
        p = ROOT / 'Workbooks' / 'E-Commerce (Software) Sales Dashboard VOTD.twbx'
        ir = prepare_payload(p.read_bytes(), p.name)['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Revenue by Channels')
        plan = P.from_sheet(s, ir)
        self.assertFalse(any(f['channel'] in ('x-axis-name', 'y-axis-name') for f in plan['fields']))


if __name__ == '__main__':
    unittest.main()

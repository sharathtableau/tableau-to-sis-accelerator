"""X13 / X14: per-sheet reviewer decisions carried into the deployed app; X15: hand edits found before a replace.
X13: a sheet the reviewer marks with a known-difference note shows that short note to app users under the sheet.
X14: a sheet the reviewer holds back is not drawn (a one-line notice with the reason instead); the rest of the
workbook deploys. Both are recorded in the IR ('review') and in the deploy approval. Written before the code."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_review as R  # noqa: E402

IR = {'dashboards': [{'name': 'D1', 'sheets': [{'name': 'A', 'kind': 'bar'}, {'name': 'B', 'kind': 'bar'}]},
                     {'name': 'D2', 'sheets': [{'name': 'B', 'kind': 'bar'}, {'name': 'C', 'kind': 'text'}]}]}


class Validate(unittest.TestCase):
    def test_sheet_names_in_order_once(self):
        self.assertEqual(R.sheet_names(IR), ['A', 'B', 'C'])

    def test_valid_review(self):
        rv = R.validate({'held_back': {'A': 'Values differ'}, 'notes': {'B': 'Rounding differs from Tableau'}}, IR)
        self.assertEqual(rv, {'held_back': {'A': 'Values differ'}, 'notes': {'B': 'Rounding differs from Tableau'}})

    def test_refusals(self):
        for bad in ({'held_back': {'Z': 'x'}},                      # unknown sheet
                    {'held_back': {'A': '  '}},                     # a hold needs a reason
                    {'notes': {'A': 'x' * 301}},                    # short notes only
                    {'held_back': {'A': 'r', 'B': 'r', 'C': 'r'}},  # nothing left to deploy
                    {'held_back': {'A': 'r'}, 'notes': {'A': 'n'}},  # held back and noted
                    {'other': {}}):
            with self.assertRaises(ValueError, msg=bad):
                R.validate(bad, IR)

    def test_empty_review_is_none(self):
        self.assertEqual(R.validate({'held_back': {}, 'notes': {'A': ' '}}, IR), {'held_back': {}, 'notes': {}})


class Apply(unittest.TestCase):
    def test_flags_every_occurrence_and_keeps_the_input(self):
        out = R.apply(IR, {'held_back': {'B': 'Wrong grain'}, 'notes': {'C': 'Known difference'}})
        flags = [(s['name'], s.get('review_hold'), s.get('review_note')) for d in out['dashboards'] for s in d['sheets']]
        self.assertEqual(flags, [('A', None, None), ('B', 'Wrong grain', None), ('B', 'Wrong grain', None),
                                 ('C', None, 'Known difference')])
        self.assertEqual(out['review'], {'held_back': {'B': 'Wrong grain'}, 'notes': {'C': 'Known difference'}})
        self.assertNotIn('review', IR)
        self.assertNotIn('review_hold', IR['dashboards'][0]['sheets'][1])

    def test_differences_from_validation(self):
        run = {'sheet_reference': {'results': {'A': ({'Status': 'Difference found'}, None),
                                               'B': ({'Status': 'Matched'}, None)}}}
        self.assertEqual(R.differences(run), {'A': 'Difference found', 'B': 'Matched'})
        self.assertEqual(R.differences({}), {})


class Render(unittest.TestCase):
    def test_held_back_sheet_is_not_drawn(self):
        import engine
        calls = []
        with patch.object(engine, '_render_sheet_contained', side_effect=lambda s, w: calls.append(s['name'])), \
                patch.object(engine.st, 'info') as info:
            engine.render_sheet({'name': 'A', 'kind': 'bar', 'review_hold': 'Values differ'}, [])
        self.assertEqual(calls, [])
        self.assertIn('Values differ', info.call_args[0][0])

    def test_note_shows_under_the_drawn_sheet(self):
        import engine
        calls = []
        with patch.object(engine, '_render_sheet_contained', side_effect=lambda s, w: calls.append(s['name'])), \
                patch.object(engine.st, 'caption') as caption:
            engine.render_sheet({'name': 'B', 'kind': 'bar', 'review_note': 'Rounding differs'}, [])
        self.assertEqual(calls, ['B'])
        self.assertIn('Rounding differs', caption.call_args[0][0])


class Packaged(unittest.TestCase):
    def test_the_console_package_carries_it(self):
        import v3_package
        self.assertIn('v3_review.py', v3_package.modules(['v3_sis_app.py', 'v3_console.py']))


class Console(unittest.TestCase):
    def run_editor(self, edit):
        import v3_console as C

        def editor(rows, **kw):
            rows = [dict(r) for r in rows]
            edit(rows)
            return rows
        run = {'sheet_reference': {'results': {'A': ({'Status': 'Difference found'}, None)}}}
        with patch.object(C.st, 'data_editor', side_effect=editor), patch.object(C.st, 'caption'), \
                patch.object(C.st, 'warning') as warn:
            out = C._review_editor(run, {'ir': IR, 'digest': 'x'})
        return out, warn

    def test_decisions_reach_the_review(self):
        def edit(rows):
            self.assertEqual(rows[0]['Validation'], 'Difference found')
            rows[0].update({'Hold back': True, 'Reason': 'Values differ'})
            rows[1].update({'Note for app users': 'Rounded differently'})
        (review, error), _w = self.run_editor(edit)
        self.assertIsNone(error)
        self.assertEqual(review, {'held_back': {'A': 'Values differ'}, 'notes': {'B': 'Rounded differently'}})

    def test_a_hold_without_reason_blocks_deploy(self):
        (review, error), warn = self.run_editor(lambda rows: rows[0].update({'Hold back': True}))
        self.assertIn('why', error)
        warn.assert_called_once()

class FakeRow:
    def __init__(self, **kw):
        self.kw = kw

    def as_dict(self):
        return self.kw


class FakeStream:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


class FakeSession:
    """A deployed V3 app folder: its build manifest and its files (some changed by hand)."""
    def __init__(self, files, manifest_files, root='@WBR_DB.TABLEAU_ACCELERATOR_V3.V3_WORKBOOK_STAGE/V3_WORKBOOK_AB'):
        import json
        self.root = root
        self.files = dict(files, **{'candidate_build.json': json.dumps({'files': manifest_files}).encode()})
        session = self

        class File:
            @staticmethod
            def get_stream(path):
                name = path.rsplit('/', 1)[-1]
                if name not in session.files:
                    raise FileNotFoundError(name)
                return FakeStream(session.files[name])
        self.file = File()

    def sql(self, text):
        session = self

        class Q:
            @staticmethod
            def collect():
                if text.startswith('DESCRIBE STREAMLIT'):
                    return [FakeRow(name='APP', root_location=session.root)]
                if text.startswith('LIST'):
                    return [FakeRow(name=session.root.lstrip('@').lower() + '/' + n) for n in session.files]
                raise AssertionError(text)
        return Q()


class X15EditedFiles(unittest.TestCase):
    def sha(self, b):
        import hashlib
        return hashlib.sha256(b).hexdigest()

    def test_unchanged_app(self):
        import v3_deploy as D
        files = {'app.py': b'a', 'engine.py': b'e', 'datasources.json': b'{}'}
        s = FakeSession(files, {'app.py': self.sha(b'a'), 'engine.py': self.sha(b'e')})
        self.assertEqual(D.edited_files(s, 'MY_APP'), ([], None))

    def test_hand_edits_are_listed(self):
        import v3_deploy as D
        files = {'app.py': b'a EDITED', 'extra.py': b'x', 'datasources.json': b'{}'}
        s = FakeSession(files, {'app.py': self.sha(b'a'), 'engine.py': self.sha(b'e')})
        changes, reason = D.edited_files(s, 'MY_APP')
        self.assertIsNone(reason)
        self.assertEqual(changes, [{'File': 'app.py', 'Change': 'edited'}, {'File': 'engine.py', 'Change': 'removed'},
                                   {'File': 'extra.py', 'Change': 'added'}])

    def test_not_checkable_says_why(self):
        import v3_deploy as D
        s = FakeSession({}, {}, root='@OTHER_DB.S.STAGE/X')
        changes, reason = D.edited_files(s, 'MY_APP')
        self.assertIsNone(changes)
        self.assertIn('not deployed by this accelerator', reason)
        s = FakeSession({'app.py': b'a'}, {})
        del s.files['candidate_build.json']
        self.assertIsNone(D.edited_files(s, 'MY_APP')[0])

    def test_console_needs_a_second_confirmation_when_edited(self):
        import v3_console as C
        with patch('v3_deploy.edited_files', return_value=([{'File': 'app.py', 'Change': 'edited'}], None)), \
                patch.object(C.backend, '_active_session', return_value=None), \
                patch.object(C.st, 'warning'), patch.object(C.st, 'dataframe'), \
                patch.object(C.st, 'checkbox', return_value=False):
            ok, record = C._replace_check('MY_APP', {'digest': 'x'})
        self.assertFalse(ok)
        self.assertEqual(record['changes'], [{'File': 'app.py', 'Change': 'edited'}])
        with patch('v3_deploy.edited_files', return_value=([], None)), \
                patch.object(C.backend, '_active_session', return_value=None), patch.object(C.st, 'caption'):
            self.assertTrue(C._replace_check('MY_APP', {'digest': 'x'})[0])

if __name__ == '__main__':
    unittest.main()

"""E10: session isolation, roles and cache identity. Written BEFORE the code (tests first).

Decision (DEC-03, generic for both SiS runtimes): whether Streamlit in Snowflake gives each viewer a process or shares
one, the app is correct:
  * engine state (IR, CALCS, datasource routing) is only set inside v3_local.preview_context, which holds one process
    lock for the whole render and restores the previous state -- two sessions never see each other's workbook;
  * the query cache is keyed by the VIEWER in a hosted session: row access policies on CURRENT_USER() (EN27) give
    different rows to different people for the same SQL, so one viewer's cached result is never served to another;
  * roles: an access table WBR_DB.V3_PORTAL.ROLES (USER_NAME, ROLE) gives AUTHOR (migrate, review, approve fixes) and
    PUBLISHER (deploy, portal); everyone else is a VIEWER (Overview, Estate catalogue). No table / no rows = open mode
    (today's behaviour, with a note). A refused page says ROLE_REQUIRED:<role> and nothing else."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import engine  # noqa: E402
import v3_roles as R  # noqa: E402


class CacheIdentity(unittest.TestCase):
    def setUp(self):
        engine._q_exec.clear()

    def test_one_viewer_reuses_another_does_not(self):
        calls = []
        with patch.object(engine, 'run_sql', side_effect=lambda sql: calls.append(sql) or [1]), \
                patch('backend._active_session', return_value=object()):
            with patch.object(engine.st, 'user', {'email': 'alice@x'}):
                engine._q_exec('SELECT 1 AS A')
                engine._q_exec('SELECT 1 AS A')
            self.assertEqual(len(calls), 1)
            with patch.object(engine.st, 'user', {'email': 'bob@x'}):
                engine._q_exec('SELECT 1 AS A')
            self.assertEqual(len(calls), 2)

    def test_local_preview_is_not_keyed(self):
        self.assertIsNone(engine.viewer_key())
        with patch('backend._active_session', return_value=object()), \
                patch.object(engine.st, 'user', {}):
            self.assertEqual(engine.viewer_key(), 'anonymous')

    def test_clear_still_works(self):
        self.assertTrue(callable(engine._q_exec.clear))


class Concurrency(unittest.TestCase):
    def test_two_sessions_two_workbooks(self):
        import plan_census as PC
        from v3_local import preview_context
        wbs = [ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx',
               next((ROOT / 'Workbooks').glob('E-Commerce*.twbx'), None)]
        if not all(w and w.exists() for w in wbs):
            self.skipTest('corpus workbooks')
        intakes = [PC.intake(w) for w in wbs]

        def count(r):
            with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
                sheet = r['ir']['dashboards'][0]['sheets'][0]
                with engine.field_scope(sheet):
                    T = engine.tbl(sheet)
                    n = int(engine.q(f'SELECT COUNT(*) AS N FROM {T}').iloc[0, 0])
                return T, n, sorted(engine.CALCS)[:3]
        expected = [count(r) for r in intakes]
        self.assertNotEqual(expected[0][0], expected[1][0])
        got, errors = {}, []

        def worker(i):
            try:
                for _ in range(3):
                    got.setdefault(i, []).append(count(intakes[i]))
            except Exception as exc:                       # pragma: no cover - the assertion reports it
                errors.append(repr(exc))
        threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(errors, [])
        for i in (0, 1):
            self.assertEqual(got[i], [expected[i]] * 3)


class FakeSession:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.seen = rows or [], fail, []

    def sql(self, text, params=None):
        self.seen.append((text, params))
        outer = self

        class Res:
            def collect(_):
                if outer.fail:
                    raise RuntimeError('does not exist or not authorized')
                return outer.rows
        return Res()


class Roles(unittest.TestCase):
    def test_roles_from_the_access_table(self):
        s = FakeSession([('AUTHOR',), ('PUBLISHER',)])
        got = R.roles_for(s, 'Alice@X')
        self.assertEqual(got, {'mode': 'TABLE', 'roles': {'AUTHOR', 'PUBLISHER', 'VIEWER'}})
        self.assertEqual(s.seen[-1][1], ['ALICE@X'])                  # bound, case-insensitive user name

    def test_open_mode_without_a_table_or_rows(self):
        self.assertEqual(R.roles_for(FakeSession(fail=True), 'a')['mode'], 'OPEN')
        self.assertEqual(R.roles_for(FakeSession([]), 'a')['mode'], 'OPEN_NO_ROWS')
        self.assertEqual(R.roles_for(None, None)['mode'], 'LOCAL')
        for mode in ('OPEN', 'OPEN_NO_ROWS', 'LOCAL'):
            self.assertTrue(R.allowed('Deploy', {'mode': mode, 'roles': set()}))

    def test_open_no_rows_when_the_table_is_empty_for_everyone(self):
        class S(FakeSession):
            def sql(self, text, params=None):
                self.seen.append((text, params))
                rows = [] if params else [(0,)]

                class Res:
                    def collect(_):
                        return rows
                return Res()
        self.assertEqual(R.roles_for(S(), 'a')['mode'], 'OPEN_NO_ROWS')

    def test_a_listed_table_denies_unlisted_people(self):
        class S(FakeSession):
            def sql(self, text, params=None):
                self.seen.append((text, params))
                rows = [] if params else [(3,)]                     # 3 rows exist, none for this user

                class Res:
                    def collect(_):
                        return rows
                return Res()
        got = R.roles_for(S(), 'mallory')
        self.assertEqual(got, {'mode': 'TABLE', 'roles': {'VIEWER'}})
        self.assertTrue(R.allowed('Overview', got))
        self.assertTrue(R.allowed('Estate catalogue', got))
        self.assertEqual(R.required('New migration', got), 'AUTHOR')
        self.assertEqual(R.required('Deploy', got), 'PUBLISHER')
        author = {'mode': 'TABLE', 'roles': {'AUTHOR', 'VIEWER'}}
        self.assertTrue(R.allowed('Validation', author))
        self.assertFalse(R.allowed('Deploy', author))

    def test_setup_sql(self):
        sql = '\n'.join(R.setup_sql('WBR_OWNER'))
        self.assertIn('CREATE TABLE IF NOT EXISTS WBR_DB.V3_PORTAL.ROLES', sql)
        self.assertNotIn('OR REPLACE', sql.upper())
        with self.assertRaises(ValueError):
            R.setup_sql("X; DROP")

    def test_console_gates_pages(self):
        src = (ROOT / 'v3_console.py').read_text(encoding='utf-8')
        self.assertIn('v3_roles', src)
        self.assertIn('ROLE_REQUIRED:', src)


if __name__ == '__main__':
    unittest.main()

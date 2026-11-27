"""E12 (constrained SQL; no model SQL execution): a static guard over the modules actually packaged into the Cortex
lab app (v3_package.modules import closure). Written BEFORE the hardening (tests first).

Invariants:
  1. No function executes SQL it received as a parameter, except the two reviewed helpers whose callers pass only
     code-built SQL from validated identifiers (v3_semantic._one_row, v3_source_binding.rows) and the engine's
     backend.run_sql (engine-built queries).
  2. Inside one function, a value returned by a model call (Cortex Analyst, Cortex Agent, AI_COMPLETE wrappers) never
     reaches .sql(...) -- except v3_semantic.run_read_only, the approved Ask-the-data path.
  3. run_read_only runs one read-only SELECT / WITH only: no writes, no second statement, no SYSTEM$ function, no
     EXECUTE IMMEDIATE / EXECUTE TASK, no CALL.
  4. The original accelerator's model-SQL executors are not packaged (cortex_calc_fallback, pipeline_app_ai_v2).
"""
import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REVIEWED_PASSTHROUGH = {('v3_semantic.py', '_one_row'), ('v3_source_binding.py', 'rows'), ('backend.py', 'run_sql')}
MODEL_CALLS = {'ask_analyst', 'cortex_analyst', 'run_agent', 'call', 'complete', 'Complete', 'ask', 'cortex_ask',
               'send_message', 'parse_response'}
APPROVED_MODEL_SQL = {('v3_semantic.py', 'run_read_only')}


def bundle():
    import v3_package as V
    return sorted(k for k in V.modules(['v3_sis_app.py', 'v3_console.py', 'v3_app.py', 'v3_runtime.py'])
                  if k.endswith('.py'))


def functions(path):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def sql_calls(fn):
    return [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == 'sql' and c.args]


class Guard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mods = bundle()

    def test_no_new_sql_passthrough(self):
        found = set()
        for m in self.mods:
            for fn in functions(m):
                params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
                assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign) for t in n.targets
                            if isinstance(t, ast.Name)}
                for c in sql_calls(fn):
                    a = c.args[0]
                    if isinstance(a, ast.Name) and a.id in params and a.id not in assigned:
                        found.add((m, fn.name))
        self.assertTrue(found <= REVIEWED_PASSTHROUGH, f'new SQL passthrough, review it: {found - REVIEWED_PASSTHROUGH}')

    def test_model_output_never_reaches_execution(self):
        bad = []
        for m in self.mods:
            for fn in functions(m):
                if (m, fn.name) in APPROVED_MODEL_SQL:
                    continue
                tainted = set()
                for n in ast.walk(fn):
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                        f = n.value.func
                        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', '')
                        if name in MODEL_CALLS:
                            for t in n.targets:
                                for x in ast.walk(t):
                                    if isinstance(x, ast.Name):
                                        tainted.add(x.id)
                for c in sql_calls(fn):
                    used = {x.id for x in ast.walk(c.args[0]) if isinstance(x, ast.Name)}
                    if used & tainted:
                        bad.append((m, fn.name, sorted(used & tainted)))
        self.assertEqual(bad, [])

    def test_legacy_model_sql_executors_are_not_packaged(self):
        for legacy in ('cortex_calc_fallback.py', 'pipeline_app_ai_v2.py'):
            self.assertNotIn(legacy, self.mods)


class ReadOnly(unittest.TestCase):
    class S:
        def __init__(self):
            self.seen = []

        def sql(self, text):
            self.seen.append(text)
            outer = self

            class R:
                def collect(self_inner):
                    return []
            return R()

    def test_refuses_everything_but_one_read_only_select(self):
        import v3_semantic as SV
        s = self.S()
        SV.run_read_only(s, 'SELECT REGION, SUM(SALES) FROM T GROUP BY 1')
        self.assertTrue(s.seen[0].startswith('SELECT * FROM (SELECT REGION'))
        for bad in ('DELETE FROM T', 'SELECT 1; DROP TABLE T', 'SELECT SYSTEM$CANCEL_ALL_QUERIES(1)',
                    'SELECT system$wait(10)', 'WITH x AS (SELECT 1) SELECT * FROM x; CALL p()',
                    "EXECUTE IMMEDIATE 'DROP TABLE T'", 'SELECT 1 FROM T WHERE 1 = (CALL p())',
                    'EXECUTE TASK t', 'CREATE TABLE X AS SELECT 1'):
            with self.assertRaises(ValueError, msg=bad):
                SV.run_read_only(self.S(), bad)


if __name__ == '__main__':
    unittest.main()

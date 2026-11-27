"""E13 (first slice): read-only discovery and an explicit, closed list of the places that change Snowflake.
Written before the gate. A static scan of the packaged modules (v3_package import closure) finds every function that
runs a mutating statement (CREATE / INSERT / UPDATE / MERGE / ALTER / DROP / GRANT / ...) through session.sql, from a
literal or a string assigned in the same function. The set must equal the reviewed list below -- a new place that
changes Snowflake fails the gate until it is reviewed (and put behind an approval); the discovery modules (reading
sources, binding, the local preview) change nothing. A runtime guard that refuses any mutation outside an approved
mutation context is the next slice (needs the live app to prove)."""
import ast
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MUT = re.compile(r'^\s*(CREATE|INSERT|ALTER|DROP|GRANT|REVOKE|MERGE|UPDATE|DELETE|TRUNCATE|PUT|COPY|CALL|EXECUTE|'
                 r'UNDROP)\b', re.I)
# reviewed: (module, function, statement) -> why it is allowed
REVIEWED = {
    ('cx2_safety.py', '_insert', 'INSERT'): 'evaluation store rows (CX9), only after the owner-run setup SQL',
    ('cx2_safety.py', 'label_evaluation', 'UPDATE'): 'a person labels an evaluation row',
    ('cx3_calc.py', 'save_verified', 'MERGE'): 'a VERIFIED translation kept in the owner-created library table',
    ('parity.py', 'ensure_vision_stage', 'CREATE'): 'original accelerator parity stage (packaged, owner flow)',
    ('pipeline.py', '_fix_date_columns_session', 'ALTER'): 'approved data load: column types of loaded tables',
    ('pipeline.py', '_fix_date_columns_session', 'UPDATE'): 'approved data load: column types of loaded tables',
    ('pipeline.py', 'deploy_streamlit_app', 'CREATE'): 'deploy, after the deploy approval',
    ('pipeline.py', 'ensure_target', 'CREATE'): 'deploy target, after the deploy approval',
    ('v3_data_load.py', '_ensure_schema', 'CREATE'): 'approved data load (21a): the load schema',
    ('v3_data_load.py', '_fix_types', 'ALTER'): 'approved data load: column types',
    ('v3_data_load.py', '_fix_types', 'UPDATE'): 'approved data load: column types',
    ('v3_rationalise.py', '_writer', 'INSERT'): 'rationalisation report rows in the evidence store',
    ('v3_agent_tools.py', '_record', 'INSERT'): 'AG01: one run row per assessment in the owner-created RUNS table',
    ('v3_recheck.py', 'record_baseline', 'INSERT'): 'CC03: source baseline recorded by a person',
    ('v3_recheck.py', 'recheck', 'INSERT'): 'CC03: re-check results written by the owner-scheduled task',
    ('v3_accuracy.py', 'save', 'INSERT'): 'EN05: one accuracy row per release in the owner-created evaluation table',
    ('v3_memory.py', 'save', 'MERGE'): 'TM01: a translation / fix proven against Tableau and applied by a person, '
                                       'kept in the owner-created memory table',
}
DISCOVERY = ('v3_source_binding.py', 'v3_physical_binding.py', 'v3_model_binding.py', 'v3_local.py',
             'v3_relationship_query.py', 'v3_sheet_plan.py', 'engine.py', 'backend.py')


def lead(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant) else 'X' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return lead(node.left)
    return None


def mutations():
    import v3_package as V
    files = V.modules(['v3_sis_app.py', 'v3_console.py', 'v3_app.py', 'v3_runtime.py'])
    out = set()
    for name, payload in files.items():
        tree = ast.parse(payload.decode('utf-8-sig'))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                    t = lead(n.value)
                    if t:
                        names.setdefault(n.targets[0].id, []).append(t)
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'sql' and n.args:
                    a = n.args[0]
                    cands = [lead(a)] if lead(a) else (names.get(a.id, []) if isinstance(a, ast.Name) else [])
                    t = next((c for c in cands if MUT.match(c)), None)
                    if t:
                        out.add((name, fn.name, MUT.match(t).group(1).upper()))
    return out


class Mutations(unittest.TestCase):
    def test_every_place_that_changes_snowflake_is_reviewed(self):
        found = mutations()
        self.assertEqual(sorted(found - set(REVIEWED)), [], 'new mutation site: review it and add it here')
        self.assertEqual(sorted(set(REVIEWED) - found), [], 'reviewed site no longer exists: remove it here')

    def test_discovery_changes_nothing(self):
        self.assertEqual(sorted(m for m in mutations() if m[0] in DISCOVERY), [])

    def test_the_scan_sees_a_planted_mutation(self):
        tree = ast.parse("def f(s):\n    q = f'CREATE TABLE {x} (a int)'\n    s.sql(q).collect()\n")
        fn = tree.body[0]
        names = {'q': [lead(fn.body[0].value)]}
        self.assertTrue(MUT.match(names['q'][0]))


if __name__ == '__main__':
    unittest.main()

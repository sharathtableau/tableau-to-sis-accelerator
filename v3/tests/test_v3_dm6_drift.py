"""DM6 (E14): runtime revalidation of packaged bindings. Written BEFORE the fix. A deployed app re-checked relationship
plans (every 10 minutes) but never a single bound table, so a dropped column, a changed type or revoked access
surfaced as a raw query error. Now every binding is re-checked on the same cadence; a datasource that drifted is
left out of the run with a BLOCKER finding naming the problem (its sheets refuse as not bound), the others render."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

TYPES = {'string': 'VARCHAR(100)', 'real': 'FLOAT', 'integer': 'NUMBER(38,0)', 'date': 'DATE',
         'datetime': 'TIMESTAMP_NTZ', 'boolean': 'BOOLEAN'}


class Fake:
    def __init__(self, schema, denied=False):
        self.schema, self.denied, self.q = schema, denied, None

    def sql(self, text):
        self.q = text
        return self

    def collect(self):
        if self.denied and 'LIMIT 0' in self.q:
            raise RuntimeError('SQL access control error: insufficient privileges (secret detail)')
        if self.q.startswith('DESCRIBE TABLE'):
            return [{'NAME': k, 'TYPE': v} for k, v in self.schema.items()]
        return []


class Drift(unittest.TestCase):
    def run_with(self, mutate=None, denied=False):
        import backend
        import config
        import findings
        import plan_census as PC
        import v3_local
        r = PC.intake(ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx')
        ir = r['ir']
        did = next(d for d, s in ir['field_scopes'].items() if s['caption'] == 'World Indicators')
        schema = {f['physical']: TYPES[f['datatype']] for f in ir['fields'].values()
                  if f['datasource_id'] == did and not f['is_calculation'] and f.get('datatype') in TYPES}
        if mutate:
            mutate(schema)
        v3_local._VALIDATED.clear()
        backend.set_session(Fake(schema, denied))
        try:
            with v3_local.preview_context(ir, {'World Indicators': 'DB.PUBLIC.WORLD_INDICATORS'}, snowflake=True):
                bound = 'World Indicators' in config.DATASOURCES
                codes = [(f['code'], f['message']) for f in findings.all_findings()]
        finally:
            backend.set_session(None)
        return bound, codes

    def test_matching_table_stays_bound(self):
        bound, codes = self.run_with()
        self.assertTrue(bound)
        self.assertNotIn('binding-drift', [c for c, _m in codes])

    def test_missing_column_changed_type_and_denied_access_are_detected(self):
        cases = (lambda s: s.pop('Birth Rate'),
                 lambda s: s.update({next(k for k, v in s.items() if v == 'FLOAT'): 'VARCHAR(10)'}),
                 None)
        for mutate, denied in zip(cases, (False, False, True)):
            bound, codes = self.run_with(mutate, denied)
            self.assertFalse(bound)
            msgs = [m for c, m in codes if c == 'binding-drift']
            self.assertTrue(msgs and 'World Indicators' in msgs[0], codes)
            self.assertNotIn('secret detail', ' '.join(msgs))


if __name__ == '__main__':
    unittest.main()

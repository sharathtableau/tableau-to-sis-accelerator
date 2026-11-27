"""Cortex as second calculation translator (lab): allowlist, classification, read-only comparison, scorecard."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_cortex_calc as CC

COLS = ['REVENUE', 'QUANTITY', 'CUSTOMER_ID', 'EVENT_DATE', 'SEGMENT']


class AllowlistTests(unittest.TestCase):
    def test_accepts_plain_expressions(self):
        for e in ['SUM(REVENUE)', 'SUM(REVENUE) / NULLIF(SUM(QUANTITY), 0)', "IFF(SEGMENT = 'SMB', 1, 0)",
                  'COUNT(DISTINCT CUSTOMER_ID)', "DATEDIFF('day', EVENT_DATE, CURRENT_DATE)",
                  'DATE_TRUNC(month, EVENT_DATE)', "CASE WHEN REVENUE > 10 THEN 'big' ELSE 'small' END"]:
            self.assertTrue(CC.validate_expression(e, COLS)[0], e)

    def test_rejects_anything_else(self):
        for e in ['SUM(REVENUE); DROP TABLE X', '(SELECT 1)', 'SUM(SECRET_COL)', 'REVENUE -- c', 'SYSTEM$WHOAMI()',
                  'SUM(REVENUE', "IDENTIFIER('X')", 'REVENUE /* x */']:
            self.assertFalse(CC.validate_expression(e, COLS)[0], e)

    def test_window_and_parameter_calcs_not_compared(self):
        # EN04: FIXED with dimensions (PARTITION BY) is compared now; ordered windows are still not
        self.assertIsNone(CC.classify('x', 'MIN(A) OVER (PARTITION BY B)')[0])
        self.assertEqual(CC.classify('x', 'SUM(A) OVER (ORDER BY B)')[0], 'Not comparable')
        self.assertEqual(CC.classify('x', 'SUM(A) * __PARAM_X__')[0], 'Not comparable')
        self.assertIsNone(CC.classify('x', 'SUM(A)')[0])


class DefaultsTests(unittest.TestCase):
    def test_constants_skipped_and_params_at_defaults(self):
        self.assertEqual(CC.classify('1', '1', '1')[0], 'Skipped')
        self.assertEqual(CC.classify('m', 'MIN(-0.5)', 'MIN(-0.5)')[0], 'Skipped')
        d = CC.param_defaults({'params': {'Select Date': '21', 'Category Parameter': '"AI Productivity"'}})
        sql = CC.with_defaults("SUM(IFF(X < __PARAM_SELECT_DATE__ AND C = __PARAM_CATEGORY_PARAMETER__, 1, 0))", d)
        self.assertNotIn('__PARAM', sql)
        self.assertIn("'AI Productivity'", sql)
        self.assertIsNone(CC.classify('p', sql)[0])


class _Session:
    def __init__(self, translations, values):
        CC.MEMORY.clear()                         # each test starts without remembered translations
        self.translations, self.values, self.queries = translations, values, []

    def sql(self, text, params=None):
        session = self
        if params is not None:
            class R:
                def collect(_self):
                    return [[json.dumps({'items': session.translations})]]
            return R()
        self.queries.append(text)

        class Row:
            def __init__(self, d):
                self.d = d

            def as_dict(self):
                return dict(self.d)

        class Q:
            columns = COLS

            def collect(_self):
                for key, d in session.values.items():
                    if key in text:
                        return [Row(d)]
                return [Row({'ROWS_COMPARED': 10, 'ROWS_DIFFERENT': 0})]
        return Q()


class RunTests(unittest.TestCase):
    def test_scorecard(self):
        used = [('Net', 'SUM(REVENUE)'), ('Avg', 'AVG(REVENUE)'), ('Flag', "IFF(SEGMENT = 'SMB', 1, 0)"),
                ('Bad', 'SUM(QUANTITY)'), ('Win', 'MIN(A) OVER (PARTITION BY B)'), ('Lost', 'SUM(REVENUE)')]
        formulas = {'Net': 'SUM([Revenue])', 'Avg': 'AVG([Revenue])', 'Flag': "IIF([Segment]='SMB',1,0)",
                    'Bad': 'SUM([Quantity])', 'Win': '{FIXED [B]: MIN([A])}'}
        translations = [
            {'name': 'Net', 'sql_expression': 'SUM(REVENUE)', 'level': 'aggregate', 'notes': ''},
            {'name': 'Avg', 'sql_expression': 'SUM(REVENUE)/COUNT(REVENUE)', 'level': 'aggregate', 'notes': ''},
            {'name': 'Flag', 'sql_expression': "IFF(SEGMENT = 'SMB', 1, 0)", 'level': 'row', 'notes': ''},
            {'name': 'Bad', 'sql_expression': 'SUM(QUANTITY); DROP TABLE T', 'level': 'aggregate', 'notes': ''}]
        values = {'(SUM(REVENUE)) AS ENGINE_VALUE, (SUM(REVENUE)) AS CORTEX': {'ENGINE_VALUE': 100.0, 'CORTEX_VALUE': 100.0},
                  '(AVG(REVENUE)) AS ENGINE_VALUE': {'ENGINE_VALUE': 10.0, 'CORTEX_VALUE': 9.5}}
        s = _Session(translations, values)
        out = CC.run_check(s, {}, used, formulas, 'DB.S.T', None)
        res = {r['Calculation']: r['Result'] for r in out['rows']}
        self.assertEqual(res, {'Net': 'Agree', 'Avg': 'Disagree', 'Flag': 'Agree', 'Bad': 'Rejected by allowlist',
                               'Lost': 'Not comparable'})
        # EN04: 'Win' (FIXED over columns A/B, not in this relation) is no longer pre-classified as not comparable;
        # with no bound column it is left out like any other calculation without columns
        self.assertEqual(out['score']['Compared'], 3)
        self.assertTrue(all(q.lstrip().upper().startswith('SELECT') for q in s.queries))
        self.assertFalse(any('DROP' in q for q in s.queries))


class DependencyTests(unittest.TestCase):
    def test_nested_calculations_are_sent_with_their_formulas(self):
        formulas = {'P.AOV': '[C.Total Revenue (copy)_1]/[C.Orders (copy)_2]',
                    'C.Total Revenue (copy)_1': 'SUM([Net Rev_3])', 'Net Rev_3': 'IF NOT [is_refunded] THEN [revenue] END',
                    'C.Orders (copy)_2': 'COUNTD([order_id])', 'Param calc': '[Parameters].[Parameter 2]'}
        deps = CC.dependencies(formulas['P.AOV'], formulas)
        self.assertEqual(set(deps), {'C.Total Revenue (copy)_1', 'C.Orders (copy)_2', 'Net Rev_3'})
        self.assertEqual(CC.dependencies(formulas['Param calc'], formulas), {})
        self.assertEqual(CC.dependencies('SUM([revenue])', formulas), {})

    def test_dependencies_reach_cortex_in_batches(self):
        used = [(f'C{i}', 'SUM(REVENUE)') for i in range(45)]
        formulas = {f'C{i}': f'SUM([Revenue]) + {i} * [Base_9]' for i in range(45)}
        formulas['Base_9'] = 'SUM([Quantity])'
        s = _Session([], {})
        prompts = []
        orig = s.sql

        def sql(text, params=None):
            if params is not None:
                prompts.append(json.dumps(params, default=str))
            return orig(text, params)
        s.sql = sql
        CC.run_check(s, {}, used, formulas, 'DB.S.T', None)
        self.assertEqual(len(prompts), 5)             # 45 calculations in batches of 10
        self.assertIn('referenced_calculations', prompts[0])
        self.assertIn('Base_9', prompts[0])


class BatchFailureTests(unittest.TestCase):
    def test_a_failed_batch_keeps_the_others(self):
        used = [(f'C{i}', 'SUM(REVENUE)') for i in range(15)]
        formulas = {f'C{i}': f'SUM([Revenue]) + {i}' for i in range(15)}
        good = [{'name': f'C{i}', 'sql_expression': 'SUM(REVENUE)', 'level': 'aggregate', 'notes': ''}
                for i in range(10)]
        s = _Session(good, {'(SUM(REVENUE)) AS ENGINE_VALUE': {'ENGINE_VALUE': 5.0, 'CORTEX_VALUE': 5.0}})
        orig, n = s.sql, {'calls': 0}

        def sql(text, params=None):
            if params is not None:
                n['calls'] += 1
                if n['calls'] >= 2:                  # second batch and its two retry halves: answer cut off

                    class R:
                        def collect(_self):
                            return [['{"items": [{"name": "C10", "sql_exp']]
                    return R()
            return orig(text, params)
        s.sql = sql
        out = CC.run_check(s, {}, used, formulas, 'DB.S.T', None)
        res = {r['Calculation']: (r['Result'], r['Evidence']) for r in out['rows']}
        self.assertEqual(n['calls'], 4)                # batch 1, batch 2, then batch 2 retried in two halves
        self.assertEqual(sum(1 for v in res.values() if v[0] == 'Agree'), 10)
        self.assertEqual(res['C12'][0], 'Not comparable')
        self.assertIn('could not be read', res['C12'][1])
        self.assertTrue(any('could not be read' in r['Reason'] for r in out['reasons']))


class TableFixedAndMemoryTests(unittest.TestCase):
    def test_table_level_fixed_form_is_allowed_and_nothing_else(self):
        ok = CC.validate_expression("IFF(EVENT_DATE <= (SELECT MAX(EVENT_DATE) FROM __TBL__), REVENUE, 0)", COLS)
        self.assertTrue(ok[0], ok)
        self.assertFalse(CC.validate_expression('(SELECT MAX(SECRET) FROM __TBL__)', COLS)[0])
        self.assertFalse(CC.validate_expression('(SELECT MAX(REVENUE) FROM OTHER_TABLE)', COLS)[0])
        self.assertFalse(CC.validate_expression('(SELECT REVENUE FROM __TBL__)', COLS)[0])

    def test_compare_substitutes_the_relation_on_both_sides(self):
        s = _Session([], {})
        CC.compare_one(s, 'DB.S.T', 'SUM(REVENUE)', 'SUM(IFF(EVENT_DATE <= (SELECT MAX(EVENT_DATE) FROM __TBL__), REVENUE, 0))', True)
        self.assertNotIn('__TBL__', s.queries[-1])
        self.assertIn('FROM DB.S.T __t', s.queries[-1])

    def test_rerun_reuses_translations(self):
        used = [('Net', 'SUM(REVENUE)')]
        formulas = {'Net': 'SUM([Revenue])'}
        tr = [{'name': 'Net', 'sql_expression': 'SUM(REVENUE)', 'level': 'aggregate', 'notes': ''}]
        s = _Session(tr, {'(SUM(REVENUE)) AS ENGINE_VALUE': {'ENGINE_VALUE': 1.0, 'CORTEX_VALUE': 1.0}})
        calls = {'n': 0}
        orig = s.sql

        def sql(text, params=None):
            if params is not None:
                calls['n'] += 1
            return orig(text, params)
        s.sql = sql
        a = CC.run_check(s, {}, used, formulas, 'DB.S.T', None)
        b = CC.run_check(s, {}, used, formulas, 'DB.S.T', None)    # same app process: no second Cortex call
        self.assertEqual(calls['n'], 1)
        self.assertEqual([r['Result'] for r in a['rows']], [r['Result'] for r in b['rows']])


class R15Tests(unittest.TestCase):
    def test_parameter_values_reach_cortex_not_names(self):
        f = "IF [Parameters].[Parameter 1] = 547 THEN [event_date] END + [Parameters].[Category Parameter]"
        out = CC.with_parameter_values(f, {'Parameter 1': '21', 'Category Parameter': '"AI Productivity"'})
        self.assertEqual(out, "IF 21 = 547 THEN [event_date] END + 'AI Productivity'")
        self.assertEqual(CC.with_parameter_values('[Parameters].[Unknown]', {}), '[Parameters].[Unknown]')
        used = [('C', 'SUM(REVENUE)')]
        formulas = {'C': 'SUM(IF [Parameters].[Parameter 1] > 0 THEN [Revenue] END)'}
        s = _Session([], {})
        prompts = []
        orig = s.sql

        def sql(text, params=None):
            if params is not None:
                prompts.append(json.dumps(params, default=str))
            return orig(text, params)
        s.sql = sql
        CC.run_check(s, {'result': {'calc_params': {'Parameter 1': '21', 'Select Date': '21'}}},
                     used, formulas, 'DB.S.T', None, {'params': {'Select Date': '21'}})
        self.assertIn('IF 21 > 0', prompts[0])
        self.assertNotIn('[Parameters].[Parameter 1]', prompts[0])

    def test_row_level_boolean_with_table_fixed_is_row_level(self):
        row = "CASE WHEN 21 = 547 THEN EVENT_DATE <= (SELECT MAX(EVENT_DATE) FROM __TBL__) ELSE DATEDIFF('day', EVENT_DATE, (SELECT MAX(EVENT_DATE) FROM __TBL__)) < 21 END"
        self.assertFalse(CC._is_aggregate(row))
        self.assertTrue(CC._is_aggregate('SUM(CASE WHEN ' + row + ' THEN REVENUE END)'))

    def test_cut_off_batch_is_retried_in_halves(self):
        used = [(f'C{i}', 'SUM(REVENUE)') for i in range(4)]
        formulas = {f'C{i}': f'SUM([Revenue]) + {i}' for i in range(4)}
        good = [{'name': f'C{i}', 'sql_expression': 'SUM(REVENUE)', 'level': 'aggregate', 'notes': ''} for i in range(4)]
        s = _Session(good, {'(SUM(REVENUE)) AS ENGINE_VALUE': {'ENGINE_VALUE': 5.0, 'CORTEX_VALUE': 5.0}})
        orig, n = s.sql, {'calls': 0}

        def sql(text, params=None):
            if params is not None:
                n['calls'] += 1
                if n['calls'] == 1:

                    class R:
                        def collect(_s):
                            return [['{"items": [{"name": "C0", "sql_exp']]
                    return R()
            return orig(text, params)
        s.sql = sql
        out = CC.run_check(s, {}, used, formulas, 'DB.S.T', None)
        self.assertEqual(n['calls'], 3)
        self.assertEqual(out['score']['Agree'], 4)


if __name__ == '__main__':
    unittest.main()

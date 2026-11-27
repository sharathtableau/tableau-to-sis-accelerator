"""EN04: the Cortex calculation cross-check compares FIXED with dimensions. Written before the code.
Cortex may write {FIXED [d1], [d2]: AGG([x])} as AGG(X) OVER (PARTITION BY D1, D2) -- that ONE window form only:
an aggregate call followed by OVER with PARTITION BY over known columns (or OVER ()), no ORDER BY, no frame, no
other window function. The engine translates FIXED the same way, so both are compared row by row in one read-only
query (windows are computed in a sub-select: Snowflake does not allow a window inside COUNT_IF). Engine FIXED inside
an outer aggregate (SUM({FIXED ...})) and table calculations stay not comparable, named."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_cortex_calc as CC  # noqa: E402

COLS = ['CUSTOMER_ID', 'ORDER_DATE', 'SALES', 'REGION']


class Allowlist(unittest.TestCase):
    def test_partition_form_allowed_only_when_asked(self):
        e = 'MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID)'
        self.assertTrue(CC.validate_expression(e, COLS, windows=True)[0])
        self.assertFalse(CC.validate_expression(e, COLS)[0])                 # default: as before
        self.assertTrue(CC.validate_expression('SUM(SALES) OVER (PARTITION BY CUSTOMER_ID, REGION) / '
                                               'SUM(SALES) OVER ()', COLS, windows=True)[0])

    def test_every_other_window_shape_is_refused(self):
        for e in ('SUM(SALES) OVER (PARTITION BY CUSTOMER_ID ORDER BY ORDER_DATE)',
                  'SUM(SALES) OVER (ORDER BY ORDER_DATE ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)',
                  'ROW_NUMBER() OVER (PARTITION BY CUSTOMER_ID)',
                  'LAG(SALES) OVER (PARTITION BY CUSTOMER_ID)',
                  'SUM(SALES) OVER (PARTITION BY SECRET_COL)',
                  'SUM(SALES) OVER w',
                  'SALES OVER (PARTITION BY CUSTOMER_ID)'):
            self.assertFalse(CC.validate_expression(e, COLS, windows=True)[0], e)

    def test_window_aggregates_are_row_level(self):
        self.assertFalse(CC._is_aggregate('MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID)'))
        self.assertFalse(CC._is_aggregate('DATEDIFF(day, MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID), ORDER_DATE)'))
        self.assertTrue(CC._is_aggregate('SUM(SALES)'))
        self.assertTrue(CC._is_aggregate('AVG(MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID))'))


class Classify(unittest.TestCase):
    def test_row_level_fixed_is_now_comparable(self):
        self.assertEqual(CC.classify('First order', 'MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID)', 'x'),
                         (None, None))

    def test_fixed_inside_an_aggregate_and_table_calcs_stay_named(self):
        st, why = CC.classify('Avg first', 'AVG(MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID))', 'x')
        self.assertEqual(st, 'Not comparable')
        self.assertIn('aggregate over FIXED', why)
        st, why = CC.classify('Running', 'SUM(SALES) OVER (ORDER BY ORDER_DATE)', 'x')
        self.assertEqual(st, 'Not comparable')


class Compare(unittest.TestCase):
    def test_windows_are_compared_in_a_sub_select(self):
        seen = {}

        class Row(dict):
            def as_dict(self):
                return dict(self)

        class S:
            def sql(self, q):
                seen['q'] = q

                class R:
                    def collect(self):
                        return [Row(ROWS_COMPARED=10, ROWS_DIFFERENT=0)]
                return R()
        res, ev = CC.compare_one(S(), 'DB.S.T', 'MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID)',
                                 'MIN(ORDER_DATE) OVER (PARTITION BY CUSTOMER_ID)', False)
        self.assertEqual(res, 'Agree')
        q = seen['q']
        self.assertIn('FROM (SELECT', q)
        self.assertLess(q.index('COUNT_IF'), q.index('OVER'))                 # the window is inside the sub-select
        self.assertNotIn('COUNT_IF(NOT EQUAL_NULL((MIN', q)

    def test_prompt_states_the_one_form(self):
        import inspect
        src = inspect.getsource(CC._translate_cortex)
        self.assertIn('OVER (PARTITION BY', src)


if __name__ == '__main__':
    unittest.main()

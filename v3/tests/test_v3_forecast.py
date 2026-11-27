"""FC: Tableau forecasts. Tableau's own exponential-smoothing model is not published and cannot be reproduced to the
cent, so the app adds an APPROXIMATED forecast: Holt-Winters (additive / multiplicative season, optional trend) fitted
per partition in plain numpy, driven by the sheet's <forecast-specification> (model, ignore-last, range, fill).
Estimate rows carry Forecast Indicator = 'Estimate'; the result is never 'Verified'. Written before the code."""
from pathlib import Path
import json
import math
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_forecast as F  # noqa: E402

XML = ("<worksheet name='Forecast'><table><view><forecast-specification auto-forecast-agg='true' "
       "band-confidence-level='95.000000' enabled='true' fill-type='fill-zero' ignore-last='1' model-type='custom' "
       "range-type='auto' season-type='ets-additive' show-prediction-bands='true' trend-type='ets-additive' />"
       "</view></table></worksheet>")


def seasonal(n, level=100.0, slope=2.0, amp=30.0):
    return [level + slope * t + amp * math.sin(2 * math.pi * (t % 12) / 12) for t in range(n)]


class Spec(unittest.TestCase):
    def test_reads_the_specification(self):
        s = F.spec_from_xml(XML)
        self.assertEqual((s['trend'], s['season'], s['ignore_last'], s['range'], s['fill_zero']),
                         ('add', 'add', 1, None, True))

    def test_disabled_or_absent(self):
        self.assertIsNone(F.spec_from_xml("<worksheet><forecast-specification enabled='false' /></worksheet>"))
        self.assertIsNone(F.spec_from_xml('<worksheet />'))

    def test_auto_range_is_one_season(self):
        self.assertEqual(F.horizon({'range': None}, 'month_trunc'), 12)
        self.assertEqual(F.horizon({'range': None}, 'quarter_trunc'), 4)
        self.assertEqual(F.horizon({'range': 6}, 'month_trunc'), 6)


class Model(unittest.TestCase):
    def test_recovers_a_clean_seasonal_trend(self):
        y = seasonal(48)
        got = F.holt_winters(y, 12, 'add', 'add', 12)
        want = seasonal(60)[48:]
        self.assertLess(max(abs(a - b) for a, b in zip(got, want)), 3.0)

    def test_no_season_no_trend_is_flat(self):
        got = F.holt_winters([5.0] * 10, 1, None, None, 3)
        self.assertTrue(all(abs(v - 5.0) < 1e-9 for v in got))

    def test_short_series_falls_back_to_no_season(self):
        got = F.holt_winters(seasonal(10), 12, 'add', 'add', 2)       # fewer than two seasons
        self.assertEqual(len(got), 2)
        self.assertTrue(all(math.isfinite(v) for v in got))

    def test_pure_numpy(self):
        src = (ROOT / 'v3_forecast.py').read_text()
        imports = set(re.findall(r'^\s*(?:import|from) (\w+)', src, re.M))
        self.assertLessEqual(imports, {'re', 'numpy', 'pandas', 'v3_sheet_plan'})


class Rows(unittest.TestCase):
    PLAN = {'fields': [{'caption': 'Order Date', 'kind': 'dim', 'channel': 'cols', 'date_part': 'month_trunc',
                        'discrete': False},
                       {'caption': 'Segment', 'kind': 'dim', 'channel': 'rows', 'date_part': 'none', 'discrete': True},
                       {'caption': 'Sales', 'kind': 'measure', 'agg': 'sum', 'channel': 'rows', 'date_part': 'none'}],
            'forecast': F.spec_from_xml(XML)}

    def frame(self):
        import pandas as pd
        months = pd.date_range('2021-01-01', periods=48, freq='MS')
        rows = [(m, seg, v * k) for seg, k in (('A', 1.0), ('B', 2.0)) for m, v in zip(months, seasonal(48))]
        import v3_sheet_plan as P
        f = self.PLAN['fields']
        return pd.DataFrame(rows, columns=[P.col_name(f[0]), P.col_name(f[1]), P.col_name(f[2], True)])

    def test_estimates_per_partition_with_ignore_last(self):
        import v3_sheet_plan as P
        out = F.add_estimates(self.frame(), self.PLAN)
        ind = out['Forecast Indicator']
        self.assertEqual(int((ind == 'Actual').sum()), 2 * 47)          # the last period becomes an estimate
        self.assertEqual(int((ind == 'Estimate').sum()), 2 * 13)        # it, plus one season ahead
        date = P.col_name(self.PLAN['fields'][0])
        est = out[ind == 'Estimate']
        self.assertEqual(str(est[date].min())[:10], '2024-12-01')
        self.assertEqual(str(est[date].max())[:10], '2025-12-01')
        seg = P.col_name(self.PLAN['fields'][1])
        self.assertEqual(sorted(est[seg].unique()), ['A', 'B'])

    def test_estimates_are_drawn_dashed(self):
        import altair
        plan = dict(self.PLAN, mark='line', fields=[dict(f, id=f'f{i}') for i, f in enumerate(self.PLAN['fields'])])
        out = F.add_estimates(self.frame(), plan)
        import v3_sheet_plan as P

        class Eng:
            alt = altair
            value_labels = staticmethod(lambda cap: {})
        spec = P.build_chart(plan, out, Eng).to_dict()
        self.assertIn("'strokeDash': {'field': 'Forecast_Indicator'", str(spec))

    def test_no_time_axis_no_forecast(self):
        plan = dict(self.PLAN, fields=self.PLAN['fields'][1:])
        import pandas as pd
        df = pd.DataFrame({'a': [1]})
        self.assertIs(F.add_estimates(df, plan), df)


class AgainstTableau(unittest.TestCase):
    """Superstore 'Forecast' capture: report the error of the approximation; never claim equality."""
    def test_error_is_measured_and_bounded(self):
        cap = Path('/tmp/claude-0/ba/sc/Superstore_Tableau2024_3.capture.json')
        if not cap.exists():
            self.skipTest('capture not present')
        rows = json.loads(cap.read_text())['captures']['Superstore_Tableau2024_3/Forecast']['sheets']['Forecast']['rows']
        num = lambda s: float(re.sub(r'[$,]', '', s))  # noqa: E731
        act, est = {}, {}
        for ind, m, seg, v in rows:
            (act if ind == 'Actual' else est).setdefault(seg, []).append((m, num(v)))
        errs = []
        for seg in act:
            y = [v for _m, v in act[seg]]                    # Tableau already left out the ignored last month
            got = F.holt_winters(y, 12, 'add', 'add', len(est[seg]))
            errs += [abs(g - w) / w for g, (_m, w) in zip(got, est[seg])]
        mape = sum(errs) / len(errs)
        print(f'\nforecast approximation vs Tableau: mean abs error {mape:.1%}')
        self.assertLess(mape, 0.25)


if __name__ == '__main__':
    unittest.main()

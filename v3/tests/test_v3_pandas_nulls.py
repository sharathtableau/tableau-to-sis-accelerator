"""pandas-version safety (6 Nov): pandas 3 reads text as the string dtype, where a missing value is pd.NA / NaN and
str() of it is '<NA>'. Two product paths assumed pandas 2 object columns:
  * date detection: '<NA>' was not skipped, so a text date column with a missing value stayed text and every date
    chart on it failed (date_trunc on VARCHAR);
  * query results: a NULL member came back as NaN instead of None, which the engine's None checks miss (a missing
    member could be shown as the text 'nan' where Tableau shows Null).
Written BEFORE the fix (tests first); must hold on pandas 2.3 (deployed) and 3.x."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend  # noqa: E402


class DateDetection(unittest.TestCase):
    def test_text_dates_with_missing_values_become_dates(self):
        for dtype in ('string', object):
            df = backend._normalize_columns(pd.DataFrame({'Event': pd.Series(['2024-01-05', None, '2024-02-01'],
                                                                                dtype=dtype)}))
            self.assertTrue(pd.api.types.is_datetime64_any_dtype(df['EVENT']), dtype)
            self.assertTrue(df['EVENT'].isna().tolist() == [False, True, False], dtype)


class ResultNulls(unittest.TestCase):
    def test_text_nulls_are_none_and_numbers_stay_numeric(self):
        df = pd.DataFrame({'M': pd.Series([None, 'A'], dtype='string'), 'V': [1.5, None]})
        out = backend.result_frame(df)
        self.assertEqual(out['M'].tolist(), [None, 'A'])
        self.assertTrue(pd.api.types.is_float_dtype(out['V']))
        self.assertTrue(pd.isna(out['V'].iloc[1]))

    def test_run_sql_uses_it_on_both_paths(self):
        src = (ROOT / 'backend.py').read_text(encoding='utf-8').split('def run_sql')[1].split('\ndef ')[0]
        self.assertEqual(src.count('result_frame('), 2)


if __name__ == '__main__':
    unittest.main()

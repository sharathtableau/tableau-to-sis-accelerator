"""E03 lossless ingestion (6 Nov corpus census): pandas 3 reads CSV text as the dedicated string dtype, and the loader
only coerced `object` columns, so thousands-separated numbers ("74,814") stayed text (12 corpus sheets failed; any
pandas upgrade would do the same in the app). Worse, v3_data_load.shape() turned a declared real / integer value it
could not read into NULL without a word. Written BEFORE the fix (tests first)."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import backend  # noqa: E402
import v3_data_load as L  # noqa: E402


def frame(values, dtype):
    return pd.DataFrame({'Region': pd.Series(['East'] * len(values), dtype=dtype),
                         'Sales': pd.Series(values, dtype=dtype)})


class Normalize(unittest.TestCase):
    def test_thousands_separated_numbers_in_either_text_dtype(self):
        for dtype in ('string', object):
            df = backend._normalize_columns(frame(['74,814', '188,411', '1,234.5'], dtype))
            self.assertTrue(pd.api.types.is_numeric_dtype(df['SALES']), dtype)
            self.assertEqual(list(df['SALES']), [74814, 188411, 1234.5])
            self.assertFalse(pd.api.types.is_numeric_dtype(df['REGION']), dtype)


class Shape(unittest.TestCase):
    FIELDS = [{'physical': 'Sales', 'datatype': 'real'}, {'physical': 'Region', 'datatype': 'string'}]

    def test_declared_real_from_string_dtype_is_loaded_as_numbers(self):
        df, _fixes, _kept, _absent = L.shape(frame(['74,814', '188,411'], 'string'), self.FIELDS)
        self.assertEqual(list(df['SALES']), [74814.0, 188411.0])

    def test_unreadable_declared_number_is_a_problem_not_a_silent_null(self):
        for kind in ('real', 'integer'):
            fields = [{'physical': 'Sales', 'datatype': kind}]
            with self.assertRaises(ValueError) as cm:
                L.shape(frame(['12', 'n/a value', '14'], 'string'), fields)
            self.assertIn('SALES', str(cm.exception))
            self.assertIn('not a number', str(cm.exception))

    def test_real_nulls_stay_null(self):
        df, *_ = L.shape(pd.DataFrame({'Sales': pd.Series(['1', None, '3'], dtype='string')}),
                         [{'physical': 'Sales', 'datatype': 'real'}])
        self.assertEqual(df['SALES'].isna().tolist(), [False, True, False])


if __name__ == '__main__':
    unittest.main()

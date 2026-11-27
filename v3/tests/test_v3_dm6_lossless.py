"""DM6 (E03): lossless ingestion. Written BEFORE the fix. Text files were read with pandas type inference (and
'mostly numeric' text coerced), so a string code '001' or ZIP '02134' became 1 / 2134 and a literal 'NA' became NULL
-- in the local preview AND the Snowflake load. Now text files are read as text (only an empty cell is NULL) and
the workbook's DECLARED type decides: string stays verbatim, numbers and dates are converted (thousands separators
read as Tableau does), undeclared columns keep the previous inference."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CSV = 'Code,ZIP,Note,Amount,Order Date,Other\n001,02134,NA,"1,200",2024-01-05,7\n010,10001,,5,2024-02-06,8\n'
TYPES = {'CODE': 'string', 'ZIP': 'string', 'NOTE': 'string', 'AMOUNT': 'real', 'ORDER_DATE': 'date'}


class Preview(unittest.TestCase):
    def test_declared_types_are_kept(self):
        import backend
        path = Path(tempfile.mkdtemp()) / 'x.csv'
        path.write_text(CSV, encoding='utf-8')
        df = backend._normalize_columns(backend._read_source_file(str(path), text=True), types=TYPES)
        self.assertEqual(df['CODE'].tolist(), ['001', '010'])
        self.assertEqual(df['ZIP'].tolist(), ['02134', '10001'])
        self.assertEqual(df['NOTE'].tolist(), ['NA', None])
        self.assertEqual(df['AMOUNT'].tolist(), [1200.0, 5.0])
        self.assertEqual(str(df['ORDER_DATE'].dtype)[:8], 'datetime')
        self.assertEqual(df['OTHER'].tolist(), [7, 8])                 # undeclared: inferred as before

    def test_without_types_behaviour_is_unchanged(self):
        import backend
        path = Path(tempfile.mkdtemp()) / 'x.csv'
        path.write_text(CSV, encoding='utf-8')
        df = backend._normalize_columns(backend._read_source_file(str(path)))
        self.assertEqual(df['CODE'].tolist(), [1, 10])


class Load(unittest.TestCase):
    def test_snowflake_load_shape_keeps_codes(self):
        import v3_data_load as DL
        df = DL._read(CSV.encode(), 'x.csv')
        fields = [{'physical': 'Code', 'datatype': 'string'}, {'physical': 'ZIP', 'datatype': 'string'},
                  {'physical': 'Note', 'datatype': 'string'}, {'physical': 'Amount', 'datatype': 'real'}]
        out, _fixes, _kept, _absent = DL.shape(df, fields)
        self.assertEqual(out['CODE'].tolist(), ['001', '010'])
        self.assertEqual(out['ZIP'].tolist(), ['02134', '10001'])
        self.assertEqual(out['NOTE'].tolist(), ['NA', None])
        self.assertEqual(out['AMOUNT'].tolist(), [1200.0, 5.0])


if __name__ == '__main__':
    unittest.main()

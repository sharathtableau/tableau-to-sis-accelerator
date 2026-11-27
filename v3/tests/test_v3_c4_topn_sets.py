"""C4: Top-N sets (<group> with groupfilter function='end' over an order expression -- Fil_Test 'Top Customers by
Profit', top [Parameter 1] by SUM(Profit)). Enumerated and conditional sets already resolved; Top-N sets were
dropped. Written BEFORE the fix: In = the N members ranked by the expression over the whole table (bottom N for
end='bottom'); the count may be a parameter. Expected from pandas."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tableau_parser as TP  # noqa: E402

GROUP = """<workbook><datasources><datasource name='ds'><group name='[Top Customers by Profit]' name-style='unqualified'>
<groupfilter count='{count}' end='{end}' function='end' units='records'><groupfilter direction='DESC'
expression='SUM([Profit])' function='order'><groupfilter function='level-members' level='[Customer Name]' />
</groupfilter></groupfilter></group></datasource></datasources></workbook>"""

DF = pd.DataFrame({'CUSTOMER_NAME': ['a', 'a', 'b', 'c', 'd', 'd'], 'PROFIT': [5.0, 5.0, 3.0, 20.0, -1.0, 2.0]})


def members(count, end='top', params=None, value=None):
    sets = TP.extract_sets(ET.fromstring(GROUP.format(count=count, end=end)), {}, params or {})
    sql = sets['Top Customers by Profit']['sql']
    if value is not None:
        sql = __import__('re').sub(r'__PARAM_[A-Z0-9_]+__', str(value), sql)
    con = duckdb.connect()
    con.register('src_rows', DF)
    con.execute('CREATE TABLE T AS SELECT * FROM src_rows')
    rows = con.execute(f"SELECT DISTINCT CUSTOMER_NAME, {sql.replace('__TBL__', 'T')} AS S FROM T").fetchall()
    return sorted(c for c, s in rows if s == 'In'), sets['Top Customers by Profit']


class TopNSets(unittest.TestCase):
    def test_top_and_bottom_n(self):
        totals = DF.groupby('CUSTOMER_NAME')['PROFIT'].sum().sort_values(ascending=False)
        self.assertEqual(members('2')[0], sorted(totals.index[:2]))            # c (20), a (10)
        self.assertEqual(members('1', 'bottom')[0], [totals.index[-1]])         # d (1)

    def test_parameter_count_and_row_level(self):
        got, entry = members('[Parameters].[Parameter 1]', params={'Parameter 1': 3}, value=3)
        self.assertEqual(got, ['a', 'b', 'c'])
        self.assertFalse(entry['agg_ready'])


if __name__ == '__main__':
    unittest.main()

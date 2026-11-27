"""DM4: per-table query for a multi-fact model (Orders -> Product <- Targets), on DuckDB with a minimal engine.
Written with the fact-role rules: the other fact's measures from its OWN rows, broken down by its own or the shared
dimension's columns; a cross-fact breakdown or a cross-fact filter refuses. Expected values from pandas."""
from pathlib import Path
import re
import sys
import unittest

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_relationship_query as RQ  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

ORDERS = pd.DataFrame({'PID': ['P1', 'P1', 'P2', 'P3'], 'NAME': ['Pen', 'Pen', 'Ink', None],
                       'REGION': ['E', 'W', 'E', 'W'], 'SALES': [1.0, 2.0, 4.0, 8.0]})       # flattened Orders+Product
TARGETS = pd.DataFrame({'TPID': ['P1', 'P1', 'P2'], 'TARGET': [10.0, 5.0, 20.0], 'QTR': ['Q1', 'Q2', 'Q1']})


class Engine:
    IR = {'fields': {'f::t': {'related': {'source': 'DS [Targets]', 'column': 'TARGET'}},
                     'f::q': {'related': {'source': 'DS [Targets]', 'column': 'QTR'}}},
          'field_scopes': {'ds': {'related_tables': {'DS [Targets]': {
              'table': 'Targets', 'parent_key': 'PID', 'key': 'TPID', 'role': 'fact',
              'shared_columns': ['PID', 'NAME']}}}}}

    def __init__(self):
        self.con = duckdb.connect()
        self.con.register('src_orders', ORDERS)
        self.con.execute('CREATE TABLE T AS SELECT * FROM src_orders')
        self.con.register('src_targets', TARGETS)
        self.con.execute('CREATE TABLE R AS SELECT * FROM src_targets')

    def tbl(self, s):
        return 'R' if s.get('datasource') == 'DS [Targets]' else 'T'

    def rdim(self, caption):
        return {'Product Name': 'NAME', 'Region': 'REGION', 'PID': 'PID'}[caption]

    def resolve_measure(self, T, sheet, ref):
        return {'sql': 'SUM(SALES)'}

    def sub_params(self, sql):
        return sql

    def q(self, sql):
        return self.con.execute(sql).fetchdf()


def f(caption, kind, agg='none', fid=None):
    return {'caption': caption, 'kind': kind, 'agg': agg, 'channel': 'rows' if kind == 'dim' else 'text',
            'date_part': 'none', 'discrete': kind == 'dim', 'field_id': fid}


def run(fields, where=''):
    e = Engine()
    plan = {'fields': fields, 'measure_values': [], 'aggregate': True}
    related = RQ.related_fields(plan, e)
    sql, cols = RQ.compile_relationship(plan, {'datasource_id': 'ds', 'name': 's'}, e, where, related, P)
    return e.q(sql), cols


class MultiFactQuery(unittest.TestCase):
    def test_both_facts_by_the_shared_dimension(self):
        df, _ = run([f('Product Name', 'dim'), f('Sales', 'measure', 'sum'), f('Target', 'measure', 'sum', 'f::t')])
        got = {r.D0: (r.M0, r.M1) for r in df.itertuples()}
        names = ORDERS[['PID', 'NAME']].drop_duplicates()
        want_t = TARGETS.merge(names, left_on='TPID', right_on='PID', how='left').groupby('NAME')['TARGET'].sum()
        want_s = ORDERS.groupby('NAME', dropna=False)['SALES'].sum()
        self.assertEqual(got['Pen'], (want_s['Pen'], want_t['Pen']))      # 3 and 15: Targets never multiplied
        self.assertEqual(got['Ink'], (want_s['Ink'], want_t['Ink']))
        self.assertEqual(float(df['M1'].sum()), TARGETS['TARGET'].sum())

    def test_other_fact_by_its_own_dimension(self):
        df, _ = run([f('Quarter', 'dim', fid='f::q'), f('Target', 'measure', 'sum', 'f::t')])
        self.assertEqual(dict(zip(df['D0'], df['M0'])), TARGETS.groupby('QTR')['TARGET'].sum().to_dict())

    def test_cross_fact_breakdowns_and_filters_refuse(self):
        with self.assertRaisesRegex(RQ.RelationshipQueryError, 'another fact'):
            run([f('Quarter', 'dim', fid='f::q'), f('Sales', 'measure', 'sum')])
        with self.assertRaisesRegex(RQ.RelationshipQueryError, 'own or shared'):
            run([f('Region', 'dim'), f('Target', 'measure', 'sum', 'f::t')])
        with self.assertRaisesRegex(RQ.RelationshipQueryError, 'filter across'):
            run([f('Product Name', 'dim'), f('Target', 'measure', 'sum', 'f::t')], where="WHERE REGION = 'E'")


if __name__ == '__main__':
    unittest.main()

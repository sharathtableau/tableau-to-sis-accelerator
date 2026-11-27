"""CF02 verified queries. Written before the code.
A sheet proven equal to Tableau becomes a verified query of the run's Semantic View (AI_VERIFIED_QUERIES), so Ask the
data answers that question with SQL already proven. Rules:
  * Only sheets whose validation status is a Match with Tableau; only shapes code can state exactly: raw dimensions
    (no date part, no calculation) and SUM of raw numeric columns of the fact table, no filters, no table calcs,
    no Measure Names / Values. Anything else is skipped with a named reason.
  * The question and the SQL are written by code (never by the model).
  * Before it is offered, code proves it: a read-only SEMANTIC_VIEW() query over the same dimensions and metrics must
    give Tableau's displayed values (display rounding allowed) for every mark of the sheet.
  * Adding them recreates the view with the clause; a person approves; if Snowflake rejects the clause the view is
    recreated as it was and a status code is returned (no Snowflake error text)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_verified_queries as VQ  # noqa: E402
import v3_semantic as SV  # noqa: E402

SEM = {'name': 'WBR_DB.PUBLIC.SALES_SEMANTIC', 'caption': 'Sales',
       'tables': [{'alias': 'ORDERS', 'table': 'DB.S.ORDERS', 'caption': 'Orders', 'role': 'fact', 'primary_key': None}],
       'relationships': [], 'facts': [],
       'dimensions': [{'table': 'ORDERS', 'name': 'REGION', 'column': 'REGION', 'caption': 'Region',
                       'datatype': 'string', 'numeric': False},
                      {'table': 'ORDERS', 'name': 'SALES', 'column': 'SALES', 'caption': 'Sales', 'datatype': 'real',
                       'numeric': True},
                      {'table': 'ORDERS', 'name': 'ORDER_DATE', 'column': 'ORDER_DATE', 'caption': 'Order Date',
                       'datatype': 'date', 'numeric': False}],
       'metrics': [{'table': 'ORDERS', 'name': 'TOTAL_SALES', 'expr': 'SUM(ORDERS.SALES)', 'caption': 'Total Sales',
                    'source': 'generated'}], 'skipped': []}


def fld(caption, kind, channel, agg='none', date_part='none', **kw):
    return dict({'id': caption, 'caption': caption, 'kind': kind, 'channel': channel, 'agg': agg,
                 'date_part': date_part, 'discrete': kind == 'dim', 'calculation': None}, **kw)


def plan(*fields):
    return {'mark': 'bar', 'aggregate': True, 'fields': list(fields)}


GOOD = plan(fld('Region', 'dim', 'rows'), fld('Sales', 'measure', 'cols', agg='sum'))
DETAIL = [{'Key': 'East', 'Measure': 'Sales', 'Tableau': '$678,781', 'App': '678781.24', 'Match': True},
          {'Key': 'West', 'Measure': 'Sales', 'Tableau': '$725,458', 'App': '725457.82', 'Match': True}]


class Sess:
    def __init__(self, rows=None, fail_on=None):
        self.sqls, self.rows, self.fail_on = [], rows or [], fail_on

    def sql(self, text, params=None):
        self.sqls.append(text)
        outer = self

        class Q:
            def collect(self):
                if outer.fail_on and outer.fail_on in text:
                    raise RuntimeError('invalid')
                return outer.rows
        return Q()


class Candidates(unittest.TestCase):
    def test_code_writes_question_and_sql(self):
        c = VQ.candidate('Sales by Region', {}, GOOD, SEM)
        self.assertEqual(c['state'], 'CANDIDATE')
        self.assertEqual(c['question'], 'What is the total Sales by Region?')
        self.assertEqual(c['sql'], 'SELECT REGION, SUM(SALES) AS TOTAL_SALES FROM __ORDERS GROUP BY REGION ORDER BY REGION')
        self.assertIn('SEMANTIC_VIEW(WBR_DB.PUBLIC.SALES_SEMANTIC DIMENSIONS ORDERS.REGION METRICS ORDERS.TOTAL_SALES)',
                      c['proof_sql'])

    def test_shapes_code_cannot_state_are_skipped_with_a_reason(self):
        cases = [
            (plan(fld('Region', 'dim', 'rows'), fld('Sales', 'measure', 'cols', agg='avg')), 'aggregation'),
            (plan(fld('Order Date', 'dim', 'cols', date_part='year'), fld('Sales', 'measure', 'rows', agg='sum')),
             'date part'),
            (plan(fld('Region', 'dim', 'rows'), fld('Sales', 'measure', 'cols', agg='sum', table_calc={'x': 1})),
             'table calculation'),
            (plan(fld('Segment', 'dim', 'rows'), fld('Sales', 'measure', 'cols', agg='sum')), 'not in the Semantic View'),
            (plan(fld('Measure Names', 'measure_names', 'rows'), fld('Sales', 'measure', 'cols', agg='sum')),
             'Measure Names'),
            (plan(fld('Region', 'dim', 'rows'), fld('Profit Ratio', 'measure', 'cols', agg='sum', calculation={'sql': 'x'})),
             'calculation'),
        ]
        for p, why in cases:
            c = VQ.candidate('S', {}, p, SEM)
            self.assertEqual(c['state'], 'SKIPPED', why)
            self.assertIn(why, c['reason'])
        self.assertIn('filter', VQ.candidate('S', {'filters': [{'field': 'Region'}]}, GOOD, SEM)['reason'])

    def test_only_sheets_matching_tableau(self):
        run = {'sheet_reference': {'results': {'A': ({'Status': 'Match'}, []), 'B': ({'Status': 'Difference found'}, []),
                                               'C': ({'Status': 'Match (2 values)'}, [])}}}
        self.assertEqual(VQ.proven_sheets(run), ['A', 'C'])


class Proof(unittest.TestCase):
    def test_semantic_view_values_equal_tableau(self):
        c = VQ.candidate('Sales by Region', {}, GOOD, SEM)
        ok = VQ.prove(Sess([('East', 678781.24), ('West', 725457.82)]), c, DETAIL)
        self.assertEqual(ok['state'], 'VERIFIED')
        bad = VQ.prove(Sess([('East', 678781.24), ('West', 1.0)]), c, DETAIL)
        self.assertEqual(bad['state'], 'NOT_MATCHED')
        short = VQ.prove(Sess([('East', 678781.24)]), c, DETAIL)
        self.assertEqual(short['state'], 'NOT_MATCHED')
        err = VQ.prove(Sess(fail_on='SEMANTIC_VIEW'), c, DETAIL)
        self.assertEqual(err['state'], 'NOT_COMPARABLE')
        self.assertNotIn('invalid', str(err))                      # codes only

    def test_percent_values_are_not_compared(self):
        c = VQ.candidate('Sales by Region', {}, GOOD, SEM)
        d = [dict(x, Tableau='12%') for x in DETAIL]
        self.assertEqual(VQ.prove(Sess([('East', 0.12), ('West', 0.12)]), c, d)['state'], 'NOT_COMPARABLE')


class Clause(unittest.TestCase):
    def test_ddl_has_the_clause_after_comment(self):
        c = dict(VQ.candidate('Sales by Region', {}, GOOD, SEM), state='VERIFIED')
        text = SV.ddl(dict(SEM, verified_queries=[VQ.entry(c, verified_at=1700000000)]))
        self.assertIn('AI_VERIFIED_QUERIES (', text)
        self.assertLess(text.index('COMMENT = \'Generated'), text.index('AI_VERIFIED_QUERIES'))
        self.assertIn("QUESTION 'What is the total Sales by Region?'", text)
        self.assertIn('VERIFIED_AT 1700000000', text)
        self.assertIn("SQL 'SELECT REGION, SUM(SALES)", text)
        self.assertNotIn('AI_VERIFIED_QUERIES', SV.ddl(SEM))

    def test_quotes_are_escaped(self):
        c = dict(VQ.candidate('Sales by Region', {}, GOOD, SEM), question="O'Brien's sales?", state='VERIFIED')
        self.assertIn("O''Brien''s", VQ.clause([VQ.entry(c, verified_at=1)]))

    def test_unverified_entries_are_refused(self):
        with self.assertRaises(ValueError):
            VQ.entry(VQ.candidate('Sales by Region', {}, GOOD, SEM))


class Apply(unittest.TestCase):
    def entries(self):
        return [VQ.entry(dict(VQ.candidate('Sales by Region', {}, GOOD, SEM), state='VERIFIED'), verified_at=1)]

    def test_needs_a_person(self):
        with self.assertRaises(ValueError):
            VQ.apply(Sess(), SEM, self.entries(), approved_by='')

    def test_recreates_with_the_clause(self):
        s = Sess()
        status, sem = VQ.apply(s, SEM, self.entries(), approved_by='Owner')
        self.assertEqual(status, 'ADDED')
        self.assertIn('AI_VERIFIED_QUERIES', s.sqls[-1])
        self.assertEqual(len(sem['verified_queries']), 1)

    def test_rejected_clause_restores_the_view(self):
        s = Sess(fail_on='AI_VERIFIED_QUERIES')
        status, sem = VQ.apply(s, SEM, self.entries(), approved_by='Owner')
        self.assertEqual(status, 'VQ_REJECTED_BY_SNOWFLAKE')
        self.assertNotIn('AI_VERIFIED_QUERIES', s.sqls[-1])       # recreated as it was
        self.assertFalse(sem.get('verified_queries'))

    def test_console_offers_it(self):
        self.assertIn('v3_verified_queries', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

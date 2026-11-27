"""Cortex gaps from the 6 Nov audit, built tests first:
  * AI_REDACT on CX6 evidence rows before AI_AGG / AI_CLASSIFY (identity categories only; same statement)
  * Cortex Search over the CX3 verified-translation library (SEARCH_PREVIEW), results used only when they map back to
    an entry this library already holds as VERIFIED; fallback AI_EMBED similarity, then word overlap."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cx2_safety as S  # noqa: E402
import cx3_calc as C  # noqa: E402
import cx6_review as R  # noqa: E402


class FakeSession:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def sql(self, text, params=None):
        self.seen.append((text, list(params or [])))
        reply = self.replies.pop(0) if self.replies else []

        class Res:
            def collect(self_inner):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return Res()


class Redact(unittest.TestCase):
    def test_categories_are_identity_only(self):
        self.assertTrue(set(S.REDACT_CATEGORIES) <= {'NAME', 'EMAIL', 'PHONE_NUMBER', 'ADDRESS', 'NATIONAL_ID', 'PASSPORT',
                                                     'TAX_IDENTIFIER', 'PAYMENT_CARD_DATA', 'DRIVERS_LICENSE', 'IP_ADDRESS'})
        for analytic in ('DATE_OF_BIRTH', 'AGE', 'GENDER'):
            self.assertNotIn(analytic, S.REDACT_CATEGORIES)
        self.assertIn("AI_REDACT(column1, ['NAME', ", S.redact_sql('column1'))

    def test_cx6_rows_are_redacted_in_the_same_statement(self):
        items = [{'id': 'E1', 'step': 'Sheet check', 'subject': 'Customers', 'status': 'Not matched',
                  'detail': 'Jane Doe: 10 vs 12', 'risk': 'high'}]
        s = FakeSession([[('summary [E1]',)]])
        R.summarize(s, items)
        self.assertIn('AI_AGG(' + S.redact_sql('column1'), s.seen[0][0])
        s = FakeSession([[('E1', json.dumps({'labels': ['data mismatch']}))]])
        R.classify(s, items)
        self.assertIn('AI_CLASSIFY(' + S.redact_sql('column2'), s.seen[0][0])


LIB = [{'formula': '{FIXED [Order ID]: SUM([Profit])}', 'status': 'VERIFIED', 'caption': 'Order Profit',
        'plan': {'kind': 'fixed', 'dimensions': ['ORDER_ID'], 'aggregate': 'SUM', 'expression': 'PROFIT'}},
       {'formula': 'RUNNING_SUM(SUM([Sales]))', 'status': 'VERIFIED', 'caption': 'Running',
        'plan': {'kind': 'window', 'function': 'RUNNING_SUM', 'expression': 'SUM(SALES)'}},
       {'formula': '{FIXED [Region]: AVG([Sales])}', 'status': 'PROPOSED', 'caption': 'x',
        'plan': {'kind': 'fixed', 'dimensions': ['REGION'], 'aggregate': 'AVG', 'expression': 'SALES'}}]


class Search(unittest.TestCase):
    def test_setup_sql(self):
        text = '\n'.join(C.library_search_setup_sql('POWERHOUSE', 'WBR_OWNER'))
        self.assertIn('CREATE CORTEX SEARCH SERVICE IF NOT EXISTS WBR_DB.CX_CALC.CX3_LIBRARY_SEARCH', text)
        self.assertIn('ON FORMULA', text)
        self.assertIn('CHANGE_TRACKING = TRUE', text)
        self.assertIn("WHERE STATUS = 'VERIFIED'", text)
        self.assertNotIn('OR REPLACE', text.upper())

    def test_search_results_map_back_to_verified_entries_only(self):
        reply = json.dumps({'results': [{'FORMULA': 'RUNNING_SUM(SUM([Sales]))'},
                                        {'FORMULA': '{FIXED [Region]: AVG([Sales])}'},     # not VERIFIED here
                                        {'FORMULA': 'made up by someone'}]})
        s = FakeSession([[(reply,)]])
        got = C.Library(LIB).similar("RUNNING_SUM(SUM([Profit]))", k=3, session=s)
        self.assertEqual([e['caption'] for e in got], ['Running'])
        sql = s.seen[0][0]
        self.assertIn("SNOWFLAKE.CORTEX.SEARCH_PREVIEW('WBR_DB.CX_CALC.CX3_LIBRARY_SEARCH', '", sql)

    def test_the_query_literal_is_escaped(self):
        s = FakeSession([[(json.dumps({'results': []}),)], [], []])
        C.Library(LIB).similar("IF [Name] = 'O\\'Brien' THEN 1 END", session=s)
        lit = s.seen[0][0].split("SEARCH_PREVIEW('WBR_DB.CX_CALC.CX3_LIBRARY_SEARCH', '")[1].rsplit("')", 1)[0]
        self.assertNotIn("\\'", lit.replace("\\\\", ''))                     # every quote doubled, backslashes doubled
        self.assertEqual(json.loads(lit.replace("''", "'").replace('\\\\', '\\'))['limit'], 3)

    def test_fallback_when_the_service_is_missing(self):
        s = FakeSession([RuntimeError('does not exist'), [(0.9,)], [(0.1,)]])
        got = C.Library(LIB).similar('{FIXED [Customer]: SUM([Profit])}', k=1, session=s)
        self.assertEqual(got[0]['caption'], 'Order Profit')
        self.assertIn('AI_EMBED', s.seen[1][0])
        self.assertEqual([e['caption'] for e in C.Library(LIB).similar('{FIXED [Customer]: SUM([Profit])}', k=1)],
                         ['Order Profit'])


if __name__ == '__main__':
    unittest.main()

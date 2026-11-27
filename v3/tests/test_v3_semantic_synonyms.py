"""Live finding 31 Oct (release 22): Snowflake rejected Semantic Views over versioned loaded tables with
'Duplicate synonym ORDERS_V2'. Synonyms must be unique across the view and never repeat an alias or a name."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_semantic as SV


def synonyms(ddl):
    out = []
    for group in re.findall(r"WITH SYNONYMS = \(([^)]*)\)", ddl):
        out += [s.strip().strip("'").upper() for s in re.findall(r"'(?:[^']|'')*'", group)]
    return out


def identifiers(sem):
    return {t['alias'].upper() for t in sem['tables']} | {e['name'].upper() for e in sem['dimensions'] + sem['facts'] + sem['metrics']}


class SynonymTests(unittest.TestCase):
    def test_versioned_single_table_has_no_duplicate_synonym(self):
        spec = {'version': SV.SINGLE_VERSION, 'table': 'WBR_DB.PIPELINE_DEMO.SALES_TARGET_V3',
                'columns': [{'column': 'SALES_TARGET', 'datatype': 'integer'}, {'column': 'CATEGORY', 'datatype': 'string'},
                            {'column': 'ORDER_DATE', 'datatype': 'date'}]}
        sem = SV.single_model(spec)
        ddl = SV.ddl(sem)
        syn = synonyms(ddl)
        self.assertEqual(len(syn), len(set(syn)), syn)
        self.assertFalse(set(syn) & identifiers(sem), set(syn) & identifiers(sem))
        self.assertNotIn("'SALES_TARGET_V3'", ddl)
        self.assertIn('sales_target_v3 AS WBR_DB.PIPELINE_DEMO.SALES_TARGET_V3', ddl)

    def test_repeated_captions_and_cortex_synonyms_are_kept_once(self):
        sem = {'name': 'DB.S.V', 'caption': None, 'tables': [{'alias': 'orders', 'table': 'DB.S.ORDERS_V2', 'caption': 'ORDERS_V2',
                                                              'role': 'fact', 'primary_key': None}],
               'relationships': [], 'facts': [],
               'dimensions': [{'table': 'orders', 'name': 'SALES', 'column': 'SALES', 'caption': 'Sales', 'synonyms': ['Revenue', 'orders_v2']},
                              {'table': 'orders', 'name': 'REGION', 'column': 'REGION', 'caption': 'Region', 'synonyms': ['Revenue', 'Area']}],
               'metrics': [{'table': 'orders', 'name': 'TOTAL_SALES', 'expr': 'SUM(orders.SALES)', 'caption': 'Total Sales',
                            'source': 'generated', 'synonyms': ['Revenue', 'sales']}]}
        syn = synonyms(SV.ddl(sem))
        self.assertEqual(len(syn), len(set(syn)), syn)
        self.assertEqual(syn.count('REVENUE'), 1)
        self.assertIn('AREA', syn)
        self.assertNotIn('SALES', syn)              # repeats the dimension name SALES
        self.assertIn('ORDERS_V2', syn)             # the table caption is not an identifier here, so it stays


if __name__ == '__main__':
    unittest.main()

"""Release 23: duplicate-table finder (read-only; writes a script, runs nothing)."""
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('find_duplicate_tables', ROOT / 'tools' / 'find_duplicate_tables.py')
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

SHA = lambda c: c * 64


class Sess:
    def __init__(self):
        self.tables = {
            'SALES_TARGET': (4603, '', 'h1'),
            'SALES_TARGET_V3': (4603, f'v3load src_sha256={SHA("a")} files=x.hyper', 'h1'),
            'SALES_TARGET_V4': (4603, f'v3load src_sha256={SHA("b")} files=x.hyper', 'h1'),
            'SALES_COMMISSION': (41, '', 'h2'),
            'SALES_COMMISSION_V2': (41, f'v3load src_sha256={SHA("c")}', 'h3'),     # same shape, other data
            'ORDERS': (37907, '', 'h4'),
            'ORDERS_V2': (10194, f'v3load data_sha256={SHA("d")} src_sha256={SHA("e")}', 'h5'),
        }
        self.sql_seen = []

    def sql(self, q, params=None):
        self.sql_seen.append(q)
        s = self

        class R:
            def collect(self_):
                if 'INFORMATION_SCHEMA.TABLES' in q:
                    return [{'TABLE_NAME': n, 'ROW_COUNT': r, 'COMMENT': c, 'CREATED': i}
                            for i, (n, (r, c, _h)) in enumerate(s.tables.items())]
                if 'INFORMATION_SCHEMA.COLUMNS' in q:
                    return [{'TABLE_NAME': n, 'COLUMN_NAME': col, 'DATA_TYPE': 'TEXT'} for n in s.tables
                            for col in (['A', 'B'] if not n.startswith('ORDERS') else ['O'])]
                if 'SEMANTIC_VIEW_EVENTS' in q:
                    return [{'VIEW_NAME': 'DB.S.SALES_TARGET_V3_SVLAB_T1', 'WORKBOOK_NAME': 'R1',
                             'TABLES': json.dumps(['WBR_DB.PIPELINE_DEMO.SALES_TARGET_V3'])},
                            {'VIEW_NAME': 'DB.S.SALES_TARGET_V4_SVLAB_T2', 'WORKBOOK_NAME': 'R1-V2',
                             'TABLES': json.dumps(['WBR_DB.PIPELINE_DEMO.SALES_TARGET_V4'])}]
                if 'DATA_LOADS' in q:
                    return []
                m = re.search(r'FROM WBR_DB\.PIPELINE_DEMO\.(\w+)$', q)
                if q.startswith('SELECT HASH_AGG') and m:
                    return [{'H': s.tables[m.group(1)][2]}]
                raise AssertionError(q)
        return R()


class DuplicateTests(unittest.TestCase):
    def test_script_keeps_one_v3_copy_and_never_drops_outside_tables(self):
        sess = Sess()
        text, summary = D.script(sess)
        self.assertEqual(len(summary), 1)
        group, keep, drop = summary[0]
        self.assertEqual(group, ['SALES_TARGET', 'SALES_TARGET_V3', 'SALES_TARGET_V4'])
        self.assertEqual((keep, drop), ('SALES_TARGET_V3', ['SALES_TARGET_V4']))
        self.assertIn('DROP TABLE IF EXISTS WBR_DB.PIPELINE_DEMO.SALES_TARGET_V4;', text)
        self.assertIn('DROP SEMANTIC VIEW IF EXISTS DB.S.SALES_TARGET_V4_SVLAB_T2;', text)
        self.assertNotIn('DROP TABLE IF EXISTS WBR_DB.PIPELINE_DEMO.SALES_TARGET;', text)
        self.assertNotIn('SALES_TARGET_V3_SVLAB_T1;', text)
        self.assertIn(f'src_sha256={SHA("b")}', re.search(r"COMMENT ON TABLE WBR_DB.PIPELINE_DEMO.SALES_TARGET_V3 IS '(.*)';", text).group(1))
        self.assertIn('SALES_TARGET: made outside this accelerator (not dropped)', text)
        self.assertNotIn('SALES_COMMISSION_V2', ' '.join(' '.join(g) for g, _k, _d in summary))   # different data

    def test_read_only(self):
        sess = Sess()
        D.script(sess)
        self.assertFalse([q for q in sess.sql_seen if re.match(r'\s*(DROP|COMMENT|ALTER|CREATE|INSERT|UPDATE|DELETE)', q)])


if __name__ == '__main__':
    unittest.main()

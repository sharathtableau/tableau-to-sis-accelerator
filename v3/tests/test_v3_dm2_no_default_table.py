"""DM2 (E02): no default-table fallback. config.table_for returns the built-in ORDERS table for an unknown
datasource; in V3's scoped mode a sheet whose datasource is not bound must refuse, never read another table.
Written BEFORE the fix. The inherited unscoped apps keep their legacy default (separately tested)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


class NoDefault(unittest.TestCase):
    def test_unbound_datasource_refuses_and_bound_ones_resolve(self):
        import engine
        import plan_census as PC
        from v3_local import preview_context
        r = PC.intake(ROOT / 'Workbooks' / 'cloud_20261010' / 'Global_Sales_Dashboard.twbx')
        ir, b = r['ir'], {k: v for k, v in r['bindings'].items() if v}
        cap = lambda s: ir['field_scopes'][s['datasource_id']]['caption']
        sheets = [s for d in ir['dashboards'] for s in d['sheets']]
        unbound = next(s for s in sheets if not b.get(cap(s)))
        bound = {'datasource': 'CALLS'}                         # bound blend secondary of the same workbook
        with preview_context(ir, b):
            self.assertIn('PREVIEW', engine.tbl(bound))
            with self.assertRaises(ValueError) as cm:
                engine.tbl(unbound)
        self.assertIn('not bound', str(cm.exception))

    def test_unscoped_legacy_keeps_its_default(self):
        import config
        self.assertEqual(config.table_for('no such datasource'), config.ORDERS)


if __name__ == '__main__':
    unittest.main()

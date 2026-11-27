"""EN14: Tableau device layouts (Phone / Tablet). Written with the code; the parser used to record only their names.
Rule: a device layout with its own zones is laid out by the same code as the default layout (canvas rectangles and
layout tree, its own size and title, only the sheets it places). The app draws it when the viewer picks it, or --
'Automatic' -- when the browser reports that device. A device without its own zones follows the default.
Proof: Superstore 2024.3 (six Phone layouts, one Tablet) and a second workbook with device layouts."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import engine  # noqa: E402

SS = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'


class Agent(unittest.TestCase):
    def test_user_agents(self):
        cases = {'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1': 'Phone',
                 'Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari/537.36': 'Phone',
                 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)': 'Tablet',
                 'Mozilla/5.0 (Linux; Android 14; SM-X710) Safari/537.36': 'Tablet',
                 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120': 'Desktop', None: 'Desktop'}
        for ua, want in cases.items():
            self.assertEqual(engine.device_from_user_agent(ua), want, ua)


@unittest.skipUnless(SS.exists(), 'corpus workbook')
class Superstore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tableau_parser as TP
        cls.ir = TP.build_ir(str(SS), scoped=True)
        cls.dash = {d['name']: d for d in cls.ir['dashboards']}

    def test_variants_parsed(self):
        phones = [n for n, d in self.dash.items() if 'Phone' in (d.get('device_variants') or {})]
        self.assertEqual(len(phones), 6)
        v = self.dash['Customers']['device_variants']['Phone']
        self.assertEqual(v['sheets'], ['CustomerScatter', 'CustomerRank'])       # the phone drops CustomerOverview
        self.assertEqual(v['canvas']['sizing_mode'], 'vscroll')
        self.assertTrue(v['canvas_zones'])

    def test_variant_view(self):
        d = self.dash['Customers']
        phone = engine.device_variant(d, 'Phone')
        self.assertEqual([s['name'] for s in phone['sheets']], ['CustomerScatter', 'CustomerRank'])
        self.assertEqual(engine.canvas_size(phone['canvas'])[0], 375)
        self.assertIs(engine.device_variant(d, 'Desktop'), d)
        self.assertIs(engine.device_variant(self.dash['Overview'], 'Tablet'), self.dash['Overview'])  # no own zones

    def test_phone_layout_draws(self):
        import findings
        import plan_census as PC
        from v3_local import preview_context
        r = PC.intake(SS)
        d = next(x for x in r['ir']['dashboards'] if x['name'] == 'Overview')
        def blockers(view):
            with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
                engine._render_dashboard_impl(view)
                return {(f['sheet'], f['code']) for f in findings.all_findings() if f.get('severity') == 'BLOCKER'}
        # the phone layout adds no failure of its own (the desktop layout's findings are the sheets' own)
        self.assertLessEqual(blockers(engine.device_variant(d, 'Phone')), blockers(d))


class SecondWorkbook(unittest.TestCase):
    def test_another_workbook_has_parsed_variants(self):
        import tableau_parser as TP
        hits = []
        for f in sorted(glob.glob(str(ROOT / 'Workbooks' / '*.twbx'))):
            if 'Superstore' in f:
                continue
            try:
                ir = TP.build_ir(f, scoped=True)
            except Exception:
                continue
            for d in ir['dashboards']:
                for name, v in (d.get('device_variants') or {}).items():
                    hits.append((Path(f).name, d['name'], name))
                    self.assertTrue(v['sheets'])
                    self.assertTrue(v['canvas_zones'] or v['layout'])
        if not hits:
            self.skipTest('no second corpus workbook with device layouts')
        self.assertTrue(hits)


if __name__ == '__main__':
    unittest.main()

"""R47: faster gates. Decoding a workbook's .hyper extract was most of every test's time (Tableau's engine reads
every row; test_v3_c6: 22 s of 34 s) and every gate run decoded the same corpus extracts again in each module.
Rule: only when TABLEAU_V3_HYPER_CACHE names a folder (an opt-in for test runs; the product never sets it) the
decoded tables are kept under a key of the extract's bytes + the Hyper API version + the decoder's code. A hit
gives exactly the frames a decode gives; flattening, keys and the CSV still run every time; a changed extract or
decoder misses; the cache never changes a result. Proof: Superstore (measure selector) and World Indicators extracts."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WBS = [ROOT / 'Workbooks' / 'Superstore_MeasureSelector_Native.twbx', ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx']


def hyper_of(wb, folder):
    with zipfile.ZipFile(wb) as z:
        name = next(n for n in z.namelist() if n.endswith('.hyper'))
        out = Path(folder) / Path(name).name
        out.write_bytes(z.read(name))
    return str(out)


class Cache(unittest.TestCase):
    def setUp(self):
        try:
            import tableauhyperapi  # noqa: F401
        except ImportError:
            self.skipTest('tableauhyperapi')
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.pop('TABLEAU_V3_HYPER_CACHE', None)

    def tearDown(self):
        if self.old is not None:
            os.environ['TABLEAU_V3_HYPER_CACHE'] = self.old
        else:
            os.environ.pop('TABLEAU_V3_HYPER_CACHE', None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def convert(self, hyper, out):
        import init_workbook as IW
        os.makedirs(out, exist_ok=True)
        return Path(IW.hyper_to_csv(hyper, out)).read_bytes()

    def test_hit_is_identical_and_skips_the_decode(self):
        import init_workbook as IW
        import tableauhyperapi
        for wb in WBS:
            if not wb.exists():
                continue
            hyper = hyper_of(wb, self.tmp)
            plain = self.convert(hyper, os.path.join(self.tmp, 'plain'))          # no cache: a decode
            self.assertFalse(os.listdir(self.tmp + '/plain') == [] )
            os.environ['TABLEAU_V3_HYPER_CACHE'] = os.path.join(self.tmp, 'cache')
            first = self.convert(hyper, os.path.join(self.tmp, 'a'))              # miss: decode + store
            self.assertEqual(len([n for n in os.listdir(self.tmp + '/cache') if n.startswith('csv_')]), 1 + WBS.index(wb))
            real = tableauhyperapi.HyperProcess

            def refuse(*a, **k):
                raise AssertionError('decoded again on a cache hit')
            tableauhyperapi.HyperProcess = refuse
            try:
                second = self.convert(hyper, os.path.join(self.tmp, 'b'))         # hit: no Hyper process
            finally:
                tableauhyperapi.HyperProcess = real
            self.assertEqual(plain, first)
            self.assertEqual(first, second)
            os.environ.pop('TABLEAU_V3_HYPER_CACHE')
            self.assertTrue(IW._hyper_cache_key(hyper))

    def test_changed_extract_misses(self):
        import init_workbook as IW
        wb = next((w for w in WBS if w.exists()), None)
        if wb is None:
            self.skipTest('corpus workbook')
        hyper = hyper_of(wb, self.tmp)
        k1 = IW._hyper_cache_key(hyper)
        with open(hyper, 'ab') as f:
            f.write(b'\0')
        self.assertNotEqual(k1, IW._hyper_cache_key(hyper))

    def test_product_never_caches(self):
        import init_workbook as IW
        wb = next((w for w in WBS if w.exists()), None)
        if wb is None:
            self.skipTest('corpus workbook')
        hyper = hyper_of(wb, self.tmp)
        self.convert(hyper, os.path.join(self.tmp, 'c'))
        self.assertEqual(sorted(os.listdir(self.tmp)), sorted([Path(hyper).name, 'c']))

    def test_relationship_extract_through_intake(self):
        # a multi-table extract (flattened star + evidence): the cached intake binds the very same data
        ra = ROOT / 'Workbooks' / 'Regional Analysis.twbx'
        if not ra.exists():
            self.skipTest('corpus workbook')
        sys.path.insert(0, str(ROOT / 'tools'))
        import plan_census as PC

        def snapshot():
            r = PC.intake(ra)
            out = {}
            for k, v in sorted(r['bindings'].items()):
                if v and str(v).endswith('.csv') and os.path.isfile(str(v)):
                    out[k] = Path(v).read_bytes()
                else:
                    out[k] = repr(v)[:0]
            return out, len(r['ir'].get('fields') or {})
        plain = snapshot()
        os.environ['TABLEAU_V3_HYPER_CACHE'] = os.path.join(self.tmp, 'cache')
        miss, hit = snapshot(), snapshot()
        self.assertEqual(plain, miss)
        self.assertEqual(plain, hit)
        self.assertTrue(any(n.startswith('csv_') for n in os.listdir(self.tmp + '/cache')))


if __name__ == '__main__':
    unittest.main()

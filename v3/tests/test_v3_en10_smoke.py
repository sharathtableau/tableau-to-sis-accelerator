"""EN10: a smoke test after every deploy opens every console page of the deployed files headless; written before
release/smoke.py. The release script runs it as its last step, after verify (hash check)."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'release'))
import smoke as S  # noqa: E402


class Smoke(unittest.TestCase):
    def test_every_page_of_the_console_loads(self):
        rows = S.smoke(ROOT)
        self.assertEqual([r['page'] for r in rows], S.PAGES)
        self.assertEqual([r for r in rows if not r['ok']], [])

    def test_a_broken_app_fails_the_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'v3_console.py').write_text('import streamlit as st\nraise RuntimeError("boom")\n')
            rows = S.smoke(tmp, pages=['Overview'])
            self.assertFalse(rows[0]['ok'])
            rows = S.smoke(tmp, main='missing.py', pages=['Overview'])
            self.assertIn('entry file missing', rows[0]['problems'][0])

    def test_release_script_runs_it(self):
        cmd = (ROOT / 'release' / 'release_v3.cmd').read_text(encoding='utf-8')
        self.assertIn('release\\smoke.py', cmd)


if __name__ == '__main__':
    unittest.main()

"""F7 (release28): a Tableau quick filter drawn in a short zone (title line + box) must not spill below its zone.
On Streamlit 1.52.2 the multiselect tag chip (28px) and its hidden search input (21px) overflowed a 41px zone by 7px.
Also: the <Full Name>/<User Name> insert must not touch st.experimental_user when st.user exists (1.52 shows a
deprecation box for it). Written before the fix (tests first)."""
from pathlib import Path
import re
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402


def _decls(css, selector_part):
    return [body for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css) if selector_part in sel]


class ControlSqueeze(unittest.TestCase):
    def test_multiselect_chip_and_input_fit_the_box(self):
        css = engine._control_squeeze_css('v3cz56', 33, True)          # 41px zone minus borders, titled
        box = 21
        tag = _decls(css, '[data-baseweb=tag]')
        self.assertTrue(any(f'height:{box - 4}px' in b for b in tag), css)
        inp = _decls(css, '.stMultiSelect input')
        self.assertTrue(any(f'max-height:{box - 4}px' in b and 'min-height:0' in b for b in inp), css)
        self.assertTrue(any('flex-wrap:nowrap' in b for b in _decls(css, '.stMultiSelect')), css)
        self.assertTrue(all('st-key-v3cz56' in sel for sel, _ in re.findall(r'([^{}]+)\{([^{}]*)\}', css)))

    def test_untitled_zone_uses_the_full_height_and_has_a_floor(self):
        self.assertIn('height:30px', engine._control_squeeze_css('k', 30, False))
        self.assertIn('height:12px', engine._control_squeeze_css('k', 14, True))


class CanvasFilterCss(unittest.TestCase):
    def test_every_canvas_multiselect_sizes_its_search_input(self):
        # owner's --sf run 6 Nov: a 41px zone is above the squeeze threshold; the 26px hidden input still spilled
        src = (ROOT / 'engine.py').read_text(encoding='utf-8')
        self.assertIn('div[class*=st-key-v3cz] .stMultiSelect input{height:16px !important;', src)
        self.assertIn('.stMultiSelect div:has(>input){height:auto;min-height:0;', src)


class ViewerName(unittest.TestCase):
    def test_st_user_is_used_and_experimental_user_never_touched(self):
        touched = []

        class FakeSt(types.SimpleNamespace):
            def __getattr__(self, name):
                raise AttributeError(name)

        fake = FakeSt(user={'name': 'Ada Lovelace', 'user_name': 'ADA'})
        type(fake).experimental_user = property(lambda s: touched.append(1) or {'name': 'X'})
        with mock.patch.object(engine, 'st', fake):
            self.assertEqual(engine._viewer_name('Full Name'), 'Ada Lovelace')
            self.assertEqual(engine._viewer_name('User Name'), 'ADA')
            fake.user = {}
            self.assertEqual(engine._viewer_name('Full Name'), '')
        self.assertEqual(touched, [])

    def test_old_streamlit_without_st_user_falls_back(self):
        fake = types.SimpleNamespace(experimental_user={'user_name': 'OLD'})
        with mock.patch.object(engine, 'st', fake):
            self.assertEqual(engine._viewer_name('User Name'), 'OLD')


if __name__ == '__main__':
    unittest.main()

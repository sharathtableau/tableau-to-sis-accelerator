"""F12 (CX2 fix queue): a Tableau text button is drawn as authored -- its own background-color, else NO fill (Tableau
draws an unstyled text button transparent, as on the E-Commerce navigation hot-spots) -- in every state, including a
button whose target dashboard is not migrated (disabled in the app). Written before the fix (tests first)."""
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402


def _css_for(node, nav=None):
    engine._ZONE_CSS.clear()
    calls = []
    with mock.patch.object(engine, 'DASH_NAV', nav), \
            mock.patch.object(engine.st, 'button', side_effect=lambda *a, **k: calls.append(k)):
        engine._render_layout_impl(dict(node, h=2000, _zone_px=0.01), {}, '')
    css = ''.join(engine._ZONE_CSS)
    engine._ZONE_CSS.clear()
    return css, calls


def _rule(css, selector_part):
    """The declarations of every rule whose selector list contains selector_part."""
    return [body for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css) if selector_part in sel]


class TextButtons(unittest.TestCase):
    def test_unstyled_button_is_transparent_even_when_disabled(self):
        # E-Commerce shape: caption '.', light font colour, no background-color, target dashboard not migrated
        node = {'nav': 'Product', 'caption': '.', 'color': '#e6e6e6', 'fontsize': '8', 'images': []}
        css, calls = _css_for(node, nav=('dash_key', ['Dashboard']))
        self.assertTrue(calls and calls[0]['disabled'])
        disabled = _rule(css, ':disabled')
        self.assertTrue(disabled, css)
        self.assertTrue(any('background:transparent' in b and 'opacity:1' in b for b in disabled), css)

    def test_authored_fill_on_a_second_workbook_shape(self):
        # Regional Analysis shape: background-color #021e4c, white Tableau Bold 12pt caption, target migrated
        node = {'nav': 'View2', 'caption': 'View2', 'color': '#ffffff', 'fontsize': '12', 'fill': '#021e4c',
                'images': []}
        css, calls = _css_for(node, nav=('dash_key', ['View1', 'View2']))
        self.assertFalse(calls[0]['disabled'])
        rules = _rule(css, 'button')
        self.assertTrue(any('background:#021e4c' in b for b in rules), css)
        self.assertTrue(any('color:#ffffff' in b for b in rules), css)
        self.assertTrue(any('background:#021e4c' in b for b in _rule(css, ':disabled')), css)


if __name__ == '__main__':
    unittest.main()

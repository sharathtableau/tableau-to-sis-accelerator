"""Lab: architecture board can be expanded and downloaded."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ArchViewTests(unittest.TestCase):
    def test_download_is_a_standalone_svg_with_background(self):
        import v3_console as VC
        from v3_platform_board import build_svg_fragment
        svg, w, _h = build_svg_fragment()
        data = VC._architecture_svg_file(svg).decode('utf-8')
        self.assertTrue(data.startswith('<svg'))
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', data)
        self.assertIn('fill="#FFFFFF"', data.split('>', 2)[1] + '>')
        self.assertEqual(data.count('<svg'), 1)

    def test_overview_renders_with_expand_button(self):
        from streamlit.testing.v1 import AppTest
        root = repr(str(Path(__file__).resolve().parents[1]))
        app = AppTest.from_string(
            f"import sys; sys.path.insert(0, {root})\nimport v3_console as VC\nVC.page_overview()\n").run(timeout=60)
        self.assertFalse(app.exception)
        self.assertTrue(any('Expand diagram' in b.label for b in app.button))


if __name__ == '__main__':
    unittest.main()

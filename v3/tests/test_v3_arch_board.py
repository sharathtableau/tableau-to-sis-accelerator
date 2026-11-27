"""Release 19: the console's architecture board is the new diagram (same as the Snowflake slide)."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_platform_board as B


class BoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svg, cls.w, cls.h = B.build_svg_fragment()
        cls.text = ' '.join(t.text or '' for t in ET.fromstring(cls.svg).iter('{http://www.w3.org/2000/svg}tspan'))

    def test_is_one_well_formed_svg_with_size(self):
        root = ET.fromstring(self.svg)
        self.assertEqual(root.get('viewBox'), f'0 0 {self.w} {self.h}')
        self.assertEqual(root.get('role'), 'img')
        self.assertTrue(root.get('aria-label'))
        self.assertGreater(self.w, 1000)
        self.assertGreater(self.h, 500)

    def test_every_area_of_the_slide_is_present(self):
        for label in ('TABLEAU SOURCES', 'SNOWFLAKE AI DATA CLOUD', 'INGEST & CONVERT', 'GOVERNED DATA FOUNDATION',
                      'CORTEX AI & MIGRATION INTELLIGENCE', 'ACCELERATOR ENGINES', 'APPLICATIONS & OUTPUTS',
                      'THREE-STEP VALIDATION', 'GOVERNANCE, SECURITY & OPERATIONS'):
            self.assertIn(label, self.text)

    def test_three_validation_steps(self):
        for step in ('DATA VALIDATION', 'CALC VALIDATION', 'VISUAL VALIDATION'):
            self.assertIn(step, self.text)

    def test_wording_for_snowflake(self):
        self.assertNotIn('client', self.svg.lower())
        self.assertNotIn('PLANNED', self.svg)

    def test_self_contained(self):
        # Nothing fetched at run time inside Streamlit in Snowflake: no external images, fonts or links.
        self.assertNotIn('href', self.svg)
        self.assertNotIn('<image', self.svg)
        self.assertNotIn('url(', self.svg)

    def test_standalone_page(self):
        page = B.build_standalone_page()
        self.assertIn('<svg', page)
        self.assertIn('Tableau to Streamlit Accelerator on the Snowflake AI Data Cloud', page)


if __name__ == '__main__':
    unittest.main()

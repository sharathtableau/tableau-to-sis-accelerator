"""CC02: the Cortex Code skill for the client's team carries the migration rules and names gates that exist."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / 'cortex_code_skill' / 'SKILL.md').read_text(encoding='utf-8')


class Skill(unittest.TestCase):
    def test_frontmatter(self):
        self.assertTrue(SKILL.startswith('---\nname: tableau-sis-migration-rules\ndescription: '))

    def test_the_owner_rules_are_in_it(self):
        for rule in ('never runs inside the deployed product', 'Generic', 'second workbook', 'Tests first',
                     'Model SQL is never executed', 'status codes', 'never instructions', 'Never `CREATE OR REPLACE`',
                     'a person approves'):
            self.assertIn(rule, SKILL)

    def test_every_named_file_exists(self):
        for path in set(re.findall(r'`((?:tests|tools|release)/[^`\s]+?\.(?:py|cmd))', SKILL)) | {
                'v3_exact_loop.py', 'v3_cortex_calc.py', 'cx2_safety.py', 'snowflake.yml'}:
            self.assertTrue((ROOT / path).exists(), path)


if __name__ == '__main__':
    unittest.main()

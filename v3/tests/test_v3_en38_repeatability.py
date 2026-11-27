"""EN38: Cortex answer repeatability. Written before the code.
Every console Cortex call keeps the hash of its prompt and its structured answer (never the prompt). When the same
prompt reaches the same model again, code compares the answers: identical = repeatable; different = the difference is
recorded (purpose, model, how many distinct answers). Shown on the Cortex AI page; it never changes a verdict."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_ai as A  # noqa: E402


def e(purpose, sha, resp, model='m', status='Completed'):
    return {'Step': 'Parsing', 'Purpose': purpose, 'Model': model, 'Status': status, '_prompt_sha256': sha,
            '_response': resp}


class Repeatability(unittest.TestCase):
    def test_same_prompt_same_answer_is_repeatable(self):
        run = {'ai_log': [e('CF05 first translation', 'a', {'x': 1}), e('CF05 first translation', 'a', {'x': 1})]}
        rows = A.repeatability(run)
        self.assertEqual(rows, [{'Step': 'Parsing', 'Purpose': 'CF05 first translation', 'Model': 'm', 'Asked': 2,
                                 'Distinct answers': 1, 'Repeatable': 'Yes'}])

    def test_a_different_answer_is_recorded(self):
        run = {'ai_log': [e('P', 'a', {'x': 1}), e('P', 'a', {'x': 2}), e('P', 'a', {'x': 1})]}
        r = A.repeatability(run)[0]
        self.assertEqual((r['Asked'], r['Distinct answers'], r['Repeatable']), (3, 2, 'No'))

    def test_key_order_does_not_count_and_singles_and_failures_are_left_out(self):
        run = {'ai_log': [e('P', 'a', {'x': 1, 'y': 2}), e('P', 'a', {'y': 2, 'x': 1}), e('Q', 'b', 'only once'),
                          e('R', 'c', None, status='Failed'), e('R', 'c', None, status='Failed'),
                          e('S', 'd', {'x': 1}, model='m1'), e('S', 'd', {'x': 1}, model='m2')]}
        rows = A.repeatability(run)
        self.assertEqual([r['Purpose'] for r in rows], ['P'])
        self.assertEqual(rows[0]['Repeatable'], 'Yes')

    def test_shown_on_the_cortex_page(self):
        self.assertIn('repeatability(', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()

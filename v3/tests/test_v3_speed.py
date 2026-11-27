"""Lab speed-ups: the read-only model check runs once per binding (TTL), the hosted query cache is kept."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_local as VL


class ValidateOnceTests(unittest.TestCase):
    def setUp(self):
        VL._VALIDATED.clear()

    def test_same_binding_checked_once_new_binding_checked_again(self):
        plan = {'root': 'E', 'tables': {'E': {'table': 'DB.S.E'}}}
        with patch('v3_model_binding.validate') as validate:
            VL._validate_once(object(), plan, {})
            VL._validate_once(object(), plan, {})
            self.assertEqual(validate.call_count, 1)
            VL._validate_once(object(), dict(plan, semantic_view='DB.S.X_CORTEX'), {})
            self.assertEqual(validate.call_count, 2)

    def test_check_repeats_after_ttl_and_failure_is_not_remembered(self):
        plan = {'root': 'E'}
        with patch('v3_model_binding.validate', side_effect=ValueError('Fact row count changed')):
            with self.assertRaises(ValueError):
                VL._validate_once(object(), plan, {})
        with patch('v3_model_binding.validate') as validate, patch('time.time', return_value=10_000.0):
            VL._validate_once(object(), plan, {})
            VL._VALIDATED[next(iter(VL._VALIDATED))] = 10_000.0 - VL.VALIDATION_TTL - 1
            VL._validate_once(object(), plan, {})
            self.assertEqual(validate.call_count, 2)


class QueryCacheTests(unittest.TestCase):
    def _run(self, snowflake):
        import engine
        q_exec, cols = MagicMock(), MagicMock()
        session = object() if snowflake else None
        with patch.object(engine, '_q_exec', q_exec), patch.object(engine, 'table_columns', cols), \
             patch('backend._active_session', return_value=session), patch.object(engine, 'configure'), \
             patch('pipeline.configure_datasources'):
            with VL.preview_context({'datasources': []}, {}, snowflake=snowflake):
                pass
        return q_exec.clear.called

    def test_hosted_keeps_cache_local_clears(self):
        self.assertFalse(self._run(True))
        self.assertTrue(self._run(False))


if __name__ == '__main__':
    unittest.main()

"""Deploys pin the Streamlit runtime instead of inheriting the account default.

BCR-2342 makes the container runtime the default for new Streamlits. The container runtime shares one process between
viewers and Snowflake documents that ROOT_LOCATION deploys (the shape pipeline.deploy_streamlit_app uses) are not
supported on it, so every deploy now runs ALTER STREAMLIT ... SET RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline  # noqa: E402


class _Result:
    def collect(self):
        return []


class _File:
    def __init__(self):
        self.put_calls = []

    def put(self, *a, **k):
        self.put_calls.append(a)


class _Session:
    def __init__(self):
        self.sql_text = []
        self.file = _File()

    def sql(self, text):
        self.sql_text.append(text)
        return _Result()

    def get_current_warehouse(self):
        return '"WH"'


class RuntimePin(unittest.TestCase):
    def _deploy(self, **kw):
        root = tempfile.mkdtemp()
        Path(root, 'app_x.py').write_text('import streamlit\n')
        session = _Session()
        with patch.object(pipeline, 'ensure_target', lambda *a, **k: None), \
                patch.object(pipeline, '_snowsight_url', lambda *a, **k: None):
            rec = pipeline.deploy_streamlit_app(session, 'x', 'app_x.py', db='DB', schema='S', identifier='APP',
                                                warehouse='WH', datasources={}, runtime_modules=[], root_dir=root,
                                                **kw)
        return session, rec

    def test_pin_runs_after_create(self):
        session, rec = self._deploy()
        create = next(i for i, t in enumerate(session.sql_text) if t.startswith('CREATE OR REPLACE STREAMLIT'))
        self.assertEqual(session.sql_text[create + 1],
                         'ALTER STREAMLIT "DB"."S"."APP" SET RUNTIME_NAME = \'SYSTEM$WAREHOUSE_RUNTIME\'')
        self.assertEqual(rec['runtime_name'], 'SYSTEM$WAREHOUSE_RUNTIME')

    def test_container_runtime_is_refused_before_anything_is_created(self):
        with patch.dict(os.environ, {'SIS_RUNTIME_NAME': 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'}):
            with self.assertRaises(RuntimeError):
                self._deploy()


if __name__ == '__main__':
    unittest.main()

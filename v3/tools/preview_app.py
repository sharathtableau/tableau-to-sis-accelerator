"""Release 27: local dashboard preview for the rendered-page gate (tools/rendered_check.py).

Draws ONE dashboard of ONE workbook exactly the way the deployed runtime does (engine.render_dashboard with the
runtime's presentation argument), so the gate measures what users see.

  python -m streamlit run tools/preview_app.py --server.port 8601 --server.headless true
  http://localhost:8601/?wb=<workbook path>&d=<dashboard name>[&sf=1]

sf=1 opens a READ-ONLY Snowpark session from an existing named connection profile (env V3_PREVIEW_CONNECTION,
default "wbr") for workbooks whose data is live Snowflake. Credentials stay in the profile; this file never reads,
prints or stores them. Without sf=1 the workbook's bundled data is used (local DuckDB).
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(page_title='V3 preview', layout='wide')

import backend  # noqa: E402
import engine  # noqa: E402
from v3_local import prepare_payload, preview_context  # noqa: E402

q = st.query_params
wb, dash_name, use_sf = q.get('wb', ''), q.get('d', ''), q.get('sf') == '1'
if not wb or not Path(wb).is_file():
    st.error('Pass ?wb=<existing workbook path>&d=<dashboard name>')
    st.stop()


@st.cache_resource
def _session(name):
    from snowflake.snowpark import Session
    return Session.builder.config('connection_name', name).create()


if use_sf:
    try:
        backend.set_session(_session(os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')))
    except Exception as exc:  # never show connection details
        st.error(f'Snowflake session unavailable ({type(exc).__name__})')
        st.stop()
else:
    backend.set_session(None)


@st.cache_data(show_spinner=False)
def _intake(path, mtime, dashboard, sf):
    raw = Path(path).read_bytes()
    return prepare_payload(raw, Path(path).name, published_views=[dashboard] if dashboard else None)


result = _intake(wb, Path(wb).stat().st_mtime, dash_name, use_sf)
dashboards = result['ir'].get('dashboards', [])
match = [d for d in dashboards if d.get('name') == dash_name] or dashboards[:1]
if not match:
    st.error('No dashboard in this workbook')
    st.stop()
with preview_context(result['ir'], dict(result['bindings']), snowflake=use_sf):
    engine.render_dashboard(match[0], presentation='native')   # same argument as v3_runtime / v3_console

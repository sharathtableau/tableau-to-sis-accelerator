"""Snowflake-only entry point; refuse local fallback in the candidate app."""
import streamlit as st
import backend
from pathlib import Path
import runpy

if backend._active_session() is None:
    st.error('The V3 Snowflake candidate requires an active Snowpark session.')
    st.stop()

# Streamlit re-executes this entry on every interaction; module imports are cached.
# The migration console (original workflow and look) is the entry; it keeps the
# single-page workbench (v3_app.py) available behind its 'Classic V3 workbench' toggle.
runpy.run_path(str(Path(__file__).with_name('v3_console.py')),run_name='__main__')

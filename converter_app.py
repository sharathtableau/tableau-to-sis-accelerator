"""
converter_app.py  --  THE IN-SNOWFLAKE CONVERTER.

A single Streamlit-in-Snowflake app. Upload a Tableau .twb/.twbx and it
parses, loads the data into Snowflake tables (via the app's own Snowpark
session), and renders the converted dashboard live -- all inside Snowflake,
nothing on a laptop.

Runs identically outside Snowflake (local DuckDB) so it can be developed and
tested before deployment.

Deploy to Snowsight: create a Streamlit app, upload this file plus
  engine.py, backend.py, config.py, calc_translator.py, findings.py,
  profile_superstore.py, tableau_parser.py, init_workbook.py
and add packages: altair, pandas, plotly, openpyxl.

DATA HANDLING
  * Live-Snowflake-connected workbooks: data already in Snowflake -> map to
    the existing tables, no load needed.
  * CSV / Excel extract (.twbx): read bundled file -> write_pandas to a table.
  * .hyper extract: tableauhyperapi is NOT in Snowflake's Anaconda channel,
    so hyper decode only works locally. In Snowflake, convert the workbook's
    extract to CSV once with `python init_workbook.py Book.twbx` first, or
    repoint the datasource at an existing Snowflake table.
"""

import json
import os
import re
import tempfile

import streamlit as st

import codegen
import config
import engine
import findings
import pipeline
import tableau_parser as TP

# Re-exported for callers/tests that imported these names from this module
# before the shared logic moved to pipeline.py.
LOAD_DB = pipeline.LOAD_DB
LOAD_SCHEMA = pipeline.LOAD_SCHEMA
get_session = pipeline.get_session
_fqn = pipeline.fqn
_bundled_data_files = pipeline.bundled_data_files
_decode_hypers_locally = pipeline.decode_hypers_locally
_match_files_to_datasources = pipeline.match_files_to_datasources
_configure_datasources = pipeline.configure_datasources
_load_into_snowflake = pipeline.load_into_snowflake

st.set_page_config(page_title="Tableau → Streamlit Converter",
                   layout="wide", initial_sidebar_state="expanded")


def _render_ir(ir):
    """Render a parsed IR inline (engine.run minus its own page config/title).
    Each dashboard is guarded: one failing dashboard can never blank the app."""
    import traceback
    findings.clear()
    engine.configure(ir)
    for cap, formula in (ir.get("calc_drops") or {}).items():
        findings.record("WARNING", "(workbook)", "calc-untranslated",
                        f"Calculated field '{cap}' could not translate: "
                        f"{formula.strip()[:120]}")
    for story in ir.get("stories") or []:
        findings.record("WARNING", story, "story-unsupported",
                        "Tableau Stories not converted (sheets available as tabs).")
    try:
        engine._render_param_controls()
    except Exception as e:
        findings.record("WARNING", "(parameters)", "param-controls-failed",
                        f"{type(e).__name__}: {e}")
    if not ir["dashboards"]:
        st.warning("No dashboards found in this workbook.")
        return
    tabs = st.tabs([d["title"] for d in ir["dashboards"]])
    for tab, dash in zip(tabs, ir["dashboards"]):
        with tab:
            try:
                engine.render_dashboard(dash)
            except Exception as e:
                findings.record("BLOCKER", dash["name"], "dashboard-failed",
                                f"{type(e).__name__}: {e}")
                st.error(f"Dashboard '{dash['title']}' could not render "
                         f"({type(e).__name__}: {e}).")
                with st.expander("Details"):
                    st.code(traceback.format_exc())
    engine._render_findings()


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("Tableau → Streamlit Converter")
session = get_session()
where = "Snowflake" if session else "local (DuckDB)"
st.caption(f"Running in: **{where}**.  Upload a Tableau workbook to convert it "
           f"to a live Streamlit dashboard.")

up = st.file_uploader("Tableau workbook", type=["twb", "twbx"])

def _convert_and_render(up):
    workdir = tempfile.mkdtemp(prefix="twbconv_")
    wb_path = os.path.join(workdir, up.name)
    raw = up.getvalue()
    with open(wb_path, "wb") as f:
        f.write(raw)

    # 1+2. discover datasources, extract/decode bundled data, load (Snowflake)
    # or wire local files (DuckDB) -- single shared path (pipeline.onboard),
    # so this never diverges from the staged demo UI or the CLI (convert.py).
    disc = pipeline.onboard(wb_path, raw, in_snowflake=bool(session),
                            session=session)
    caption_to_file = disc["caption_to_file"]
    if disc["blocked"]:
        st.warning("This workbook ships a **.hyper** extract "
                   f"({', '.join(disc['blocked'])}) that cannot be decoded "
                   "inside Snowflake. Run `python init_workbook.py` on it "
                   "once to produce CSVs, or repoint its datasource at an "
                   "existing Snowflake table, then re-upload.")

    with st.status("Converting…", expanded=True) as status:
        st.write("Parsed workbook.")
        if disc["load_report"]:
            for cap, table, n, note in disc["load_report"]:
                st.write(f"• `{cap}` → `{table}` — {n} rows ({note})")
        else:
            for cap, path in caption_to_file.items():
                st.write(f"• `{cap}` → local `{os.path.basename(path) if path else '—'}`")
        ir = TP.build_ir(wb_path)
        st.write(f"Inferred {sum(len(d['sheets']) for d in ir['dashboards'])} "
                 f"sheets across {len(ir['dashboards'])} dashboard(s).")
        # SAVE the generated app + IR to the project folder (persistent output)
        stem = re.sub(r"[^0-9A-Za-z]+", "_",
                      os.path.splitext(up.name)[0]).strip("_").lower() or "workbook"
        app_name = f"app_{stem}.py"
        app_code = codegen.build(ir)
        with open(app_name, "w", encoding="utf-8") as f:
            f.write(app_code)
        with open(f"{stem}_ir.json", "w", encoding="utf-8") as f:
            json.dump(ir, f, indent=2)
        st.write(f"Saved generated app → **{os.path.abspath(app_name)}**")
        status.update(label="Converted + saved.", state="complete", expanded=False)

    st.success(f"Generated app saved to your folder as `{app_name}`. "
               f"Run it standalone with:  `streamlit run {app_name}`")
    st.download_button("Download the generated app.py", app_code,
                       file_name=app_name, mime="text/x-python")
    st.divider()
    _render_ir(ir)


if up is not None:
    import traceback
    try:
        _convert_and_render(up)
    except Exception as e:
        st.error(f"Could not convert **{up.name}** — {type(e).__name__}: {e}")
        st.caption("The conversion stopped before rendering. Details below — "
                   "share this if it's unexpected.")
        with st.expander("Error details"):
            st.code(traceback.format_exc())
else:
    st.info("Drop a `.twb` or `.twbx` above. Live-Snowflake-connected and "
            "CSV/Excel-extract workbooks convert fully in-Snowflake; `.hyper` "
            "extracts need a one-time local CSV step first.")

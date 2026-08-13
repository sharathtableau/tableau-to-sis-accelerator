"""pipeline_app.py -- THE Tableau to SiS migration workbench (V2 UI).

The deployed staged demo. Design follows tableau_to_sis_v2_preview.html and the
approved pipeline_app_v2.py prototype: a dark-navy icon-led nav, a five-stage
orientation strip, real live progress, and one workbook per migration run.

Structure:
  Overview        what the accelerator does, one automated workflow
  New migration   intake (upload OR Tableau Server/Cloud) -> the real 5-stage run
  Inventory       dashboards / data model / calculations / filters / findings
  Preview         the generated app rendered live
  Validation      verdict, review queue, evidence, and the DEEP proof machinery
  Deploy & Ask    human-gated Snowflake deploy + Cortex Analyst
  Architecture    the platform board (gen_platform_architecture.py)

All migration logic is reused from the existing modules (pipeline /
tableau_parser / semantic_layer / cortex_semantic / codegen / engine / parity)
-- nothing is reimplemented here. The heavyweight validation and reporting
features carried over from the pre-V2 UI (Cortex-judged section validation,
Cortex vision validation, the skill-methodology dashboard-by-dashboard report,
the R12 proof-first validation pack, and the migration report PDF) live in
deep_validation.py and are wired into the Validation page as on-demand actions.

    streamlit run pipeline_app.py --server.port 8510
"""

from __future__ import annotations

import datetime
import html as _html
import json
import os
import re
import tempfile
import traceback

import streamlit as st

import backend
import calc_translator
import codegen
import config
import cortex_semantic as CS
import deep_validation as DV
import engine
import findings
import gen_platform_architecture as ARCH
import parity
import pipeline
import semantic_layer as SL  # noqa: F401  (pipeline.data_model_report imports it)
import tableau_parser as TP
import tableau_server as TS

st.set_page_config(
    page_title="Tableau to SiS Workbench",
    page_icon=":material/swap_horiz:",
    layout="wide",
    # The nav IS the workbench -- "auto" collapses it on narrower viewports and
    # Streamlit drops it from the DOM entirely, leaving no way back to the other
    # pages. Always open.
    initial_sidebar_state="expanded",
)

# The five REAL pipeline stages. Names, order and count must stay in step with
# run_migration() below -- the stepper, live progress and activity feed all
# index into this one list, so they can never drift apart.
STAGES = ["Discovery", "Parsing", "Data model", "App build", "Validation"]

STAGE_NOTE = {
    "Discovery": "Resolving datasources and landing data in Snowflake",
    "Parsing": "Building the dashboard, sheet, filter and calculation inventory",
    "Data model": "Reconstructing relationships and semantic definitions",
    "App build": "Generating the Streamlit in Snowflake application",
    "Validation": "Comparing migrated measures against source evidence",
}

PAGES = ["Overview", "New migration", "Inventory", "Preview", "Validation",
         "Deploy & Ask"]

# Material Symbols ligature per nav item. st.radio has no `icon=` parameter, so
# these are injected as ::before content on each option -- using the same
# Material Symbols font Streamlit already loads for st.button icons, so the nav
# and the command buttons share one icon set.
PAGE_ICON = {
    "Overview": "dashboard",
    "New migration": "note_add",
    "Inventory": "account_tree",
    "Preview": "web_asset",
    "Validation": "verified_user",
    "Deploy & Ask": "rocket_launch",
}


def _nav_icon_css():
    rules = [
        'section[data-testid="stSidebar"] div[role="radiogroup"] label p::before {'
        " font-family:'Material Symbols Rounded','Material Symbols Outlined';"
        " font-weight:normal; font-size:1.05rem; line-height:1; margin-right:10px;"
        " vertical-align:-3px; color:#6dcdf0; }",
        'section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked)'
        " p::before { color:#ffffff; }",
    ]
    for index, page in enumerate(PAGES, start=1):
        rules.append(
            f'section[data-testid="stSidebar"] div[role="radiogroup"] '
            f'label:nth-of-type({index}) p::before {{ content:"{PAGE_ICON[page]}"; }}'
        )
    return "\n".join(rules)


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def inject_styles():
    st.markdown(
        """
        <style>
        __NAV_ICON_CSS__
        :root {
            --ink:#102a43; --muted:#62788a; --line:#d6e6ef; --soft:#f2f8fb;
            --blue:#29b5e8; --blue-dark:#11567f; --navy:#071b2e; --navy-soft:#0c2941;
            --green:#14a07a; --amber:#d99b2b; --red:#cf4e58;
        }
        .stApp { background:#f5fafc; color:var(--ink); }
        .block-container { max-width:1480px; padding-top:1.3rem; padding-bottom:3rem; }

        /* ---- Sidebar: deep navy navigation ------------------------------- */
        section[data-testid="stSidebar"] { border-right:0; background:var(--navy); }
        section[data-testid="stSidebar"] .block-container { padding-top:1.25rem; }
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] label { color:#b9cedd !important; }
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#7fa0b8 !important; }
        section[data-testid="stSidebar"] hr { border-color:#1c3b53; }
        section[data-testid="stSidebar"] div[role="radiogroup"] { gap:0; }
        section[data-testid="stSidebar"] div[role="radiogroup"] label {
            border-radius:5px; padding:8px 10px; margin-bottom:3px;
            border-left:3px solid transparent; transition:background .15s ease;
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] label:hover { background:#0c2941; }
        section[data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child { display:none; }
        section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
            background:#123b58; border-left-color:var(--blue);
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
            color:#fff !important; font-weight:700;
        }
        section[data-testid="stSidebar"] [data-testid="stExpander"] {
            border-color:#26465e; background:#0a2338;
        }
        .v2-brand { display:flex; align-items:center; gap:11px; font-weight:800;
                    color:#fff; margin-bottom:1.2rem; }
        .v2-brand-text { color:#fff !important; line-height:1.05; }
        .v2-brand-text small { display:block; color:#6dcdf0 !important; font-size:.62rem;
                               text-transform:uppercase; margin-top:5px; letter-spacing:.08em; }
        .v2-mark { position:relative; width:24px; height:24px; display:block; flex:none; }
        .v2-mark i { position:absolute; left:10px; top:2px; width:4px; height:20px;
                     border-radius:2px; background:var(--blue); transform-origin:center; }
        .v2-mark i:nth-child(2){transform:rotate(60deg)}
        .v2-mark i:nth-child(3){transform:rotate(120deg)}
        .v2-sidebar-note { font-size:.72rem; color:#7fa0b8; border-top:1px solid #1c3b53;
                           padding-top:.9rem; margin-top:1rem; }

        /* ---- Typography -------------------------------------------------- */
        h1,h2,h3,h4 { color:var(--ink); letter-spacing:0; }
        h1 { font-size:1.6rem !important; line-height:1.2 !important; margin-bottom:.25rem !important; }
        h2 { font-size:1.2rem !important; }
        h3 { font-size:1rem !important; }
        p, label, [data-testid="stCaptionContainer"] { color:var(--muted); }

        /* ---- Native widgets ---------------------------------------------- */
        [data-testid="stMetric"] {
            border:1px solid var(--line); border-radius:6px; background:#fff;
            padding:.8rem 1rem; box-shadow:0 5px 18px rgba(17,86,127,.06);
        }
        [data-testid="stMetric"] label { font-size:.76rem; }
        [data-testid="stMetricValue"] { font-size:1.4rem; }
        [data-testid="stFileUploaderDropzone"] {
            min-height:150px; border:1.5px dashed #67c5e8; border-radius:6px;
            background:#f7fcfe; padding:1.5rem 1.2rem;
        }
        [data-testid="stFileUploaderDropzone"] svg { color:var(--blue); width:30px; height:30px; }
        button[kind="primary"] { border-radius:5px; background:var(--blue-dark);
                                 border-color:var(--blue-dark); font-weight:700; }
        button[kind="primary"]:hover { background:#0d486c; border-color:#0d486c; }
        button[kind="secondary"] { border-radius:5px; border-color:#bfd4e1; }
        [data-testid="stExpander"] { border:1px solid var(--line); border-radius:6px; }
        [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:6px; overflow:hidden; }

        /* ---- Top bar ------------------------------------------------------ */
        .v2-topbar { display:flex; justify-content:space-between; align-items:flex-start;
                     gap:20px; border:1px solid var(--line); border-top:4px solid var(--blue);
                     border-radius:8px; padding:17px 19px; margin-bottom:1.1rem; background:#fff;
                     box-shadow:0 8px 26px rgba(17,86,127,.07); }
        .v2-topbar p { margin:.2rem 0 0; font-size:.88rem; }
        .v2-kicker { color:var(--blue-dark); font-size:.7rem; font-weight:800;
                     text-transform:uppercase; margin-bottom:.25rem; letter-spacing:.06em; }
        .v2-state { white-space:nowrap; border:1px solid #bcddec; color:var(--blue-dark);
                    background:#eef8fc; padding:7px 11px; border-radius:999px;
                    font-size:.72rem; font-weight:800; text-transform:uppercase; }
        .v2-state.ok { border-color:#b8dfd2; color:#087254; background:#edf9f5; }
        .v2-state.warn { border-color:#f0d9a8; color:#8b5c08; background:#fff6e6; }

        /* ---- Stepper ------------------------------------------------------ */
        .v2-stepper { display:grid; grid-template-columns:repeat(5,minmax(0,1fr));
                      border:1px solid var(--line); border-radius:7px; overflow:hidden;
                      margin:.6rem 0 1.1rem; background:#fff;
                      box-shadow:0 6px 20px rgba(17,86,127,.05); }
        .v2-step { position:relative; padding:11px 12px 10px 42px; min-height:52px;
                   border-right:1px solid var(--line); background:#fff; }
        .v2-step:last-child { border-right:0; }
        .v2-step b { display:block; font-size:.78rem; color:var(--ink); }
        .v2-step span { font-size:.68rem; color:var(--muted); }
        .v2-step i { position:absolute; left:12px; top:13px; display:flex;
                     align-items:center; justify-content:center; width:21px; height:21px;
                     border-radius:50%; background:#e7ebef; color:#64717d; font-style:normal;
                     font-size:.67rem; font-weight:800; }
        .v2-step.active { background:#edf9fd; box-shadow:inset 0 3px 0 var(--blue); }
        .v2-step.active i { background:var(--blue-dark); color:#fff;
                            animation:v2Pulse 1.25s ease-in-out infinite; }
        .v2-step.done { box-shadow:inset 0 3px 0 var(--green); }
        .v2-step.done i { background:#dff3eb; color:var(--green); }
        .v2-step.failed { background:#fff5f5; box-shadow:inset 0 3px 0 var(--red); }
        .v2-step.failed i { background:#fde4e6; color:var(--red); }

        /* ---- Live run panel ----------------------------------------------- */
        .v2-live { border:1px solid var(--line); border-radius:7px; padding:17px 18px;
                   margin:12px 0; background:#fff; box-shadow:0 7px 22px rgba(17,86,127,.06); }
        .v2-live-head { display:flex; justify-content:space-between; gap:15px;
                        align-items:flex-start; margin-bottom:12px; font-size:.8rem; }
        .v2-live-head b { color:var(--ink); font-size:.95rem; }
        .v2-live-head small { display:block; color:var(--muted); font-size:.72rem; margin-top:2px; }
        .v2-live-state { display:flex; align-items:center; gap:7px; color:var(--blue-dark);
                         font-size:.72rem; font-weight:800; text-transform:uppercase;
                         white-space:nowrap; }
        .v2-live-state.done { color:var(--green); }
        .v2-live-state.failed { color:var(--red); }
        .v2-dot { width:9px; height:9px; border-radius:50%; background:var(--blue);
                  animation:v2Pulse 1.25s ease-in-out infinite; flex:none; }
        .v2-dot.done { background:var(--green); animation:none; }
        .v2-dot.failed { background:var(--red); animation:none; }
        .v2-track { height:9px; overflow:hidden; border-radius:999px; background:#e4eef3; }
        .v2-fill { height:100%; border-radius:999px;
                   background:linear-gradient(90deg,var(--blue-dark),var(--blue),var(--green));
                   background-size:180% 100%; transition:width .55s ease;
                   animation:v2Flow 1.4s linear infinite; }
        .v2-fill.done { animation:none; background:var(--green); }
        .v2-fill.failed { animation:none; background:var(--red); }
        .v2-meta { display:flex; justify-content:space-between; gap:12px; margin-top:7px;
                   color:var(--muted); font-size:.72rem; }
        .v2-meta b { color:var(--ink); }
        .v2-events { margin-top:14px; border-top:1px solid var(--line); }
        .v2-event { display:grid; grid-template-columns:16px 1fr; gap:9px; padding:9px 0;
                    border-bottom:1px solid #e7eff3; font-size:.78rem; }
        .v2-event:last-child { border-bottom:0; }
        .v2-event i { width:8px; height:8px; margin-top:5px; border-radius:50%; background:#a7bbc6; }
        .v2-event.current i { background:var(--blue); animation:v2Pulse 1.25s ease-in-out infinite; }
        .v2-event.done i { background:var(--green); }
        .v2-event.failed i { background:var(--red); }
        .v2-event b { display:block; color:var(--ink); font-size:.76rem; }
        .v2-event span { color:var(--muted); font-size:.72rem; }
        @keyframes v2Pulse { 0%,100%{opacity:.4;transform:scale(.85)} 50%{opacity:1;transform:scale(1.15)} }
        @keyframes v2Flow { from{background-position:100% 0} to{background-position:-80% 0} }

        /* ---- Overview surfaces -------------------------------------------- */
        .v2-flow { display:grid; grid-template-columns:1fr 44px 1.08fr 44px 1fr;
                   align-items:center; border-radius:8px; overflow:hidden; margin:1rem 0 1.4rem;
                   background:var(--navy); border:1px solid #143a55;
                   box-shadow:0 15px 38px rgba(7,27,46,.18); }
        .v2-flow section { min-height:130px; padding:22px; background:transparent;
                           border-top:3px solid var(--blue); }
        .v2-flow section:nth-of-type(2) { background:var(--navy-soft); }
        .v2-flow section:nth-of-type(3) { border-color:#38c89b; }
        .v2-flow small { display:block; color:#6dcdf0; font-size:.66rem; text-transform:uppercase;
                         font-weight:800; margin-bottom:9px; letter-spacing:.08em; }
        .v2-flow b { display:block; color:#fff; font-size:1.03rem; margin-bottom:7px; }
        .v2-flow span { color:#a9c1d1; font-size:.78rem; line-height:1.5; }
        .v2-flow i { text-align:center; color:var(--blue); font-style:normal; font-size:1.3rem; }
        .v2-cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:18px;
                    margin:.8rem 0 1.3rem; }
        .v2-card { border:1px solid var(--line); border-top:4px solid var(--accent,var(--blue));
                   border-radius:7px; padding:16px; background:#fff;
                   box-shadow:0 7px 22px rgba(17,86,127,.06); }
        .v2-card h3 { margin:0 0 9px; font-size:.95rem !important; }
        .v2-card p { margin:0 0 7px; font-size:.79rem; line-height:1.45; }
        .v2-trust { display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
                    border-top:1px solid var(--line); border-bottom:1px solid var(--line);
                    margin:1.1rem 0 1.4rem; background:#fff; }
        .v2-trust div { padding:13px 16px; border-right:1px solid var(--line); }
        .v2-trust div:last-child { border-right:0; }
        .v2-trust b { display:block; color:var(--blue-dark); font-size:.78rem; }
        .v2-trust span { color:var(--muted); font-size:.7rem; }

        /* ---- Intake side rail --------------------------------------------- */
        .v2-note { border-left:4px solid var(--blue); background:#eaf7fc; padding:11px 13px;
                   color:#294a60; font-size:.84rem; margin:.6rem 0 .8rem; }
        .v2-note b { color:var(--blue-dark); }
        .v2-auto { display:flex; gap:12px; align-items:flex-start; border:1px solid #b8dfd2;
                   background:#edf9f5; border-radius:7px; padding:13px 15px; margin:.6rem 0 1rem; }
        .v2-auto b { display:block; color:#087254; font-size:.84rem; }
        .v2-auto span { display:block; color:#4d7164; font-size:.77rem; margin-top:3px; }
        .v2-next { border:1px solid var(--line); border-radius:7px; background:#fff; overflow:hidden; }
        .v2-next-title { padding:11px 13px; border-bottom:1px solid var(--line);
                         color:var(--ink); font-size:.78rem; font-weight:800; }
        .v2-next-row { display:grid; grid-template-columns:30px 1fr; gap:10px; align-items:center;
                       padding:10px 12px; border-bottom:1px solid #e7eff3; }
        .v2-next-row:last-child { border-bottom:0; }
        .v2-next-icon { display:flex; width:28px; height:28px; align-items:center;
                        justify-content:center; border-radius:6px; background:#e8f7fc;
                        color:var(--blue-dark); font-size:.67rem; font-weight:900; }
        .v2-next-row b { display:block; color:var(--ink); font-size:.75rem; }
        .v2-next-row span { display:block; color:var(--muted); font-size:.68rem; margin-top:2px; }

        /* ---- Summary + empty ---------------------------------------------- */
        .v2-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px;
                      margin:.7rem 0 1rem; }
        .v2-summary div { border:1px solid var(--line); border-radius:6px; padding:10px 12px;
                          background:#fff; }
        .v2-summary span { display:block; font-size:.69rem; color:var(--muted); text-transform:uppercase; }
        .v2-summary b { display:block; font-size:1.05rem; margin-top:3px; color:var(--ink); }
        .v2-empty { border:1px dashed #aecbdc; background:#fff; border-radius:7px; padding:30px;
                    text-align:center; color:var(--muted); }
        .v2-section { border-top:1px solid var(--line); padding-top:1rem; margin-top:1rem; }

        /* ---- Architecture board ------------------------------------------- */
        .v2-arch { width:100%; overflow-x:auto; background:#0a1428; border:1px solid #143a55;
                   border-radius:8px; padding:clamp(.75rem,1.6vw,1.3rem);
                   box-shadow:0 14px 34px rgba(7,27,46,.16); }

        @media(max-width:900px){
            .v2-stepper,.v2-summary,.v2-cards,.v2-trust { grid-template-columns:1fr 1fr; }
            .v2-step { border-bottom:1px solid var(--line); }
            .v2-flow { grid-template-columns:1fr; }
            .v2-flow i { padding:5px; transform:rotate(90deg); }
            .v2-topbar { display:block; }
            .v2-state { display:inline-block; margin-top:.6rem; }
        }
        </style>
        """.replace("__NAV_ICON_CSS__", _nav_icon_css()),
        unsafe_allow_html=True,
    )


def esc(value):
    return _html.escape(str(value if value is not None else "-"), quote=True)


def stem_for(name):
    return re.sub(r"[^0-9A-Za-z]+", "_", os.path.splitext(name)[0]).strip("_").lower() or "workbook"


def active_run():
    return st.session_state.get("v2_run")


def queue_page(page):
    """Apply navigation before the radio widget is created on the next rerun."""
    st.session_state["v2_pending_page"] = page


def reset_migration():
    for key in ["v2_run", "v2_source", "v2_fetched", "v2_tableau_conn", "v2_deployment"]:
        st.session_state.pop(key, None)
    queue_page("New migration")


def run_state():
    """(label, css class) for the header pill -- reflects the real run, never a
    decorative status."""
    run = active_run()
    if not run:
        return "Ready for migration", ""
    bugs = run["validation"]["summary"]["measures_bug"]
    return ("Validation passed", "ok") if bugs == 0 else (f"{bugs} to review", "warn")


def render_header(title, subtitle):
    label, klass = run_state()
    st.markdown(
        f"""
        <div class="v2-topbar">
          <div><div class="v2-kicker">Tableau to Streamlit in Snowflake</div>
          <h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>
          <div class="v2-state {klass}">{esc(label)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stepper(done=0, active=1, failed=None):
    """Five-stage orientation strip. `failed` is a 1-based stage index."""
    parts = []
    for idx, name in enumerate(STAGES, 1):
        if failed == idx:
            cls, marker, note = "failed", "!", "Failed"
        elif idx <= done:
            cls, marker, note = "done", "&#10003;", "Complete"
        elif idx == active:
            cls, marker, note = "active", str(idx), "Running"
        else:
            cls, marker, note = "", str(idx), "Pending"
        parts.append(f'<div class="v2-step {cls}"><i>{marker}</i>'
                     f'<b>{esc(name)}</b><span>{note}</span></div>')
    st.markdown(f'<div class="v2-stepper">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_live(slot, workbook, origin, stage, events, phase="running"):
    """Live run panel: real progress derived from actual stage boundaries.

    No timers and no synthetic delays -- the bar advances only when a stage
    genuinely starts or finishes, so a slow stage visibly sits still."""
    index = STAGES.index(stage) if stage in STAGES else 0
    if phase == "done":
        percent, state_txt, state_cls = 100, "Migration complete", "done"
    elif phase == "failed":
        percent = int((index / len(STAGES)) * 100)
        state_txt, state_cls = f"{stage} failed", "failed"
    else:
        percent = int(((index + 0.45) / len(STAGES)) * 100)
        state_txt, state_cls = f"{stage} in progress", ""

    steps = []
    for i, name in enumerate(STAGES):
        if phase == "failed" and i == index:
            cls, marker, note = "failed", "!", "Failed"
        elif phase == "done" or i < index:
            cls, marker, note = "done", "&#10003;", "Complete"
        elif i == index:
            cls, marker, note = "active", str(i + 1), "Running"
        else:
            cls, marker, note = "", str(i + 1), "Pending"
        steps.append(f'<div class="v2-step {cls}"><i>{marker}</i>'
                     f'<b>{esc(name)}</b><span>{note}</span></div>')

    rows = []
    for pos, (title, detail, kind) in enumerate(events):
        cls = kind if kind in ("done", "failed") else ("current" if pos == 0 else "")
        rows.append(f'<div class="v2-event {cls}"><i></i><div><b>{esc(title)}</b>'
                    f'<span>{esc(detail)}</span></div></div>')

    slot.markdown(
        f'<div class="v2-live">'
        f'<div class="v2-live-head"><div><b>{esc(workbook)}</b>'
        f'<small>{esc(origin)} &rarr; Streamlit in Snowflake</small></div>'
        f'<div class="v2-live-state {state_cls}"><span class="v2-dot {state_cls}"></span>'
        f'{esc(state_txt)}</div></div>'
        f'<div class="v2-track"><div class="v2-fill {state_cls}" style="width:{percent}%"></div></div>'
        f'<div class="v2-meta"><span>{esc(STAGE_NOTE.get(stage, ""))}</span><b>{percent}%</b></div>'
        f'<div class="v2-stepper" style="margin:14px 0 0">{"".join(steps)}</div>'
        f'<div class="v2-events">{"".join(rows)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Session + navigation
# --------------------------------------------------------------------------- #
def resolve_session():
    """Snowflake session: the hosted SiS one, else an opt-in local `snow` CLI
    connection. DuckDB is never surfaced as a target -- it stays an internal
    local-development fallback inside backend.py."""
    hosted = pipeline.get_session()
    if hosted is not None:
        return hosted, "Snowflake (hosted)"
    session = st.session_state.get("v2_sf_session")
    if session is not None:
        return session, f"Snowflake ({st.session_state.get('v2_sf_conn', 'connected')})"
    return None, "Snowflake (not connected)"


def sidebar(session, session_label):
    with st.sidebar:
        st.markdown(
            '<div class="v2-brand"><span class="v2-mark"><i></i><i></i><i></i></span>'
            '<span class="v2-brand-text">Tableau to SiS<small>Migration workbench</small></span></div>',
            unsafe_allow_html=True,
        )
        page = st.radio("Navigation", PAGES, label_visibility="collapsed", key="v2_page")
        st.markdown("---")
        st.caption("TARGET PLATFORM")
        st.write(f"**{session_label}**")
        st.caption("Workbook datasources are matched to existing Snowflake objects "
                   "automatically from workbook metadata.")
        if pipeline.get_session() is None:
            with st.expander("Local developer connection"):
                conn = st.text_input("Snow CLI connection", value="wbr", key="v2_conn")
                if st.button("Connect", icon=":material/link:",
                             use_container_width=True, key="v2_connect"):
                    try:
                        with st.spinner("Connecting to Snowflake..."):
                            st.session_state["v2_sf_session"] = pipeline.snow_session(conn)
                        st.session_state["v2_sf_conn"] = conn
                        st.rerun()
                    except Exception as exc:
                        st.session_state.pop("v2_sf_session", None)
                        st.error(f"Could not connect: {type(exc).__name__}")
                st.caption("Required to land data, deploy, and use Cortex Analyst.")
        run = active_run()
        if run:
            st.markdown("---")
            st.caption("ACTIVE WORKBOOK")
            st.write(f"**{run['name']}**")
            s = run["validation"]["summary"]
            st.caption(f"{len(run['ir'].get('dashboards', []))} dashboards · "
                       f"{s['measures_pass']}/{s['measures_checked']} measures passed")
            st.button("Start another migration", use_container_width=True, key="v2_reset",
                      on_click=reset_migration, icon=":material/refresh:")
    return page


# --------------------------------------------------------------------------- #
# Pages: Overview + Architecture
# --------------------------------------------------------------------------- #
def page_overview():
    render_header(
        "Tableau to SiS Accelerator",
        "Automated discovery, conversion, validation and governed deployment for one "
        "Tableau workbook at a time.",
    )
    st.markdown(
        """
        <div class="v2-flow">
          <section><small>Source</small><b>Tableau workbook</b>
            <span>Upload one TWB/TWBX or pull it directly from Tableau Server or Cloud.</span></section>
          <i>&rsaquo;</i>
          <section><small>Automated accelerator</small><b>Discover, translate, rebuild</b>
            <span>Resolve Snowflake data, reconstruct the data model, translate calculations,
            and generate the application.</span></section>
          <i>&rsaquo;</i>
          <section><small>Outcome</small><b>Streamlit in Snowflake</b>
            <span>A reviewable application with validation evidence and a human-gated
            deployment.</span></section>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader("What the accelerator handles")
    st.markdown(
        """
        <div class="v2-cards">
          <div class="v2-card" style="--accent:#29b5e8"><h3>Connect and understand</h3>
            <p>Upload a workbook or browse Tableau Server/Cloud.</p>
            <p>Detect datasources, dashboards, sheets, filters, parameters and calculations.</p></div>
          <div class="v2-card" style="--accent:#11567f"><h3>Automate the migration</h3>
            <p>Match workbook sources to governed Snowflake data automatically.</p>
            <p>Rebuild relationships, semantic definitions, controls and visual behaviour.</p></div>
          <div class="v2-card" style="--accent:#14a07a"><h3>Prove and deploy</h3>
            <p>Preview the generated application and validate migrated measures.</p>
            <p>Produce review artifacts, deploy with approval, and query through Cortex Analyst.</p></div>
        </div>
        <div class="v2-trust">
          <div><b>Snowflake native</b><span>Runs inside the governance boundary</span></div>
          <div><b>Metadata driven</b><span>No manual table selection</span></div>
          <div><b>Live migration status</b><span>Real progress across five stages</span></div>
          <div><b>Validation gated</b><span>Evidence before deployment</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.subheader("One automated workflow")
    render_stepper(done=0, active=1)
    st.markdown(
        '<div class="v2-auto"><div><b>No table selection or manual data-model setup</b>'
        '<span>The accelerator reads the Tableau workbook metadata, identifies the '
        'corresponding Snowflake objects, and reconstructs the model during the run.</span>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    action, detail = st.columns([0.25, 0.75])
    with action:
        st.button("Start a migration", type="primary", use_container_width=True,
                  key="v2_overview_start", on_click=queue_page, args=("New migration",),
                  icon=":material/play_arrow:")
    with detail:
        st.caption("Snowflake remains the execution and governance boundary throughout "
                   "the generated application.")

    # Platform architecture folded in here (not its own nav item) -- reference
    # material, collapsed by default so it doesn't lengthen the primary
    # workflow page, but one click away instead of a separate destination.
    with st.expander("Platform architecture — how the accelerator is wired"):
        st.caption("External systems in the margins, layers in the middle, Cortex "
                   "called in-account.")
        try:
            svg, _w, _h = ARCH.build_svg_fragment()
        except Exception as exc:
            st.error(f"Architecture board unavailable: {type(exc).__name__}: {exc}")
        else:
            st.markdown(f'<div class="v2-arch">{svg}</div>', unsafe_allow_html=True)
            st.caption("Rendered from gen_platform_architecture.py — the same source "
                       "used everywhere else this board appears, so it can never drift.")


# --------------------------------------------------------------------------- #
# Pages: intake + run
# --------------------------------------------------------------------------- #
def save_source(name, raw, origin, tableau_conn=None):
    st.session_state["v2_source"] = {"name": name, "raw": raw, "origin": origin}
    if tableau_conn:
        st.session_state["v2_tableau_conn"] = tableau_conn
    else:
        # An uploaded file has no live Tableau connection behind it; a stale one
        # from a previous REST fetch belongs to a DIFFERENT workbook and must
        # never be used to pull "truth" for this one.
        st.session_state.pop("v2_tableau_conn", None)


def render_upload_intake():
    upload = st.file_uploader("Tableau workbook", type=["twb", "twbx"], key="v2_upload",
                              accept_multiple_files=False)
    if upload is not None:
        save_source(upload.name, upload.getvalue(), "File upload")
        st.success(f"Ready: {upload.name}")
    st.caption("One workbook per migration run.")


def render_tableau_intake():
    st.caption("The personal access token stays in Streamlit secrets or environment "
               "variables (TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET). It is never entered here.")
    site_url = st.text_input(
        "Tableau site URL (any link from your site works -- home page, a "
        "workbook, a view)",
        value="https://prod-useast-b.online.tableau.com/#/site/b360bi",
        placeholder="https://<pod>.online.tableau.com/#/site/<site>/home",
        key="v2_site_url")
    if st.button("Connect to Tableau", icon=":material/link:", key="v2_tableau_connect",
                 disabled=not site_url):
        try:
            loc = TS.parse_site_url(site_url)
            with st.spinner("Loading projects and workbooks..."):
                browse = TS.list_site_contents(loc["server_url"], loc["site_content_url"])
            st.session_state["v2_browse"] = {**loc, **browse}
            st.success(f"Connected — {len(browse['projects'])} project(s), "
                       f"{len(browse['workbooks'])} workbook(s).")
        except Exception as exc:
            st.session_state.pop("v2_browse", None)
            st.error(f"Could not connect: {type(exc).__name__}: {exc}")

    browse = st.session_state.get("v2_browse")
    if browse:
        projects = sorted({w["project_name"] for w in browse["workbooks"] if w["project_name"]})
        project = st.selectbox("Project", ["All projects"] + projects, key="v2_project")
        books = [w for w in browse["workbooks"]
                 if project == "All projects" or w["project_name"] == project]
        books.sort(key=lambda row: row["name"].lower())
        if books:
            labels = [f"{w['name']} ({w['project_name']})" for w in books]
            selected = st.selectbox("Workbook", labels, index=None, key="v2_workbook",
                                    placeholder=f"Search {len(labels)} workbook(s)")
            if selected and st.button("Fetch workbook", icon=":material/cloud_download:",
                                      key="v2_fetch"):
                wb = books[labels.index(selected)]
                try:
                    with st.spinner(f"Downloading {wb['name']}..."):
                        fetched = TS.fetch_workbook_by_id(
                            browse["server_url"], browse["site_content_url"],
                            wb["id"], name_hint=wb["name"])
                    save_source(fetched["filename"], fetched["bytes"], "Tableau Server/Cloud",
                                {"server_url": browse["server_url"],
                                 "site_content_url": browse["site_content_url"],
                                 "workbook_id": wb["id"]})
                    st.success(f"Ready: {fetched['filename']}")
                except Exception as exc:
                    st.error(f"Could not fetch {wb['name']}: {type(exc).__name__}: {exc}")
        else:
            st.caption("No workbooks in this project.")

    with st.expander("Use a direct workbook or view link"):
        direct = st.text_input("Tableau workbook/view URL", key="v2_direct_url")
        if st.button("Fetch from link", icon=":material/cloud_download:",
                     key="v2_fetch_direct", disabled=not direct):
            try:
                with st.spinner("Downloading workbook..."):
                    fetched = TS.fetch_workbook(direct)
                loc = TS.parse_site_url(direct)
                save_source(fetched["filename"], fetched["bytes"], "Tableau direct link",
                            {"server_url": loc["server_url"],
                             "site_content_url": loc["site_content_url"],
                             "workbook_id": fetched["workbook_id"]})
                st.success(f"Ready: {fetched['filename']}")
            except Exception as exc:
                st.error(f"Could not fetch workbook: {type(exc).__name__}: {exc}")


def run_migration(source, session, replicate_model=False, emit=None):
    """The five REAL stages, delegating entirely to the existing modules.

    `emit(stage, title, detail)` is called at each genuine stage boundary --
    the caller uses it to drive the live panel. Nothing here sleeps or fakes
    progress. Raises on failure with the stage recorded on the exception so
    the caller can mark exactly which step broke."""
    def step(stage, title, detail):
        if emit:
            emit(stage, title, detail)

    in_sf = session is not None
    workdir = tempfile.mkdtemp(prefix="sis_v2_")
    wb_path = os.path.join(workdir, source["name"])
    with open(wb_path, "wb") as handle:
        handle.write(source["raw"])
    stem = stem_for(source["name"])
    notes = []

    # ---- 1 Discovery ------------------------------------------------------
    step("Discovery", "Discovery started", "Inspecting datasources and connections")
    try:
        discovery = pipeline.onboard(wb_path, source["raw"], in_snowflake=in_sf,
                                     session=session)
    except Exception as exc:
        raise _StageError("Discovery", exc) from exc
    if discovery.get("missing"):
        raise _StageError("Discovery", RuntimeError(
            "These datasources have no data in Snowflake yet: "
            + ", ".join(discovery["missing"])
            + ". Onboard them once from a laptop (python preload_demo.py "
              f"\"{source['name']}\"), then run again."))
    if discovery.get("blocked"):
        notes.append(f"Hyper extract(s) {', '.join(discovery['blocked'])} could not be "
                     "decoded in this environment.")
    for cap, _fq, status, note in (discovery.get("auto_bind_reports") or []):
        if status in ("ambiguous", "mismatch"):
            notes.append(f"{cap} was not auto-bound to an existing table — {note}")

    # Optional scope-B replication: separate tables + a relationship view rather
    # than one flattened extract. Local-connected only (a .hyper cannot decode
    # inside Snowsight), and never fatal.
    model_tables = []
    if replicate_model and in_sf and discovery.get("hyper_paths"):
        try:
            model_tables = pipeline.build_data_model_tables(
                session, discovery["root"], discovery["hyper_paths"])
        except Exception as exc:
            notes.append(f"Data-model replication skipped: {exc}")

    # ---- 2 Parsing --------------------------------------------------------
    step("Parsing", "Parsing started", "Translating workbook XML into the model")
    try:
        ir = TP.build_ir(wb_path)
    except Exception as exc:
        raise _StageError("Parsing", exc) from exc

    # ---- 3 Data model + semantic layer ------------------------------------
    step("Data model", "Data model started", "Reconstructing relationships")
    try:
        model = pipeline.data_model_report(session, discovery["root"])
    except Exception as exc:
        raise _StageError("Data model", exc) from exc
    model_views = []
    for entry in model:
        if entry.get("deployable") and in_sf:
            try:
                model_views.append(pipeline.deploy_model_view(session, entry))
            except Exception as exc:
                notes.append(f"Join view for {entry.get('caption')} did not deploy: {exc}")
    try:
        blends = TP.blends(discovery["root"]) or []
    except Exception:
        blends = []

    mapping = {c: config.DATASOURCES[c] for c in config.DATASOURCES}
    real_cols = CS.introspect_columns_via_session(session, mapping) if in_sf else None
    semantic_ddl = CS.generate_semantic_view(ir, mapping, stem, db=pipeline.LOAD_DB,
                                             schema=pipeline.LOAD_SCHEMA,
                                             real_cols=real_cols)
    metrics, _skipped = CS.build_metrics(ir)
    semantic_view, semantic_state = None, "not generated"
    if "CREATE OR REPLACE SEMANTIC VIEW" in semantic_ddl and metrics:
        # Take the name from the DDL itself rather than rebuilding it here --
        # cortex_semantic normalises the stem, so a guess can silently point
        # Cortex Analyst at a view that does not exist.
        semantic_view = semantic_ddl.split("SEMANTIC VIEW", 1)[1].split("\n", 1)[0].strip()
        semantic_state = "generated"
        if in_sf:
            # The DDL is CREATE **OR REPLACE** SEMANTIC VIEW -- already
            # idempotent and 0 Cortex tokens (pure DDL), so there is no
            # correctness reason to skip it when a same-named view exists.
            # An earlier "skip if exists" gate here (removed 2026-08-06)
            # actively caused a real bug: once the semantic layer generator
            # was fixed to stop dropping certain dimensions (mbar/map/dtbar
            # chart kinds -- see cortex_semantic._field_candidates), any
            # workbook stem that had EVER been migrated before the fix kept
            # "reusing" its stale, pre-fix view forever, because existence
            # alone (not content) gated the redeploy. Always redeploying is
            # the only way a code fix here can ever reach an already-tested
            # workbook without a manual DROP SEMANTIC VIEW.
            existed_before = pipeline.semantic_view_exists(session, semantic_view)
            try:
                session.sql(semantic_ddl).collect()
                semantic_state = "updated" if existed_before else "deployed"
            except Exception as exc:
                semantic_state = "not deployed"
                notes.append(f"Semantic view generated but did not deploy: {exc}")

    # ---- 4 App build ------------------------------------------------------
    step("App build", "App build started", "Generating the Streamlit application")
    try:
        app_code = codegen.build(ir)
    except Exception as exc:
        raise _StageError("App build", exc) from exc
    app_name = f"app_{stem}.py"
    app_path = os.path.join(workdir, app_name)
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write(app_code)

    # ---- 5 Validation -----------------------------------------------------
    step("Validation", "Validation started", "Checking measures against source evidence")
    try:
        validation = parity.check_workbook(ir)
    except Exception as exc:
        raise _StageError("Validation", exc) from exc

    # Real Tableau reference values, when (and only when) the workbook came in
    # over REST -- there is no live view to query for a manual upload.
    conn = st.session_state.get("v2_tableau_conn")
    truth_notes = []
    resolved_truth = {}
    if conn and (validation.get("calc_metrics") or validation.get("measures")):
        try:
            vds_truth, vds_notes = {}, []
            try:
                vds_truth, vds_notes = parity.pull_vds_tableau_truth(
                    conn["server_url"], conn["site_content_url"],
                    list(validation.get("calc_metrics") or []),
                    measure_captions=[m["measure"] for m in validation.get("measures") or []],
                    datasource_hints=[m["datasource"] for m in validation.get("measures") or []])
            except Exception as exc:
                vds_notes = [{"datasource": "-", "error": f"{type(exc).__name__}: {exc}"}]
            live_truth, tnotes = parity.pull_live_tableau_truth(
                conn["server_url"], conn["site_content_url"], conn["workbook_id"],
                list(validation.get("calc_metrics") or [])
                + parity.raw_measure_metrics(validation))
            merged = dict(live_truth)
            merged.update(vds_truth)          # Tableau's own aggregate wins
            if merged:
                parity.apply_live_truth_to_measures(validation, merged)
            truth_notes = (tnotes or []) + (vds_notes or [])
            resolved_truth = merged
        except Exception as exc:
            truth_notes = [{"datasource": "-", "error": f"{type(exc).__name__}: {exc}"}]

    notebook = parity.build_notebook(ir, validation, source["name"])
    # Per-SECTION notebook (Tableau formula <-> app <-> tables, Cortex-judged at
    # notebook-run time). Built here so it reflects the same resolved truth the
    # tables above were re-judged against.
    section_notebook = DV.build_section_notebook(
        ir, validation, source["name"], stem, live_truth=resolved_truth)

    return {
        "name": source["name"],
        "origin": source["origin"],
        "stem": stem,
        "workdir": workdir,
        "wb_path": wb_path,
        "discovery": discovery,
        "ir": ir,
        "model": model,
        "model_views": model_views,
        "model_tables": model_tables,
        "blends": blends,
        "semantic_ddl": semantic_ddl,
        "semantic_view": semantic_view,
        "semantic_state": semantic_state,
        "semantic_metrics": len(metrics),
        "app_name": app_name,
        "app_path": app_path,
        "app_code": app_code,
        "validation": validation,
        "notebook": notebook,
        "section_notebook": section_notebook,
        "truth_notes": truth_notes,
        # Resolved Tableau reference values + the REST connection they came
        # from -- the deep-validation sections (Cortex judging, vision) need
        # both, and re-deriving them there would re-issue REST calls.
        "live_truth": resolved_truth,
        "tableau_conn": conn,
        "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes,
        # config.DATASOURCES is process-global and is repointed per upload by
        # pipeline.onboard. Snapshot it so Preview/Validation reruns render
        # against THIS workbook's routing even after another page touched it.
        "datasources": dict(config.DATASOURCES),
    }


class _StageError(Exception):
    """Carries which of the five stages failed, so the UI can mark that step."""

    def __init__(self, stage, original):
        super().__init__(str(original))
        self.stage = stage
        self.original = original


def page_migrate(session):
    run = active_run()
    render_header(run["name"] if run else "New migration",
                  "Choose one workbook source, run the accelerator, then review each "
                  "result in its own workspace view.")

    if run:
        render_reset_row(run, "migrate")
        render_stepper(done=5, active=0)
        s = run["validation"]["summary"]
        sheets = sum(len(d.get("sheets", [])) for d in run["ir"].get("dashboards", []))
        st.markdown(
            f'<div class="v2-summary">'
            f'<div><span>Dashboards</span><b>{len(run["ir"].get("dashboards", []))}</b></div>'
            f'<div><span>Sheets</span><b>{sheets}</b></div>'
            f'<div><span>Measures passed</span><b>{s["measures_pass"]}/{s["measures_checked"]}</b></div>'
            f'<div><span>Review items</span><b>{s["measures_bug"] + s["calcs_dropped"]}</b></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.success("Migration complete. Use the workspace navigation to inspect the "
                   "inventory, preview, validation evidence and deployment package.")
        for note in run.get("notes") or []:
            st.warning(note)
        nav = st.columns(4)
        for col, target in zip(nav, ["Inventory", "Preview", "Validation", "Deploy & Ask"]):
            with col:
                st.button(target, use_container_width=True, key=f"v2_go_{target}",
                          on_click=queue_page, args=(target,))
        return

    render_stepper(done=0, active=1)
    left, right = st.columns([1.65, 0.75], gap="large")
    with left:
        st.subheader("Select workbook source")
        mode = st.segmented_control(
            "Workbook source", ["Upload file", "Tableau Server / Cloud"],
            default="Upload file", label_visibility="collapsed", key="v2_source_mode")
        if mode == "Tableau Server / Cloud":
            render_tableau_intake()
        else:
            render_upload_intake()
    with right:
        st.subheader("Automation")
        st.markdown('<div class="v2-note"><b>Target platform</b><br>'
                    'Streamlit in Snowflake</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="v2-auto"><div><b>Automatic source resolution</b>'
            '<span>Snowflake objects and the Tableau data model are resolved from '
            'workbook metadata. No table input is required.</span></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="v2-next"><div class="v2-next-title">What happens next</div>'
            '<div class="v2-next-row"><div class="v2-next-icon">01</div>'
            '<div><b>Analyze workbook</b><span>Inventory content and dependencies</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">02</div>'
            '<div><b>Resolve Snowflake model</b><span>Match data and rebuild relationships</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">03</div>'
            '<div><b>Validate generated app</b><span>Check measures before review</span></div></div></div>',
            unsafe_allow_html=True,
        )
        with st.expander("Run options"):
            st.checkbox("Replicate data model as separate tables + view",
                        key="v2_replicate", value=False, disabled=session is None,
                        help="Loads a relationship extract's tables separately and creates "
                             "the join view Tableau's model implies, instead of one "
                             "flattened table. Requires a Snowflake connection and a local "
                             "hyper decode.")
            st.caption("Hyper extracts decode locally. In Snowsight they must already be "
                       "loaded or map to an existing Snowflake table.")

    source = st.session_state.get("v2_source")
    st.markdown('<div class="v2-section"></div>', unsafe_allow_html=True)
    action, note = st.columns([0.25, 0.75])
    with action:
        start = st.button("Run migration", icon=":material/play_arrow:", type="primary",
                          use_container_width=True, disabled=source is None, key="v2_run_btn")
    with note:
        if source:
            st.caption(f"Ready to migrate **{source['name']}** from {source['origin']}.")
        else:
            st.caption("Select or fetch one workbook to continue.")

    if start and source:
        slot = st.empty()
        events = []
        cursor = {"stage": STAGES[0]}

        def emit(stage, title, detail):
            cursor["stage"] = stage
            events.insert(0, (title, detail, ""))
            render_live(slot, source["name"], source["origin"], stage, events)

        try:
            result = run_migration(
                source, session,
                replicate_model=bool(st.session_state.get("v2_replicate")), emit=emit)
        except _StageError as exc:
            events.insert(0, (f"{exc.stage} failed", str(exc), "failed"))
            render_live(slot, source["name"], source["origin"], exc.stage, events,
                        phase="failed")
            st.error(f"Migration stopped in {exc.stage}: "
                     f"{type(exc.original).__name__}: {exc.original}")
            with st.expander("Error details"):
                st.code("".join(traceback.format_exception(
                    type(exc.original), exc.original, exc.original.__traceback__)))
            return
        except Exception as exc:
            events.insert(0, ("Migration failed", str(exc), "failed"))
            render_live(slot, source["name"], source["origin"], cursor["stage"], events,
                        phase="failed")
            st.error(f"Migration stopped: {type(exc).__name__}: {exc}")
            with st.expander("Error details"):
                st.code(traceback.format_exc())
            return

        events.insert(0, ("Migration complete",
                          "Inventory, preview, validation and deployment package ready",
                          "done"))
        render_live(slot, source["name"], source["origin"], STAGES[-1], events, phase="done")
        st.session_state["v2_run"] = result
        queue_page("Inventory")
        st.rerun()


# --------------------------------------------------------------------------- #
# Pages: results
# --------------------------------------------------------------------------- #
def render_reset_row(run, key_suffix):
    """'Working with <workbook>' + a visible reset button. Every page that
    shows an active run gets this -- the sidebar's "Start another migration"
    (under ACTIVE WORKBOOK) is easy to miss, especially once scrolled past,
    so clearing the current workbook to upload/select a different one never
    requires hunting for it, on ANY page that has a run (New migration
    included -- it used to be missing there specifically, the one place
    someone finishing or reviewing a run is most likely to look for it)."""
    label_col, reset_col = st.columns([0.75, 0.25])
    with label_col:
        st.caption(f"Working with **{run['name']}** — {run['origin']}")
    with reset_col:
        st.button("Start new migration", use_container_width=True,
                  key=f"v2_reset_{key_suffix}", on_click=reset_migration,
                  icon=":material/refresh:")


def require_run(title, subtitle):
    render_header(title, subtitle)
    run = active_run()
    if run is None:
        st.markdown('<div class="v2-empty">Run a migration first. Results appear here '
                    'without changing the deployed app.</div>', unsafe_allow_html=True)
        st.button("Go to New migration", type="primary", key=f"v2_empty_{title}",
                  on_click=queue_page, args=("New migration",),
                  icon=":material/play_arrow:")
        return None
    render_reset_row(run, title)
    # Re-point the process-global datasource routing at THIS run's workbook.
    config.DATASOURCES.update(run.get("datasources") or {})
    return run


def page_inventory():
    run = require_run("Workbook inventory",
                      "The migration scope in one place. Open details only when needed.")
    if not run:
        return
    ir = run["ir"]
    sheets = [(d.get("title"), sheet) for d in ir.get("dashboards", [])
              for sheet in d.get("sheets", [])]
    s = run["validation"]["summary"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Datasources", run["discovery"].get("n_datasources", 0))
    c2.metric("Dashboards", len(ir.get("dashboards", [])))
    c3.metric("Sheets", len(sheets))
    c4.metric("Calculations", s["calcs_translated"] + s["calcs_dropped"])

    tabs = st.tabs(["Dashboards", "Data model", "Calculations", "Filters & parameters",
                    "Findings"])
    with tabs[0]:
        st.dataframe(
            [{"Dashboard": dash, "Sheet": sh.get("name"), "Chart": sh.get("kind"),
              "Datasource": sh.get("datasource")} for dash, sh in sheets]
            or [{"Dashboard": "No dashboards found"}],
            use_container_width=True, hide_index=True)
    with tabs[1]:
        # --- Where each datasource actually landed in Snowflake ------------
        # This is the piece that went missing in the V2 rewrite: the pre-V2
        # Stage 1 showed exactly which physical table each Tableau datasource
        # was routed to (or reused, unchanged). Rebuilt here from the real
        # discovery result (load_report -- what was actually loaded THIS run)
        # falling back to the routed table from config.DATASOURCES (a caption
        # can be routed without a fresh load, e.g. a reused existing table).
        st.markdown("**Snowflake landing**")
        load_by_cap = {r[0]: r for r in (run["discovery"].get("load_report") or [])}
        landing_rows = []
        for cap in (run["ir"].get("datasources") or []):
            if cap in load_by_cap:
                _cap, table, rows_loaded, status = load_by_cap[cap]
                landing_rows.append({"Datasource": cap, "Table": table or "—",
                                     "Rows": rows_loaded, "Status": status})
            else:
                table = (run.get("datasources") or {}).get(cap, {}).get("table")
                landing_rows.append({"Datasource": cap, "Table": table or "—",
                                     "Rows": "—", "Status": "routed (no fresh load)"})
        st.dataframe(landing_rows or [{"Datasource": "No datasources found"}],
                     use_container_width=True, hide_index=True)

        # --- Relationship model + the join view each star datasource maps to
        st.markdown("**Data model**")
        deployed_by_cap = {}
        for view_name in run.get("model_views") or []:
            # deploy_model_view() returns a bare view FQN with no datasource
            # label attached -- match it back to its datasource by the same
            # to_phys(caption)+"_MODEL" naming convention semantic_layer.py
            # itself uses, so the table below can show which view belongs to
            # which datasource instead of just a flat success list.
            short = view_name.replace('"', "").split(".")[-1].upper()
            for m in run["model"]:
                if short == (calc_translator.to_phys(m.get("caption") or "") + "_MODEL").upper():
                    deployed_by_cap[m.get("caption")] = view_name
        rows = []
        for m in run["model"]:
            cap = m.get("caption")
            if m.get("n_tables", 0) <= 1:
                view_label = "— (single table, no join)"
            elif cap in deployed_by_cap:
                view_label = f"✅ {deployed_by_cap[cap]}"
            elif m.get("deployable"):
                # Deployable this run but not deployed (e.g. no live session,
                # or scope-B replication wasn't run) -- show the name it
                # WOULD get so it's not just an opaque checkbox.
                candidate = (f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}."
                            f"{calc_translator.to_phys(cap or '')}_MODEL")
                view_label = f"not deployed — would be {candidate}"
            else:
                view_label = "not deployable this run"
            rows.append({"Datasource": cap,
                        "Shape": str(m.get("shape") or "").replace("_", "-"),
                        "Tables": m.get("n_tables"),
                        "Relationships": len(m.get("joins") or []),
                        "Join view": view_label})
        st.dataframe(rows or [{"Datasource": "No relationship model detected"}],
                     use_container_width=True, hide_index=True)
        joins = [{"Datasource": m.get("caption"), "Left": j["left"], "Left key": j["lkey"],
                  "Right": j["right"], "Right key": j["rkey"]}
                 for m in run["model"] for j in (m.get("joins") or [])]
        if joins:
            with st.expander(f"Relationship detail ({len(joins)})"):
                st.dataframe(joins, use_container_width=True, hide_index=True)
        for blend in run.get("blends") or []:
            links = ", ".join(f"{l['primary_field']} = {l['secondary_field']}"
                              for l in blend["links"]) or "—"
            st.warning(f"**Data blend** — `{blend['primary']}` is blended with "
                       f"`{blend['secondary']}` on {links}. Tableau links these at query "
                       "time and aggregates the secondary to the link grain, so it is not "
                       "a row-level join.")
        st.caption("The model was inferred from workbook metadata and the Snowflake "
                   "catalog. No table selection was required.")

        # --- Cortex semantic layer (the OTHER thing that went missing) -----
        st.markdown("**Cortex semantic layer**")
        view = run.get("semantic_view")
        state = run.get("semantic_state")
        if not view:
            st.caption("No metrics/dimensions resolved to expose — semantic layer "
                       "skipped (nothing for Cortex Analyst to query here).")
        else:
            icon = {"deployed": "✅", "updated": "✅", "not deployed": "⚠️"}.get(state, "")
            st.markdown(f"{icon} `{view}` — **{state}** "
                       f"({run.get('semantic_metrics', 0)} metric(s))")
            if state == "updated":
                st.caption("A semantic view already existed under this name — replaced "
                           "with the current definition (CREATE OR REPLACE), so a fix or "
                           "workbook change always reaches Cortex Analyst.")
            elif state == "not deployed":
                st.caption("Generated but did not deploy — see the run notes above, or "
                           "the downloaded SQL in Deploy & Ask.")
    with tabs[2]:
        translated = [{"Calculation": cap, "Status": "Translated",
                       "Detail": value.get("sql") if isinstance(value, dict) else str(value)}
                      for cap, value in (ir.get("calcs") or {}).items()]
        dropped = [{"Calculation": cap, "Status": "Review", "Detail": formula}
                   for cap, formula in (ir.get("calc_drops") or {}).items()]
        st.dataframe(translated + dropped or [{"Calculation": "No calculations"}],
                     use_container_width=True, hide_index=True)
    with tabs[3]:
        # IR shapes (verified against tableau_parser): a sheet filter is
        # {caption, kind}; ir["params"] is a {name: default} MAPPING, not a
        # list; allowed values live in ir["param_domains"][name].
        filters = [{"Dashboard": dash, "Sheet": sh.get("name"),
                    "Field": f.get("caption"), "Kind": f.get("kind")}
                   for dash, sh in sheets for f in (sh.get("filters") or [])
                   if isinstance(f, dict)]
        st.markdown("**Filters**")
        st.dataframe(filters or [{"Filter": "No sheet filters detected"}],
                     use_container_width=True, hide_index=True)
        st.markdown("**Parameters**")
        domains = ir.get("param_domains") or {}
        params = [{"Parameter": name, "Default": default,
                   "Allowed values": ", ".join(str(v) for v in domains[name])
                   if isinstance(domains.get(name), list) else "any"}
                  for name, default in (ir.get("params") or {}).items()]
        st.dataframe(params or [{"Parameter": "No parameters"}],
                     use_container_width=True, hide_index=True)
    with tabs[4]:
        issues = [{"Severity": "Review", "Area": "Calculation", "Item": cap, "Detail": formula}
                  for cap, formula in (ir.get("calc_drops") or {}).items()]
        issues += [{"Severity": "Blocked", "Area": "Datasource", "Item": cap,
                    "Detail": "Hyper extract could not be decoded in this environment"}
                   for cap in (run["discovery"].get("blocked") or [])]
        issues += [{"Severity": "Note", "Area": "Run", "Item": "-", "Detail": note}
                   for note in (run.get("notes") or [])]
        if issues:
            st.dataframe(issues, use_container_width=True, hide_index=True)
        else:
            st.success("No blocking inventory findings.")


def render_dashboard_preview(ir):
    """One st.tabs() tab per Tableau dashboard -- matches how Tableau itself
    presents multiple dashboards, and how the pre-V2 app rendered them.
    Rendered eagerly (not lazily behind a selectbox) so every dashboard's
    render errors are isolated and visible without having to select each one
    in turn."""
    findings.clear()
    engine.configure(ir)
    for cap, formula in (ir.get("calc_drops") or {}).items():
        findings.record("WARNING", "(workbook)", "calc-untranslated",
                        f"Calculated field '{cap}' not translated: "
                        f"{formula.strip()[:120]}")
    try:
        engine._render_param_controls()
    except Exception as exc:
        findings.record("WARNING", "(parameters)", "param-controls-failed",
                        f"{type(exc).__name__}: {exc}")
    dashboards = ir.get("dashboards") or []
    if not dashboards:
        st.warning("No dashboards found in this workbook.")
        return
    tabs = st.tabs([d["title"] for d in dashboards])
    for tab, dashboard in zip(tabs, dashboards):
        with tab:
            try:
                engine.render_dashboard(dashboard)
            except Exception as exc:
                findings.record("BLOCKER", dashboard["name"], "dashboard-failed",
                                f"{type(exc).__name__}: {exc}")
                st.error(f"'{dashboard['title']}' could not render ({exc}).")
                with st.expander("Error details"):
                    st.code(traceback.format_exc())
    engine._render_findings()


def page_preview():
    run = require_run("App preview",
                      "Review the converted experience on its own, separate from "
                      "pipeline logs and validation evidence.")
    if not run:
        return
    left, right = st.columns([0.72, 0.28])
    with left:
        # The generated app is a thin runtime wrapper around a single embedded
        # IR literal, so a line count understates it badly -- report real size.
        st.caption(f"Generated application: **{run['app_name']}** "
                   f"({len(run['app_code']):,} bytes, deterministic source)")
    with right:
        st.download_button("Download app source", run["app_code"], file_name=run["app_name"],
                           mime="text/x-python", icon=":material/download:",
                           use_container_width=True)
    render_dashboard_preview(run["ir"])


def page_validation():
    run = require_run("Validation",
                      "The verdict first. Detailed computation evidence stays available below.")
    if not run:
        return
    result = run["validation"]
    s = result["summary"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Measures passed", f"{s['measures_pass']}/{s['measures_checked']}")
    c2.metric("Measures with bug", s["measures_bug"])
    c3.metric("Calculations translated", s["calcs_translated"])
    if s["measures_bug"] == 0:
        st.success("Validation passed. No numerical mismatches were detected.")
    else:
        st.warning(f"{s['measures_bug']} result(s) require review before deployment.")

    issues = []
    for row in result.get("measures") or []:
        if row["verdict"] != "PASS":
            issues.append({"Area": "Measure", "Datasource": row["datasource"],
                           "Item": row["measure"], "Verdict": row["verdict"],
                           "App": row["app"], "Source": row["source"],
                           "Tableau": row.get("tableau")})
    for row in result.get("calc_metrics") or []:
        if row["verdict"] not in ("PASS", "EXECUTED"):
            issues.append({"Area": "Calculation", "Datasource": row["datasource"],
                           "Item": row["metric"], "Verdict": row["verdict"],
                           "App": row.get("value"), "Source": None,
                           "Tableau": row.get("tableau_bound")})
    st.subheader("Review queue")
    if issues:
        st.dataframe(issues, use_container_width=True, hide_index=True)
    else:
        st.caption("Nothing requires action.")

    if run.get("truth_notes"):
        with st.expander("Tableau reference pull notes"):
            st.dataframe(run["truth_notes"], use_container_width=True, hide_index=True)
    elif run["origin"] == "File upload":
        st.caption("Tableau reference values are pulled over REST only when the workbook is "
                   "fetched from Tableau Server/Cloud — an uploaded file has no live view "
                   "to query.")

    with st.expander("All measure evidence"):
        st.dataframe(result.get("measures") or [{"Measure": "None"}],
                     use_container_width=True, hide_index=True)
    with st.expander("Calculated-field evidence"):
        st.dataframe(result.get("calc_metrics") or [{"Calculation": "None"}],
                     use_container_width=True, hide_index=True)
    with st.expander("Datasource row-count evidence"):
        st.dataframe(result.get("datasources") or [{"Datasource": "None"}],
                     use_container_width=True, hide_index=True)
    # parity.build_notebook already returns serialized .ipynb JSON -- passing it
    # through json.dumps again would wrap it in a string literal and produce a
    # file Jupyter cannot open.
    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button("Download validation notebook", run["notebook"],
                           file_name=f"{run['stem']}_validation.ipynb",
                           mime="application/x-ipynb+json",
                           icon=":material/download:", use_container_width=True)
    with dl2:
        if run.get("section_notebook"):
            st.download_button(
                "Download SECTION validation notebook", run["section_notebook"],
                file_name=f"{run['stem']}_section_validation.ipynb",
                mime="application/x-ipynb+json", icon=":material/download:",
                use_container_width=True,
                help="Each migrated metric against its Tableau TWB formula and "
                     "known figure — Cortex decides each verdict when the "
                     "notebook runs; the deterministic check is kept as a "
                     "labeled cross-check.")

    # ---- Deep validation: the heavyweight proof machinery ------------------
    # On-demand (runs a live Snowflake query per dashboard), so nothing here
    # fires automatically. 2026-08: consolidated onto the single proof-first
    # pack (deep_validation.render_proof_first_validation) -- replaces the
    # older three-panel UI (Cortex per-metric judge, skill-methodology
    # Cortex-narrated write-up, Cortex vision comparison), which is fully
    # deterministic (0 Cortex tokens) and was judged the clearer surface.
    session, _label = resolve_session()
    st.markdown('<div class="v2-section"></div>', unsafe_allow_html=True)
    st.subheader("Deep validation")
    st.caption("Runs a live Snowflake query per dashboard plus real Tableau/"
               "Streamlit evidence pulls, so it's click-gated rather than "
               "automatic. Fully deterministic -- no Cortex tokens spent.")

    with st.expander("Proof-first validation", expanded=True):
        DV.render_proof_first_validation(
            run["ir"], run["validation"], session, run["name"], run["stem"],
            conn=run.get("tableau_conn"))
    with st.expander("Migration report (structured + PDF)"):
        # The carried report functions read a pre-V2 run shape
        # (ir / parity / workbook / app_name / target / ts) -- adapt rather
        # than rename keys in the workbench run, so deep_validation.py stays
        # a verbatim copy of the code that already proved itself.
        DV.render_migration_report(
            {"ir": run["ir"], "parity": run["validation"],
             "workbook": run["name"], "app_name": run["app_name"],
             "stem": run["stem"], "ts": run["ts"],
             "target": f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}"},
            run.get("model") or [])


# --------------------------------------------------------------------------- #
# Cortex Analyst
# --------------------------------------------------------------------------- #
def _maybe_json(value):
    """Cortex Analyst's REST bridge has been observed nesting a JSON-encoded
    STRING one level deeper than expected (e.g. `message.content` itself a
    JSON string rather than an already-decoded list) depending on account/
    runtime version. Try to decode a string once; on any failure, or for a
    non-string, return it unchanged rather than raising."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def parse_analyst(data):
    """Extract (sql, text) from a Cortex Analyst response payload. Tolerant of
    the message being at the top level or under 'message', and of 'content'
    arriving as an already-decoded list OR a JSON string of one -- the exact
    shape has varied by account/runtime, so every level is defensively
    unwrapped rather than assumed."""
    sql, text = None, []
    data = _maybe_json(data)
    if not isinstance(data, dict):
        return None, ""
    msg = _maybe_json(data.get("message", data))
    if not isinstance(msg, dict):
        return None, ""
    content = _maybe_json(msg.get("content", []))
    if not isinstance(content, list):
        content = []
    for item in content:
        item = _maybe_json(item)
        if not isinstance(item, dict):
            continue
        if item.get("type") == "sql":
            sql = item.get("statement") or item.get("sql")
        elif item.get("type") == "text":
            text.append(item.get("text", ""))
    return sql, "\n\n".join(value for value in text if value)


def cortex_analyst(session, semantic_view, question):
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
            "semantic_view": semantic_view}
    try:
        import _snowflake                       # SiS-only in-app REST bridge
        response = _snowflake.send_snow_api_request(
            "POST", "/api/v2/cortex/analyst/message", {}, {}, body, None, 30000)
        status = response.get("status") if isinstance(response, dict) else None
        raw = response.get("content") if isinstance(response, dict) else response
        if status is not None and status >= 400:
            # `content` on an error response is a plain message, not the
            # Analyst payload shape -- surface it directly rather than trying
            # (and failing) to parse it as one.
            raise RuntimeError(f"Cortex Analyst returned HTTP {status}: {raw}")
        parsed = _maybe_json(raw)
        sql, text = parse_analyst(parsed)
        if sql is None and not text:
            # Parsed without error but found nothing recognizable -- this is
            # exactly the "shape drifted again" case; show the raw payload
            # (truncated) so it's diagnosable instead of silently empty.
            snippet = json.dumps(parsed)[:400] if isinstance(parsed, (dict, list)) else str(parsed)[:400]
            raise RuntimeError(f"Unrecognized Cortex Analyst response shape: {snippet}")
        return sql, text
    except ModuleNotFoundError:
        pass
    connection = getattr(session, "connection", None)
    host = getattr(connection, "host", None)
    token = getattr(getattr(connection, "rest", None), "token", None)
    if not (host and token):
        raise RuntimeError("No Cortex Analyst REST session is available from here.")
    import urllib.request
    request = urllib.request.Request(
        f"https://{host}/api/v2/cortex/analyst/message", data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f'Snowflake Token="{token}"',
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return parse_analyst(json.loads(response.read()))


def page_deploy(session):
    run = require_run("Deploy & Ask",
                      "Deployment stays human-gated. Cortex Analyst becomes available "
                      "once the semantic view exists.")
    if not run:
        return
    s = run["validation"]["summary"]
    left, right = st.columns([1, 1], gap="large")

    with left:
        st.subheader("Deployment package")
        st.dataframe(
            [{"Item": "Application", "Value": run["app_name"]},
             {"Item": "Target", "Value": f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}"},
             {"Item": "Validation", "Value": f"{s['measures_pass']}/{s['measures_checked']} passed"},
             {"Item": "Semantic view", "Value": run.get("semantic_view") or "not generated"},
             {"Item": "Deployment", "Value": "Human approval required"}],
            use_container_width=True, hide_index=True)

        blocked = s["measures_bug"] > 0
        override = False
        if blocked:
            override = st.checkbox(
                f"Deploy anyway — {s['measures_bug']} open review item(s)",
                key="v2_override",
                help="Validation is a gate, not a hard block. Ticking this records that a "
                     "human accepted the open items.")
        deploy = st.button("Deploy to Snowflake", icon=":material/rocket_launch:",
                           type="primary", use_container_width=True, key="v2_deploy",
                           disabled=session is None or (blocked and not override))
        if session is None:
            st.caption("Connect Snowflake to enable deployment.")
        elif blocked and not override:
            st.caption("Resolve the review queue, or accept the open items above.")
        if deploy:
            try:
                with st.spinner("Staging runtime files and creating the Streamlit app..."):
                    result = pipeline.deploy_streamlit_app(session, run["stem"], run["app_path"])
                st.session_state["v2_deployment"] = result
            except Exception as exc:
                st.error(f"Deployment failed: {type(exc).__name__}: {exc}")
        done = st.session_state.get("v2_deployment")
        if done:
            st.success(f"Deployed {done['identifier']}")
            if done.get("url"):
                st.link_button("Open in Snowsight", done["url"],
                               icon=":material/open_in_new:", use_container_width=True)

        st.download_button("Download semantic model SQL", run["semantic_ddl"],
                           file_name=f"{run['stem']}_semantic_view.sql", mime="text/plain",
                           icon=":material/download:", use_container_width=True)

    with right:
        st.subheader("Ask your data")
        view = run.get("semantic_view")
        state = run.get("semantic_state")
        if not view:
            st.info("No semantic view was generated for this workbook — there were no "
                    "metrics to expose, so there is nothing for Cortex Analyst to query.")
            return
        st.caption(f"Semantic view: `{view}` — {state}.")
        if state not in ("deployed", "updated"):
            st.warning("The semantic view has not been deployed to Snowflake yet, so "
                       "Cortex Analyst cannot answer against it. Connect Snowflake and "
                       "re-run the migration, or deploy the downloaded SQL manually.")
        with st.form("v2_analyst_form"):
            question = st.text_input("Question",
                                     placeholder="Total sales by region last quarter")
            ask = st.form_submit_button("Ask Cortex Analyst", icon=":material/auto_awesome:",
                                        use_container_width=True, disabled=session is None)
        if session is None:
            st.caption("Connect Snowflake to use Cortex Analyst.")
        if ask and question.strip():
            try:
                with st.spinner("Asking Cortex Analyst..."):
                    sql, answer = cortex_analyst(session, view, question)
                if answer:
                    st.write(answer)
                if sql:
                    st.code(sql, language="sql")
                    try:
                        st.dataframe(session.sql(sql).to_pandas(),
                                     use_container_width=True, hide_index=True)
                    except Exception as exc:
                        st.warning(f"Analyst returned SQL but it did not run: {exc}")
                if not (sql or answer):
                    st.info("Cortex Analyst returned no answer for that question.")
            except Exception as exc:
                st.warning(f"Cortex Analyst is not reachable from here: {exc}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    inject_styles()
    pending = st.session_state.pop("v2_pending_page", None)
    if pending in PAGES:
        st.session_state["v2_page"] = pending
    elif st.session_state.get("v2_page") not in (None, *PAGES):
        st.session_state["v2_page"] = "Overview"

    session, session_label = resolve_session()
    backend.set_session(session)
    page = sidebar(session, session_label)

    if page == "Overview":
        page_overview()
    elif page == "New migration":
        page_migrate(session)
    elif page == "Inventory":
        page_inventory()
    elif page == "Preview":
        page_preview()
    elif page == "Validation":
        page_validation()
    else:
        page_deploy(session)


# Streamlit executes the entry script with __name__ == "__main__". Guarding the
# call keeps the module importable (for tests / harnesses) without rendering the
# whole app as a side effect of the import.
if __name__ == "__main__":
    main()

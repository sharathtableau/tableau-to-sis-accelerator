"""Compact V2 workbench for the Tableau to SiS accelerator.

This is a separate review entry point. It does not import or modify
pipeline_app.py, and it is not included in snowflake.yml.

Run locally:
    streamlit run pipeline_app_v2.py --server.port 8520
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import traceback
from collections import Counter

import pandas as pd
import streamlit as st

import backend
import codegen
import config
import cortex_semantic as CS
import engine
import findings
import parity
import pipeline
import semantic_layer as SL
import tableau_parser as TP
import tableau_server as TS


st.set_page_config(
    page_title="Tableau to SiS Workbench",
    page_icon=":material/swap_horiz:",
    layout="wide",
    initial_sidebar_state="auto",
)


STAGES = ["Discovery", "Parsing", "Data model", "App build", "Validation"]
PAGES = ["Overview", "New migration", "Inventory", "Preview", "Validation", "Deploy & Ask"]


def inject_styles():
    st.markdown(
        """
        <style>
        :root {
            --ink: #102a43;
            --muted: #62788a;
            --line: #d6e6ef;
            --soft: #f2f8fb;
            --blue: #29b5e8;
            --blue-dark: #11567f;
            --navy: #071b2e;
            --navy-soft: #0c2941;
            --green: #14a07a;
            --amber: #e5a836;
            --red: #d45555;
        }
        .stApp { background: #f6fafc; color: var(--ink); }
        .block-container { max-width: 1480px; padding-top: 1.3rem; padding-bottom: 3rem; }
        section[data-testid="stSidebar"] { border-right: 0; background: var(--navy); }
        section[data-testid="stSidebar"] .block-container { padding-top: 1.25rem; }
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] label { color: #b9cedd !important; }
        section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#7fa0b8 !important; }
        section[data-testid="stSidebar"] hr { border-color:#1c3b53; }
        section[data-testid="stSidebar"] div[role="radiogroup"] label {
            border-radius: 6px;
            padding: 7px 9px;
            margin-bottom: 3px;
            transition: background .15s ease;
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {
            background: #123b58;
            box-shadow: inset 3px 0 0 var(--blue);
        }
        section[data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {
            color:#ffffff !important;
            font-weight:700;
        }
        section[data-testid="stSidebar"] div[data-testid="stExpander"] {
            border-color:#26465e;
            background:#0a2338;
        }
        h1, h2, h3, h4 { color: var(--ink); letter-spacing: 0; }
        h1 { font-size: 1.65rem !important; line-height: 1.2 !important; margin-bottom: .25rem !important; }
        h2 { font-size: 1.25rem !important; }
        h3 { font-size: 1rem !important; }
        p, label, [data-testid="stCaptionContainer"] { color: var(--muted); }
        div[data-testid="stMetric"] {
            border: 1px solid var(--line);
            border-radius: 6px;
            background: #fff;
            padding: .8rem 1rem;
            box-shadow: 0 5px 18px rgba(17,86,127,.06);
        }
        div[data-testid="stMetric"] label { font-size: .76rem; }
        div[data-testid="stMetricValue"] { font-size: 1.45rem; }
        div[data-testid="stFileUploaderDropzone"] {
            min-height: 155px;
            border: 1.5px dashed #67c5e8;
            border-radius: 6px;
            background: #f7fcfe;
            padding: 1.6rem 1.2rem;
        }
        div[data-testid="stFileUploaderDropzone"] svg { color:var(--blue); width:32px; height:32px; }
        div[data-testid="stFileUploaderDropzone"] small { color:var(--muted); }
        button[kind="primary"] { border-radius: 5px; background:var(--blue-dark); border-color:var(--blue-dark); font-weight:700; }
        button[kind="primary"]:hover { background:#0d486c; border-color:#0d486c; }
        button[kind="secondary"] { border-radius: 5px; border-color:#bfd4e1; }
        div[data-testid="stExpander"] { border: 1px solid var(--line); border-radius: 6px; }
        div[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
        .v2-brand { display:flex; align-items:center; gap:11px; font-weight:800; color:#fff; margin-bottom:1.25rem; letter-spacing:0; }
        .v2-brand-text { color:#fff !important; line-height:1.05; }
        .v2-brand-text small { display:block; color:#6dcdf0 !important; font-size:.62rem; text-transform:uppercase; margin-top:5px; letter-spacing:.08em; }
        .v2-mark { position:relative; width:24px; height:24px; display:block; }
        .v2-mark i { position:absolute; left:10px; top:2px; width:4px; height:20px; border-radius:2px; background:var(--blue); transform-origin:center; }
        .v2-mark i:nth-child(2){transform:rotate(60deg)}.v2-mark i:nth-child(3){transform:rotate(120deg)}
        .v2-mark i:nth-child(4){display:none}
        .v2-topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; border:1px solid var(--line); border-top:4px solid var(--blue); border-radius:8px; padding:17px 19px; margin-bottom:1.2rem; background:#fff; box-shadow:0 8px 26px rgba(17,86,127,.07); }
        .v2-topbar p { margin:.2rem 0 0; font-size:.9rem; }
        .v2-state { white-space:nowrap; border:1px solid #b8dfd2; color:#087254; background:#edf9f5; padding:7px 11px; border-radius:999px; font-size:.72rem; font-weight:800; text-transform:uppercase; }
        .v2-state.neutral { color:var(--blue-dark); border-color:#bcddec; background:#eef8fc; }
        .v2-stepper { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); border:1px solid var(--line); border-radius:7px; overflow:hidden; margin:.7rem 0 1.2rem; background:#fff; box-shadow:0 6px 20px rgba(17,86,127,.05); }
        .v2-step { position:relative; padding:11px 12px 10px 42px; min-height:50px; border-right:1px solid var(--line); background:#fff; }
        .v2-step:last-child { border-right:0; }
        .v2-step b { display:block; font-size:.78rem; color:var(--ink); }
        .v2-step span { font-size:.69rem; color:var(--muted); }
        .v2-step i { position:absolute; left:12px; top:12px; display:flex; align-items:center; justify-content:center; width:21px; height:21px; border-radius:50%; background:#e7ebef; color:#64717d; font-style:normal; font-size:.68rem; font-weight:800; }
        .v2-step.active { box-shadow:inset 0 3px 0 var(--blue), 0 0 0 1px rgba(41,181,232,.12); background:#edf9fd; }
        .v2-step.active i { background:var(--blue-dark); color:#fff; box-shadow:0 0 0 5px rgba(41,181,232,.14); }
        .v2-step.done { box-shadow:inset 0 3px 0 var(--green); }
        .v2-step.done i { background:#dff3eb; color:var(--green); }
        .v2-live-progress { border:1px solid var(--line); border-radius:7px; padding:15px 16px; margin:12px 0; background:#fff; box-shadow:0 7px 22px rgba(17,86,127,.06); }
        .v2-live-head { display:flex; justify-content:space-between; gap:15px; margin-bottom:9px; font-size:.78rem; }
        .v2-live-head b { color:var(--ink); }
        .v2-live-head span { color:var(--blue-dark); font-weight:700; }
        .v2-live-dot { display:inline-block; width:8px; height:8px; margin-right:7px; border-radius:50%; background:var(--blue); animation:v2Pulse 1.25s ease-in-out infinite; }
        .v2-progress-track { height:8px; overflow:hidden; border-radius:999px; background:#e4eef3; }
        .v2-progress-fill { height:100%; border-radius:999px; background:linear-gradient(90deg,var(--blue-dark),var(--blue),var(--green)); background-size:180% 100%; transition:width .55s ease; animation:v2Flow 1.4s linear infinite; }
        .v2-live-stages { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:7px; margin-top:11px; }
        .v2-live-stage { padding:6px 7px; border-radius:4px; background:#edf2f5; color:#8295a2; text-align:center; font-size:.65rem; }
        .v2-live-stage.done { background:#e5f6ef; color:#087254; font-weight:700; }
        .v2-live-stage.active { background:#e1f4fb; color:var(--blue-dark); font-weight:800; box-shadow:inset 0 -2px var(--blue); }
        @keyframes v2Pulse { 0%,100%{opacity:.35;transform:scale(.85)} 50%{opacity:1;transform:scale(1.15)} }
        @keyframes v2Flow { from{background-position:100% 0} to{background-position:-80% 0} }
        .v2-section { border-top:1px solid var(--line); padding-top:1rem; margin-top:1rem; }
        .v2-callout { border-left:4px solid var(--blue); background:#eaf7fc; padding:11px 13px; color:#294a60; font-size:.86rem; margin:.7rem 0 1rem; }
        .v2-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:.75rem 0 1.1rem; }
        .v2-summary div { border:1px solid var(--line); border-radius:6px; padding:10px 12px; }
        .v2-summary span { display:block; font-size:.7rem; color:var(--muted); text-transform:uppercase; }
        .v2-summary b { display:block; font-size:1.05rem; margin-top:3px; color:var(--ink); }
        .v2-empty { border:1px dashed #aecbdc; background:#fff; border-radius:7px; padding:30px; text-align:center; color:var(--muted); }
        .v2-kicker { color:var(--blue-dark); font-size:.7rem; font-weight:800; text-transform:uppercase; margin-bottom:.25rem; letter-spacing:.06em; }
        .v2-sidebar-note { font-size:.72rem; color:#7fa0b8; border-top:1px solid #1c3b53; padding-top:.9rem; margin-top:1rem; }
        .v2-product-flow { display:grid; grid-template-columns:1fr 44px 1.08fr 44px 1fr; align-items:center; border-radius:8px; overflow:hidden; margin:1rem 0 1.5rem; background:var(--navy); border:1px solid #143a55; box-shadow:0 15px 38px rgba(7,27,46,.18); }
        .v2-product-flow section { min-height:132px; padding:22px; background:transparent; border-top:3px solid transparent; }
        .v2-product-flow section:nth-of-type(1) { border-color:#6dcdf0; }
        .v2-product-flow section:nth-of-type(2) { background:var(--navy-soft); border-color:var(--blue); }
        .v2-product-flow section:nth-of-type(3) { border-color:#38c89b; }
        .v2-product-flow small { display:block; color:#6dcdf0; font-size:.66rem; text-transform:uppercase; font-weight:800; margin-bottom:9px; letter-spacing:.08em; }
        .v2-product-flow b { display:block; color:#fff; font-size:1.05rem; margin-bottom:7px; }
        .v2-product-flow span { color:#a9c1d1; font-size:.79rem; line-height:1.5; }
        .v2-product-flow i { text-align:center; color:var(--blue); font-style:normal; font-size:1.3rem; }
        .v2-capabilities { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:20px; margin:1rem 0 1.4rem; }
        .v2-capability { border:1px solid var(--line); border-top:4px solid var(--cap-color); border-radius:7px; padding:16px; background:#fff; box-shadow:0 7px 22px rgba(17,86,127,.06); }
        .v2-capability h3 { margin:0 0 9px; font-size:.95rem !important; }
        .v2-capability p { margin:0 0 7px; font-size:.8rem; line-height:1.45; }
        .v2-auto-note { display:flex; gap:12px; align-items:flex-start; border:1px solid #b8dfd2; background:#edf9f5; border-radius:7px; padding:13px 15px; margin:1rem 0; }
        .v2-auto-note b { display:block; color:#087254; font-size:.85rem; }
        .v2-auto-note span { display:block; color:#4d7164; font-size:.78rem; margin-top:3px; }
        .v2-next { border:1px solid var(--line); border-radius:7px; background:#fff; overflow:hidden; margin-top:12px; }
        .v2-next-title { padding:11px 13px; border-bottom:1px solid var(--line); color:var(--ink); font-size:.79rem; font-weight:800; }
        .v2-next-row { display:grid; grid-template-columns:30px 1fr; gap:9px; align-items:center; padding:10px 12px; border-bottom:1px solid #e7eff3; }
        .v2-next-row:last-child { border-bottom:0; }
        .v2-next-icon { display:flex; width:28px; height:28px; align-items:center; justify-content:center; border-radius:6px; background:#e8f7fc; color:var(--blue-dark); font-size:.68rem; font-weight:900; }
        .v2-next-row:nth-child(3) .v2-next-icon { background:#edf0fb; color:#4e60a8; }
        .v2-next-row:nth-child(4) .v2-next-icon { background:#e8f7f1; color:#087254; }
        .v2-next-row b { display:block; color:var(--ink); font-size:.75rem; }
        .v2-next-row span { display:block; color:var(--muted); font-size:.68rem; margin-top:2px; }
        .v2-trust-strip { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); border-top:1px solid var(--line); border-bottom:1px solid var(--line); margin:1.2rem 0 1.5rem; background:#fff; }
        .v2-trust-strip div { padding:13px 16px; border-right:1px solid var(--line); }
        .v2-trust-strip div:last-child { border-right:0; }
        .v2-trust-strip b { display:block; color:var(--blue-dark); font-size:.78rem; }
        .v2-trust-strip span { color:var(--muted); font-size:.7rem; }
        @media(max-width:900px){
            .v2-stepper,.v2-summary,.v2-capabilities,.v2-trust-strip{grid-template-columns:1fr 1fr}.v2-step{border-bottom:1px solid var(--line)}.v2-live-stages{grid-template-columns:1fr 1fr}
            .v2-product-flow{grid-template-columns:1fr}.v2-product-flow i{padding:5px;transform:rotate(90deg)}
            .v2-topbar{display:block}.v2-state{display:inline-block;margin-top:.6rem}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def esc(value):
    import html
    return html.escape(str(value if value is not None else "-"), quote=True)


def stem_for(name):
    return re.sub(r"[^0-9A-Za-z]+", "_", os.path.splitext(name)[0]).strip("_").lower() or "workbook"


def active_run():
    return st.session_state.get("v2_run")


def queue_page(page):
    """Apply navigation before the radio widget is created on the next rerun."""
    st.session_state["v2_pending_page"] = page


def reset_migration():
    for key in ["v2_run", "v2_source", "v2_fetched", "v2_tableau_conn"]:
        st.session_state.pop(key, None)
    queue_page("New migration")


def render_header(title, subtitle):
    run = active_run()
    state = "Ready for migration"
    klass = "neutral"
    if run:
        state = "Validation passed" if run["validation"]["summary"]["measures_bug"] == 0 else "Review required"
        klass = "" if run["validation"]["summary"]["measures_bug"] == 0 else "neutral"
    st.markdown(
        f"""
        <div class="v2-topbar">
          <div><div class="v2-kicker">Tableau to Streamlit in Snowflake</div><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>
          <div class="v2-state {klass}">{esc(state)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stepper(done=0, active=1):
    parts = []
    for idx, name in enumerate(STAGES, 1):
        if idx <= done:
            cls, marker, note = "done", "OK", "Complete"
        elif idx == active:
            cls, marker, note = "active", str(idx), "Current"
        else:
            cls, marker, note = "", str(idx), "Pending"
        parts.append(
            f'<div class="v2-step {cls}"><i>{marker}</i><b>{esc(name)}</b><span>{note}</span></div>'
        )
    st.markdown(f'<div class="v2-stepper">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_live_progress(workbook, current_stage, slot):
    current = STAGES.index(current_stage)
    percent = int(((current + 0.45) / len(STAGES)) * 100)
    stages = []
    for index, name in enumerate(STAGES):
        klass = "done" if index < current else "active" if index == current else ""
        stages.append(f'<div class="v2-live-stage {klass}">{esc(name)}</div>')
    slot.markdown(
        f'<div class="v2-live-progress"><div class="v2-live-head"><b>{esc(workbook)}</b>'
        f'<span><i class="v2-live-dot"></i>{esc(current_stage)} in progress</span></div>'
        f'<div class="v2-progress-track"><div class="v2-progress-fill" style="width:{percent}%"></div></div>'
        f'<div class="v2-live-stages">{"".join(stages)}</div></div>',
        unsafe_allow_html=True,
    )


def resolve_session():
    hosted = pipeline.get_session()
    if hosted is not None:
        return hosted, "Snowflake connected"
    session = st.session_state.get("v2_sf_session")
    return session, "Snowflake connected" if session is not None else "Snowflake target"


def sidebar(session_label):
    with st.sidebar:
        st.markdown(
            '<div class="v2-brand"><span class="v2-mark"><i></i><i></i><i></i><i></i></span>'
            '<span class="v2-brand-text">Tableau to SiS<small>Migration workbench</small></span></div>',
            unsafe_allow_html=True,
        )
        page = st.radio("Navigation", PAGES, label_visibility="collapsed", key="v2_page")
        st.markdown("---")
        st.caption("TARGET PLATFORM")
        st.write("**Snowflake**")
        st.caption("Workbook datasources and existing Snowflake objects are resolved automatically.")
        if pipeline.get_session() is None:
            with st.expander("Local developer connection"):
                conn = st.text_input("Snow CLI connection", value="wbr", key="v2_conn")
                if st.button("Connect", icon=":material/link:", use_container_width=True, key="v2_connect"):
                    try:
                        with st.spinner("Connecting..."):
                            st.session_state["v2_sf_session"] = pipeline.snow_session(conn)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        run = active_run()
        if run:
            st.markdown("---")
            st.caption("ACTIVE WORKBOOK")
            st.write(f"**{run['name']}**")
            s = run["validation"]["summary"]
            st.caption(f"{len(run['ir'].get('dashboards', []))} dashboards | {s['measures_pass']}/{s['measures_checked']} measures")
            st.button(
                "Start another migration", use_container_width=True,
                key="v2_reset", on_click=reset_migration, icon=":material/refresh:",
            )
        st.markdown('<div class="v2-sidebar-note">Separate V2 review build. The deployed staged demo is unchanged.</div>', unsafe_allow_html=True)
    return page


def page_overview(session_label):
    render_header(
        "Tableau to SiS Accelerator",
        "Modernize Tableau workbooks into governed Streamlit in Snowflake applications, with automated discovery, conversion, and validation.",
    )
    st.markdown(
        """
        <div class="v2-product-flow">
          <section><small>Source</small><b>Tableau workbook</b><span>Upload a TWB/TWBX or pull it directly from Tableau Server or Cloud.</span></section>
          <i>&gt;</i>
          <section><small>Automated accelerator</small><b>Discover, translate, rebuild</b><span>Resolve Snowflake data, reconstruct the data model, translate calculations, and generate the app.</span></section>
          <i>&gt;</i>
          <section><small>Outcome</small><b>Streamlit in Snowflake</b><span>A reviewable application with validation evidence and a human-gated deployment.</span></section>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("What it handles")
    st.markdown(
        """
        <div class="v2-capabilities">
          <div class="v2-capability" style="--cap-color:#29b5e8"><h3>Connect and understand</h3><p>Upload a workbook or browse Tableau Server/Cloud.</p><p>Detect datasources, dashboards, sheets, filters, parameters, and calculations.</p></div>
          <div class="v2-capability" style="--cap-color:#11567f"><h3>Automate the migration</h3><p>Match workbook sources to governed Snowflake data automatically.</p><p>Rebuild relationships, semantic definitions, controls, and visual behavior.</p></div>
          <div class="v2-capability" style="--cap-color:#14a07a"><h3>Prove and deploy</h3><p>Preview the generated SiS application and validate migrated measures.</p><p>Produce review artifacts, deploy with approval, and query through Cortex Analyst.</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="v2-trust-strip">
          <div><b>Snowflake native</b><span>Runs inside the governance boundary</span></div>
          <div><b>Metadata driven</b><span>No manual table selection</span></div>
          <div><b>Guided migration progress</b><span>Live status across all five stages</span></div>
          <div><b>Validation gated</b><span>Evidence before deployment</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("One automated workflow")
    render_stepper(done=0, active=1)
    st.markdown(
        """
        <div class="v2-auto-note">
          <div><b>No table selection or manual data-model setup</b><span>The accelerator reads the Tableau workbook metadata, identifies the corresponding Snowflake objects, and reconstructs the model as part of the migration run.</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    action, detail = st.columns([0.25, 0.75])
    with action:
        st.button(
            "Start a migration", type="primary", use_container_width=True,
            key="v2_overview_start", on_click=queue_page, args=("New migration",), icon=":material/play_arrow:",
        )
    with detail:
        st.caption("Snowflake remains the execution and governance boundary throughout the generated application.")


def save_source(name, raw, origin, tableau_conn=None):
    st.session_state["v2_source"] = {"name": name, "raw": raw, "origin": origin}
    if tableau_conn:
        st.session_state["v2_tableau_conn"] = tableau_conn
    else:
        st.session_state.pop("v2_tableau_conn", None)


def render_upload_intake():
    upload = st.file_uploader("Tableau workbook", type=["twb", "twbx"], key="v2_upload")
    if upload is not None:
        save_source(upload.name, upload.getvalue(), "File upload")
        st.success(f"Ready: {upload.name}")


def render_tableau_intake():
    st.caption("The PAT stays in Streamlit secrets or environment variables; it is never entered on this screen.")
    site_url = st.text_input(
        "Tableau site URL",
        value="https://prod-useast-b.online.tableau.com/#/site/b360bi",
        key="v2_site_url",
    )
    if st.button("Connect to Tableau", icon=":material/link:", key="v2_tableau_connect", disabled=not site_url):
        try:
            loc = TS.parse_site_url(site_url)
            with st.spinner("Loading projects and workbooks..."):
                browse = TS.list_site_contents(loc["server_url"], loc["site_content_url"])
            st.session_state["v2_browse"] = {**loc, **browse}
        except Exception as exc:
            st.session_state.pop("v2_browse", None)
            st.error(f"Could not connect: {exc}")

    browse = st.session_state.get("v2_browse")
    if browse:
        projects = sorted({w["project_name"] for w in browse["workbooks"] if w["project_name"]})
        project = st.selectbox("Project", ["All projects"] + projects, key="v2_project")
        books = [w for w in browse["workbooks"] if project == "All projects" or w["project_name"] == project]
        books.sort(key=lambda row: row["name"].lower())
        labels = [f"{w['name']} ({w['project_name']})" for w in books]
        selected = st.selectbox("Workbook", labels, index=None, placeholder="Search workbooks", key="v2_workbook")
        if selected and st.button("Fetch workbook", icon=":material/cloud_download:", key="v2_fetch"):
            wb = books[labels.index(selected)]
            try:
                with st.spinner(f"Downloading {wb['name']}..."):
                    fetched = TS.fetch_workbook_by_id(
                        browse["server_url"], browse["site_content_url"], wb["id"], name_hint=wb["name"]
                    )
                save_source(
                    fetched["filename"], fetched["bytes"], "Tableau Server/Cloud",
                    {"server_url": browse["server_url"], "site_content_url": browse["site_content_url"], "workbook_id": wb["id"]},
                )
                st.success(f"Ready: {fetched['filename']}")
            except Exception as exc:
                st.error(f"Could not fetch {wb['name']}: {exc}")

    with st.expander("Use a direct workbook or view link"):
        direct = st.text_input("Tableau workbook/view URL", key="v2_direct_url")
        if st.button("Fetch from link", icon=":material/cloud_download:", key="v2_fetch_direct", disabled=not direct):
            try:
                with st.spinner("Downloading workbook..."):
                    fetched = TS.fetch_workbook(direct)
                loc = TS.parse_site_url(direct)
                save_source(
                    fetched["filename"], fetched["bytes"], "Tableau direct link",
                    {"server_url": loc["server_url"], "site_content_url": loc["site_content_url"], "workbook_id": fetched["workbook_id"]},
                )
                st.success(f"Ready: {fetched['filename']}")
            except Exception as exc:
                st.error(f"Could not fetch workbook: {exc}")


def run_migration(source, session, status_callback=None):
    def stage(name):
        if status_callback:
            status_callback(name)

    progress = st.progress(0, text="Preparing migration")
    workdir = tempfile.mkdtemp(prefix="sis_v2_")
    wb_path = os.path.join(workdir, source["name"])
    with open(wb_path, "wb") as handle:
        handle.write(source["raw"])

    stage("Discovery")
    progress.progress(8, text="Discovery: resolving datasources")
    discovery = pipeline.onboard(
        wb_path, source["raw"], in_snowflake=pipeline.get_session() is not None, session=session
    )
    if discovery.get("missing"):
        raise RuntimeError("Missing datasource targets: " + ", ".join(discovery["missing"]))

    stage("Parsing")
    progress.progress(28, text="Parsing: building workbook inventory")
    ir = TP.build_ir(wb_path)

    stage("Data model")
    progress.progress(48, text="Data model: inspecting relationships")
    model = SL.describe_model(discovery["root"], pipeline.LOAD_DB, pipeline.LOAD_SCHEMA)
    semantic_ddl = CS.generate_semantic_view(
        ir, config.DATASOURCES, stem_for(source["name"]), db=pipeline.LOAD_DB, schema=pipeline.LOAD_SCHEMA
    )

    stage("App build")
    progress.progress(66, text="App build: generating Streamlit source")
    app_code = codegen.build(ir)
    app_name = f"app_{stem_for(source['name'])}.py"
    app_path = os.path.join(workdir, app_name)
    with open(app_path, "w", encoding="utf-8") as handle:
        handle.write(app_code)

    stage("Validation")
    progress.progress(82, text="Validation: checking measures")
    validation = parity.check_workbook(ir)
    notebook = parity.build_notebook(ir, validation, source["name"])
    progress.progress(100, text="Migration workspace ready")

    return {
        "name": source["name"],
        "origin": source["origin"],
        "workdir": workdir,
        "wb_path": wb_path,
        "discovery": discovery,
        "ir": ir,
        "model": model,
        "semantic_ddl": semantic_ddl,
        "app_name": app_name,
        "app_path": app_path,
        "app_code": app_code,
        "validation": validation,
        "notebook": notebook,
        "stem": stem_for(source["name"]),
    }


def page_migrate(session, session_label):
    run = active_run()
    render_header("New migration" if not run else run["name"], "Choose a source, run the accelerator, then review each result in its own workspace view.")
    render_stepper(done=5 if run else 0, active=6 if run else 1)

    if run:
        s = run["validation"]["summary"]
        sheets = sum(len(d.get("sheets", [])) for d in run["ir"].get("dashboards", []))
        st.markdown(
            f'<div class="v2-summary"><div><span>Dashboards</span><b>{len(run["ir"].get("dashboards", []))}</b></div>'
            f'<div><span>Sheets</span><b>{sheets}</b></div><div><span>Measures passed</span><b>{s["measures_pass"]}/{s["measures_checked"]}</b></div>'
            f'<div><span>Review items</span><b>{s["measures_bug"] + s["calcs_dropped"]}</b></div></div>',
            unsafe_allow_html=True,
        )
        st.success("Migration complete. Use the workspace navigation to inspect the inventory, preview, validation, and deployment package.")
        return

    left, right = st.columns([1.65, 0.75], gap="large")
    with left:
        st.subheader("1. Select workbook source")
        source_mode = st.segmented_control(
            "Workbook source", ["Upload file", "Tableau Server / Cloud"],
            default="Upload file", label_visibility="collapsed", key="v2_source_mode",
        )
        if source_mode == "Upload file":
            render_upload_intake()
        else:
            render_tableau_intake()
    with right:
        st.subheader("Automation")
        st.markdown(
            '<div class="v2-callout"><b>Target platform</b><br>Streamlit in Snowflake</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="v2-auto-note"><div><b>Automatic source resolution</b>'
            '<span>Snowflake objects and the Tableau data model are discovered from workbook metadata. No table input is required.</span></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="v2-next"><div class="v2-next-title">What happens next</div>'
            '<div class="v2-next-row"><div class="v2-next-icon">01</div><div><b>Analyze workbook</b><span>Inventory Tableau content and dependencies</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">02</div><div><b>Resolve Snowflake model</b><span>Match governed data and rebuild relationships</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">03</div><div><b>Validate generated app</b><span>Check measures before deployment review</span></div></div></div>',
            unsafe_allow_html=True,
        )
        with st.expander("Platform constraints"):
            st.caption("Hyper extracts decode locally. In Snowsight, they must already be loaded or mapped to an existing Snowflake table.")

    source = st.session_state.get("v2_source")
    st.markdown('<div class="v2-section"></div>', unsafe_allow_html=True)
    action, note = st.columns([0.25, 0.75])
    with action:
        start = st.button("Run migration", icon=":material/play_arrow:", type="primary", use_container_width=True, disabled=source is None, key="v2_run_btn")
    with note:
        if source:
            st.caption(f"Ready to migrate {source['name']} from {source['origin']}.")
        else:
            st.caption("Select or fetch one workbook to continue.")
    if start:
        live_slot = st.empty()
        try:
            st.session_state["v2_run"] = run_migration(
                source, session, lambda stage: render_live_progress(source["name"], stage, live_slot)
            )
            queue_page("Inventory")
            st.rerun()
        except Exception as exc:
            live_slot.empty()
            st.error(f"Migration stopped: {type(exc).__name__}: {exc}")
            with st.expander("Error details"):
                st.code(traceback.format_exc())


def require_run(title, subtitle):
    render_header(title, subtitle)
    run = active_run()
    if run is None:
        st.markdown('<div class="v2-empty">Run a migration first. The result will appear here without changing the current deployed app.</div>', unsafe_allow_html=True)
        return None
    return run


def page_inventory():
    run = require_run("Workbook inventory", "A concise migration scope. Open details only when you need them.")
    if not run:
        return
    ir = run["ir"]
    sheets = [(d.get("title"), sheet) for d in ir.get("dashboards", []) for sheet in d.get("sheets", [])]
    s = run["validation"]["summary"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Datasources", run["discovery"].get("n_datasources", 0))
    c2.metric("Dashboards", len(ir.get("dashboards", [])))
    c3.metric("Sheets", len(sheets))
    c4.metric("Calculations", s["calcs_translated"] + s["calcs_dropped"])

    tabs = st.tabs(["Dashboards", "Data model", "Calculations", "Findings"])
    with tabs[0]:
        st.dataframe(
            [{"Dashboard": dashboard, "Sheet": sheet.get("name"), "Chart": sheet.get("kind"), "Datasource": sheet.get("datasource")} for dashboard, sheet in sheets],
            use_container_width=True, hide_index=True,
        )
    with tabs[1]:
        rows = []
        for item in run["model"]:
            rows.append({
                "Datasource": item.get("caption"), "Shape": item.get("shape"), "Tables": item.get("n_tables"),
                "Relationships": len(item.get("joins") or []), "Deployable": item.get("deployable", False),
            })
        st.dataframe(rows or [{"Datasource": "No relationship model detected"}], use_container_width=True, hide_index=True)
        st.caption("The accelerator inferred this model from the workbook and Snowflake catalog; no table selection was required.")
    with tabs[2]:
        translated = [{"Calculation": cap, "SQL": value.get("sql") if isinstance(value, dict) else str(value)} for cap, value in (ir.get("calcs") or {}).items()]
        dropped = [{"Calculation": cap, "Tableau formula": formula, "Status": "Review"} for cap, formula in (ir.get("calc_drops") or {}).items()]
        st.dataframe(translated + dropped, use_container_width=True, hide_index=True)
    with tabs[3]:
        issues = []
        for cap, formula in (ir.get("calc_drops") or {}).items():
            issues.append({"Severity": "Review", "Area": "Calculation", "Item": cap, "Detail": formula})
        for cap in run["discovery"].get("blocked") or []:
            issues.append({"Severity": "Blocked", "Area": "Datasource", "Item": cap, "Detail": "Hyper extract could not be decoded in this environment"})
        if issues:
            st.dataframe(issues, use_container_width=True, hide_index=True)
        else:
            st.success("No blocking inventory findings.")


def render_dashboard_tabs(ir):
    findings.clear()
    engine.configure(ir)
    try:
        engine._render_param_controls()
    except Exception:
        pass
    dashboards = ir.get("dashboards") or []
    if not dashboards:
        st.warning("No dashboards found in this workbook.")
        return
    selected = st.selectbox("Dashboard", [d["title"] for d in dashboards], key="v2_dashboard")
    dashboard = next(d for d in dashboards if d["title"] == selected)
    try:
        engine.render_dashboard(dashboard)
    except Exception as exc:
        st.error(f"Dashboard could not render: {exc}")
        with st.expander("Error details"):
            st.code(traceback.format_exc())


def page_preview():
    run = require_run("App preview", "Review the converted experience without mixing it into pipeline logs or validation evidence.")
    if not run:
        return
    toolbar_a, toolbar_b = st.columns([0.72, 0.28])
    with toolbar_a:
        st.caption(f"Generated application: {run['app_name']}")
    with toolbar_b:
        st.download_button("Download app source", run["app_code"], file_name=run["app_name"], mime="text/x-python", icon=":material/download:", use_container_width=True)
    render_dashboard_tabs(run["ir"])


def page_validation():
    run = require_run("Validation", "Start with the verdict. Detailed computation evidence remains available below.")
    if not run:
        return
    result = run["validation"]
    s = result["summary"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Measures passed", f"{s['measures_pass']}/{s['measures_checked']}")
    c2.metric("Bugs", s["measures_bug"])
    c3.metric("Calculations translated", s["calcs_translated"])
    if s["measures_bug"] == 0:
        st.success("Validation passed. No numerical mismatches were detected.")
    else:
        st.warning(f"{s['measures_bug']} result(s) require review.")

    issues = []
    for row in result.get("measures") or []:
        if row["verdict"] != "PASS":
            issues.append({"Area": "Measure", "Datasource": row["datasource"], "Item": row["measure"], "Verdict": row["verdict"], "App": row["app"], "Reference": row["source"]})
    for row in result.get("calc_metrics") or []:
        if row["verdict"] not in ("PASS", "EXECUTED"):
            issues.append({"Area": "Calculation", "Datasource": row["datasource"], "Item": row["metric"], "Verdict": row["verdict"], "App": row.get("value"), "Reference": row.get("tableau_bound")})
    st.subheader("Review queue")
    if issues:
        st.dataframe(issues, use_container_width=True, hide_index=True)
    else:
        st.caption("Nothing requires action.")

    with st.expander("All measure evidence"):
        st.dataframe(result.get("measures") or [], use_container_width=True, hide_index=True)
    with st.expander("Calculated-field evidence"):
        st.dataframe(result.get("calc_metrics") or [], use_container_width=True, hide_index=True)
    with st.expander("Datasource row-count evidence"):
        st.dataframe(result.get("datasources") or [], use_container_width=True, hide_index=True)
    st.download_button(
        "Download validation notebook", json.dumps(run["notebook"], indent=2),
        file_name=f"{run['stem']}_validation.ipynb", mime="application/x-ipynb+json", icon=":material/download:",
    )


def parse_analyst(data):
    sql, text = None, []
    msg = (data or {}).get("message", data) if isinstance(data, dict) else {}
    for item in (msg or {}).get("content", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "sql":
            sql = item.get("statement") or item.get("sql")
        elif item.get("type") == "text":
            text.append(item.get("text", ""))
    return sql, "\n\n".join(value for value in text if value)


def cortex_analyst(session, semantic_view, question):
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": question}]}], "semantic_view": semantic_view}
    try:
        import _snowflake
        response = _snowflake.send_snow_api_request("POST", "/api/v2/cortex/analyst/message", {}, {}, body, None, 30000)
        raw = response.get("content") if isinstance(response, dict) else response
        return parse_analyst(json.loads(raw) if isinstance(raw, str) else raw)
    except ModuleNotFoundError:
        pass
    connection = getattr(session, "connection", None)
    host = getattr(connection, "host", None)
    token = getattr(getattr(connection, "rest", None), "token", None)
    if not (host and token):
        raise RuntimeError("No Cortex Analyst REST session is available")
    import urllib.request
    request = urllib.request.Request(
        f"https://{host}/api/v2/cortex/analyst/message", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f'Snowflake Token="{token}"', "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return parse_analyst(json.loads(response.read()))


def page_deploy(session):
    run = require_run("Deploy & Ask", "Deployment stays human-gated. Cortex Analyst is available only after a semantic view exists.")
    if not run:
        return
    s = run["validation"]["summary"]
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.subheader("Deployment package")
        st.dataframe(
            [
                {"Item": "Application", "Value": run["app_name"]},
                {"Item": "Target", "Value": f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}"},
                {"Item": "Validation", "Value": f"{s['measures_pass']}/{s['measures_checked']} passed"},
                {"Item": "Deployment", "Value": "Human approval required"},
            ], use_container_width=True, hide_index=True,
        )
        deploy = st.button("Deploy to Snowflake", icon=":material/rocket_launch:", type="primary", use_container_width=True, disabled=session is None or s["measures_bug"] > 0, key="v2_deploy")
        if session is None:
            st.caption("Connect Snowflake to enable deployment.")
        elif s["measures_bug"] > 0:
            st.caption("Resolve validation bugs before deployment.")
        if deploy:
            try:
                with st.spinner("Staging runtime files and creating the Streamlit app..."):
                    result = pipeline.deploy_streamlit_app(session, run["stem"], run["app_path"])
                st.session_state["v2_deployment"] = result
                st.success(f"Deployed {result['identifier']}")
                if result.get("url"):
                    st.link_button("Open in Snowsight", result["url"], icon=":material/open_in_new:", use_container_width=True)
            except Exception as exc:
                st.error(str(exc))
        st.download_button(
            "Download semantic model SQL", run["semantic_ddl"],
            file_name=f"{run['stem']}_semantic_view.sql", mime="text/sql",
            icon=":material/download:", use_container_width=True,
        )

    with right:
        st.subheader("Ask your data")
        semantic_name = f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}.{run['stem'].upper()}_SEMANTIC"
        st.caption(f"Semantic view: {semantic_name}")
        with st.form("v2_analyst_form"):
            question = st.text_input("Question", placeholder="Total sales by region last quarter")
            ask = st.form_submit_button("Ask Cortex Analyst", icon=":material/auto_awesome:", use_container_width=True, disabled=session is None)
        if ask and question.strip():
            try:
                with st.spinner("Asking Cortex Analyst..."):
                    sql, answer = cortex_analyst(session, semantic_name, question)
                if answer:
                    st.write(answer)
                if sql:
                    st.code(sql, language="sql")
                    rows = session.sql(sql).to_pandas()
                    st.dataframe(rows, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.warning(str(exc))


inject_styles()
pending_page = st.session_state.pop("v2_pending_page", None)
if pending_page:
    st.session_state["v2_page"] = pending_page
elif st.session_state.get("v2_page") not in (None, *PAGES):
    st.session_state["v2_page"] = "Overview"
session, session_label = resolve_session()
backend.set_session(session)
page = sidebar(session_label)

if page == "Overview":
    page_overview(session_label)
elif page == "New migration":
    page_migrate(session, session_label)
elif page == "Inventory":
    page_inventory()
elif page == "Preview":
    page_preview()
elif page == "Validation":
    page_validation()
else:
    page_deploy(session)

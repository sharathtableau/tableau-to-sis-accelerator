"""Presentation helpers for the V3 migration console.

Extracted from the original accelerator's pipeline_app.py (SHA256 904c25fd09651c8e…,
byte-identical in the parent and V3) so the console keeps the original look:
navy navigation, top bar, five-stage strip and live run panel. Only presentation is
reused here. pipeline_app.py itself is NOT imported, because it pulls in the legacy
run path that writes tables and views during discovery (gaps E02/E13).
Changes from the original: the st.metric card rules are removed (V3 dashboards keep
their verified KPI presentation), and a 'review' stage state is added so a stage can
say "needs review" instead of only complete/failed.
"""
import html as _html

import streamlit as st

STAGES = ["Discovery", "Parsing", "Data model", "App build", "Validation"]
STAGE_NOTE = {
    "Discovery": "Retrieving the workbook and inventorying its datasources (read-only)",
    "Parsing": "Building the scoped workbook model, assessment and Cortex review",
    "Data model": "Verifying Snowflake bindings and authored relationships (read-only)",
    "App build": "Packaging the Streamlit in Snowflake application candidate",
    "Validation": "Summarising support, refusals and what still needs verification",
}
PAGES = ["Overview", "New migration", "Inventory", "Preview", "Validation", "Cortex AI", "Deploy",
         "Estate catalogue"]
PAGE_ICON = {"Overview": "dashboard", "New migration": "note_add", "Inventory": "account_tree",
             "Preview": "web_asset", "Validation": "verified_user", "Cortex AI": "auto_awesome",
             "Deploy": "rocket_launch"}
STATE_LABEL = {"done": "Complete", "review": "Needs review", "failed": "Failed",
               "active": "Running", "": "Pending"}


def _nav_icon_css():
    # The Material Symbols font is not available inside Streamlit in Snowflake, so ligature
    # icons render as their names ("dashboard Overview"). Navigation uses plain labels.
    return ""


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
        section[data-testid="stSidebar"] button[kind="secondary"], section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] { background:#0c2941 !important; border-color:#26465e !important; }
        section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover { background:#123b58 !important; border-color:var(--blue) !important; }
        section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] p, section[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] span { color:#e6f1f8 !important; }
        /* Material Symbols font is unavailable in Streamlit in Snowflake: hide ligature text */
        [data-testid="stIconMaterial"] { display:none !important; }
        /* Material icons are unavailable in SiS: give the sidebar toggles a visible glyph. */
        [data-testid="stExpandSidebarButton"]::before { content:"\\2630"; font-size:22px; color:#11567f; line-height:1; }
        [data-testid="stSidebarCollapseButton"] button::before { content:"\\00AB"; font-size:22px; color:#e6f1f8; line-height:1; }
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
        [data-testid="stFileUploaderDropzone"] {
            min-height:150px; border:1.5px dashed #67c5e8; border-radius:6px;
            background:#f7fcfe; padding:1.5rem 1.2rem;
        }
        [data-testid="stFileUploaderDropzone"] svg { color:var(--blue); width:30px; height:30px; }
        button[kind="primary"] { border-radius:5px; background:var(--blue-dark);
                                 border-color:var(--blue-dark); font-weight:700; }
        button[kind="primary"]:hover { background:#0d486c; border-color:#0d486c; }
        button[kind="primary"] p, [data-testid="stBaseButton-primary"] p,
        [data-testid="stBaseDownloadButton-primary"] p { color:#ffffff !important; }
        [data-testid="stBaseLinkButton-primary"] { background:var(--blue-dark) !important; border-color:var(--blue-dark) !important; border-radius:5px; }
        [data-testid="stBaseLinkButton-primary"] p, [data-testid="stBaseLinkButton-primary"] span { color:#ffffff !important; font-weight:700; }
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
        .v2-step.review { background:#fffaf0; box-shadow:inset 0 3px 0 var(--amber); }
        .v2-step.review i { background:#fff0d1; color:#8b5c08; }
        .v2-pill { display:inline-block; border-radius:999px; padding:2px 9px; font-size:.7rem; font-weight:800; text-transform:uppercase; }
        .v2-pill.ok { background:#edf9f5; color:#087254; } .v2-pill.warn { background:#fff6e6; color:#8b5c08; } .v2-pill.bad { background:#fff1f2; color:#a3333d; }

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
        .v2-arch { width:100%; overflow-x:auto; background:#FFFFFF; border:1px solid #B9CDE6;
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


def render_header(title, subtitle, label="Ready for migration", klass=""):
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


def _step(idx, name, cls, detail=None):
    marker = {"done": "&#10003;", "failed": "!", "review": "?"}.get(cls, str(idx))
    note = detail or STATE_LABEL.get(cls, "Pending")
    return (f'<div class="v2-step {cls}"><i>{marker}</i>'
            f'<b>{esc(name)}</b><span>{esc(note)}</span></div>')


def render_stepper(states=None):
    """Five-stage strip. `states` maps stage name -> done/review/failed/active."""
    states = states or {}
    parts = [_step(i, name, states.get(name, "")) for i, name in enumerate(STAGES, 1)]
    st.markdown(f'<div class="v2-stepper">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_live(slot, workbook, origin, states, events, current=None, finished=False):
    """Live run panel driven only by real stage boundaries (no timers)."""
    completed = sum(1 for s in STAGES if states.get(s) in ("done", "review"))
    failed = any(v == "failed" for v in states.values())
    if finished and not failed:
        percent = 100
        review = any(v == "review" for v in states.values())
        state_txt, state_cls = ("Run complete — review items" if review else "Run complete"), "done"
    elif failed:
        percent = int(completed / len(STAGES) * 100)
        state_txt, state_cls = f"{current or 'Run'} failed", "failed"
    else:
        percent = int((completed + 0.45) / len(STAGES) * 100)
        state_txt, state_cls = f"{current} in progress", ""
    steps = "".join(_step(i, name, states.get(name, "")) for i, name in enumerate(STAGES, 1))
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
        f'<div class="v2-meta"><span>{esc(STAGE_NOTE.get(current, ""))}</span><b>{percent}%</b></div>'
        f'<div class="v2-stepper" style="margin:14px 0 0">{steps}</div>'
        f'<div class="v2-events">{"".join(rows)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

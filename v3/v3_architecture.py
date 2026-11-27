"""V3 platform architecture board for the console Overview.

Replaces the original accelerator's board (gen_platform_architecture.py) inside the V3
console because that board describes behaviour V3 deliberately removed: tables and views
written during ingestion, PASS/BUG verdicts from executed queries and Cortex SQL proposals.
Every box here is labelled BUILT (in this console) or PLANNED (roadmap item), so the
picture never claims more than the product does.
"""
from v3_console_style import esc

CSS = """
<style>
.v3a { display:grid; grid-template-columns: 170px 1fr 190px; gap:14px; background:#071b2e;
       border:1px solid #143a55; border-radius:8px; padding:16px; color:#d7e6f1; font-size:.76rem; }
.v3a h4 { color:#6dcdf0 !important; font-size:.66rem !important; letter-spacing:.08em; text-transform:uppercase;
          margin:0 0 8px !important; }
.v3a .col { display:flex; flex-direction:column; gap:10px; }
.v3a .box { background:#0c2941; border:1px solid #26465e; border-radius:6px; padding:9px 10px; }
.v3a .box b { display:block; color:#fff; font-size:.8rem; margin-bottom:2px; }
.v3a .box span { color:#a9c1d1; font-size:.7rem; line-height:1.35; display:block; }
.v3a .ext { border-color:#29b5e8; }
.v3a .flow { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:8px; }
.v3a .flow .box { border-top:3px solid #29b5e8; min-height:118px; }
.v3a .ai { border-color:#9b7bea; border-top:3px solid #9b7bea; }
.v3a .gov { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.v3a .gov .box { border-top:3px solid #14a07a; }
.v3a .plan { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.v3a .plan .box { border-style:dashed; background:transparent; }
.v3a .tag { display:inline-block; font-size:.58rem; font-weight:800; padding:1px 6px; border-radius:999px;
            margin-left:4px; vertical-align:1px; }
.v3a .built { background:#0f3d33; color:#6fe0b8; } .v3a .planned { background:#3a2f12; color:#f2c46b; }
.v3a .out { border-color:#14a07a; border-top:3px solid #14a07a; }
.v3a .arrow { color:#29b5e8; text-align:center; font-size:.7rem; }
@media(max-width:1000px){ .v3a{grid-template-columns:1fr;} .v3a .flow,.v3a .gov,.v3a .plan{grid-template-columns:1fr 1fr;} }
</style>
"""

FLOW = [
    ('Discovery', 'Workbook pulled over the Tableau REST API (or uploaded); datasources inventoried. Read-only.'),
    ('Parsing', 'Scoped model of every datasource, field, sheet occurrence, filter, parameter and action.'),
    ('Data model', 'Bound to existing Snowflake objects; schema, access, relationship keys and fact grain verified.'),
    ('App build', 'Typed plans compiled to Snowflake SQL; original renderers reused; hash-inventoried package.'),
    ('Validation', 'Refusals listed, verification evidence shown, Tableau parity marked open until proven.'),
]
GOVERNANCE = [
    ('Owner role and RBAC', 'The app runs with its owner role\'s rights: it sees only what that role is granted, and policies on that role apply.'),
    ('Secret + external access', 'Tableau token held in a Snowflake secret; egress only through the integration.'),
    ('Warehouse execution', 'Queries push down to your warehouse; no data rows are extracted or sent to Cortex.'),
    ('No writes during a run', 'Discovery and conversion are read-only; deploy is a separate reviewed step.'),
]
PLANNED = [
    ('Semantic Views + Cortex Analyst', 'Publish validated models; governed natural-language questions.'),
    ('Tableau reference capture', 'Automated Tableau-vs-app comparison per filter state (worker service).'),
    ('AI repair loop', 'Cortex proposes typed plans for refused items; code verifies; numbers accept.'),
    ('AI evaluation and cost', 'Accuracy, unsafe-proposal rejection, latency and cost per model version.'),
]


def _box(title, text, cls='', tag=None):
    badge = f'<span class="tag {tag}">{tag.upper()}</span>' if tag else ''
    return f'<div class="box {cls}"><b>{esc(title)}{badge}</b><span>{esc(text)}</span></div>'


def board_html():
    left = ('<div class="col"><h4>Sources</h4>'
            + _box('Tableau Cloud / Server', 'Workbooks via REST API and a personal access token.', 'ext')
            + _box('Uploaded TWB / TWBX', 'Same intake path as Cloud.', 'ext')
            + _box('Your Snowflake data', 'Existing tables and views the workbook already points at.', 'ext')
            + '</div>')
    flow = ''.join(_box(f'{i}. {name}', text) for i, (name, text) in enumerate(FLOW, 1))
    centre = ('<div class="col"><h4>Migration console · Streamlit in Snowflake</h4>'
              f'<div class="flow">{flow}</div>'
              + _box('Snowflake Cortex · AI_COMPLETE', 'Structured workbook assessment at intake (actual calls '
                     'counted). Proposals are validated against the source; model output is never executed as code.',
                     'ai', 'built')
              + '<h4 style="margin-top:6px">Governance</h4>'
              + '<div class="gov">' + ''.join(_box(t, x) for t, x in GOVERNANCE) + '</div>'
              + '<h4 style="margin-top:6px">Next on the roadmap</h4>'
              + '<div class="plan">' + ''.join(_box(t, x, '', 'planned') for t, x in PLANNED) + '</div>'
              + '</div>')
    right = ('<div class="col"><h4>Outcomes</h4>'
             + _box('Deployed Streamlit app', 'One app per workbook, created from the validated package after review.',
                    'out', 'built')
             + _box('Assessment and coverage', 'Downloadable JSON: every component, its status and reason.', 'out', 'built')
             + _box('Application package', 'Hash-inventoried build with provenance; acceptance recorded separately.',
                    'out', 'built')
             + '</div>')
    return CSS + f'<div class="v3a">{left}{centre}{right}</div>'

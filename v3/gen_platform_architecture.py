# Platform architecture, built on the reference's grammar:
# labelled layers · externals in the margins · tech in parentheses · payload-labelled edges
# · nested QA sub-groups · AI engine as hero · outputs as separate chips.
#
# Importable: pipeline_app.py's Overview tab calls build_svg_fragment() to inline
# this exact board (no page chrome) into the deployed Streamlit-in-Snowflake app.
# Run directly (`python gen_platform_architecture.py`) to regenerate the standalone
# Platform-Architecture.html file instead.
import html

W = 1720
CY, PU, GD, GR, CO = "#00d4d4", "#8b7cf8", "#f0b429", "#34d399", "#e05a4e"
TAB = "#8fa6c4"
MUT = "rgba(255,255,255,.6)"
DIM = "rgba(255,255,255,.34)"

out = []
def e(s): return html.escape(s, quote=True)
def add(s): out.append(s)
def reset(): out.clear()
def txt(x, y, s, size, fill, w=400, anchor="start", ls=None, fam=None, style=None):
    a = f' letter-spacing="{ls}"' if ls else ""
    f = f' font-family="{fam}"' if fam else ""
    st = f' font-style="{style}"' if style else ""
    add(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{w}" fill="{fill}"{a}{f}{st}>{e(s)}</text>')
def rect(x, y, w_, h, fill, stroke=None, rx=6, sop=".45", fop=None, dash=None, filt=None, sw=1):
    s = f' stroke="{stroke}" stroke-opacity="{sop}" stroke-width="{sw}"' if stroke else ""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    f = f' fill-opacity="{fop}"' if fop else ""
    fl = f' filter="url(#{filt})"' if filt else ""
    add(f'<rect x="{x}" y="{y}" width="{w_}" height="{h}" rx="{rx}" fill="{fill}"{f}{s}{d}{fl}/>')

def layer(x, y, w_, h, label, col, sub=None):
    """outlined container with a centred caps label at the top"""
    rect(x, y, w_, h, col, col, rx=10, sop=".38", fop=".035")
    txt(x+w_/2, y+22, label, 11.5, col, 800, anchor="middle", ls="2.2")
    if sub:
        txt(x+w_/2, y+38, sub, 9.5, DIM, 500, anchor="middle", ls=".6")

def cbox(x, y, w_, h, lines, tech, col, fill="#10203c", tag=None, hero=False, filt=None):
    """component box: name line(s) + (tech) — the reference's core unit"""
    dash = "5 4" if tag == "PLANNED" else None
    rect(x, y, w_, h, fill, col, rx=7, sop=".75" if hero else ".6",
         dash=dash, filt=filt or ("glow" if hero else None), sw=1.6 if hero else 1)
    n = len(lines) + (1 if tech else 0)
    fs = 13 if hero else 11
    lh = 15 if hero else 13
    y0 = y + h/2 - (n*lh)/2 + lh - 3
    for i, ln in enumerate(lines):
        txt(x+w_/2, y0+i*lh, ln, fs+(4 if hero else 0), "#fff", 800 if hero else 700, anchor="middle")
    if tech:
        txt(x+w_/2, y0+len(lines)*lh+(3 if hero else 0), f"({tech})", fs-1.5, col, 600, anchor="middle")
    if tag:
        tw = 6.2*len(tag)+12
        rect(x+w_-tw-6, y+5, tw, 14, GD, rx=3, fop=".25", stroke=GD, sop=".6")
        txt(x+w_-tw/2-6, y+15.5, tag, 8, GD, 800, anchor="middle", ls=".8")

def ext(x, y, w_, h, label, col, kind):
    """external system — icon node in the margin, outside every layer"""
    rect(x, y, w_, h, "#0d1830", col, rx=9, sop=".55")
    cx, cy = x+w_/2, y+h/2-6
    if kind == "cloud":
        add(f'<path d="M{cx-19},{cy+7} a11,11 0 0 1 3,-21 a14,14 0 0 1 26,-3 a10,10 0 0 1 8,10 '
            f'a9,9 0 0 1 -6,14 z" fill="{col}" fill-opacity=".3" stroke="{col}" stroke-opacity=".85"/>')
    else:  # snowflake
        for a in (0, 60, 120):
            import math
            dx, dy = 17*math.cos(math.radians(a)), 17*math.sin(math.radians(a))
            add(f'<line x1="{cx-dx}" y1="{cy-dy}" x2="{cx+dx}" y2="{cy+dy}" stroke="{col}" stroke-width="2.4" stroke-linecap="round"/>')
            for sgn in (1, -1):
                bx, by = cx+sgn*dx*.62, cy+sgn*dy*.62
                add(f'<line x1="{bx}" y1="{by}" x2="{bx+sgn*6*math.cos(math.radians(a+55))}" '
                    f'y2="{by+sgn*6*math.sin(math.radians(a+55))}" stroke="{col}" stroke-width="1.8" stroke-linecap="round"/>')
    for i, ln in enumerate(label):
        txt(cx, y+h-20+i*12, ln, 9.5, col, 700, anchor="middle")

def edge(pts, col, marker="a", label=None, lx=None, ly=None, dash=None, width=1.5, op=".55", anchor="middle"):
    """orthogonal connector with a payload label"""
    d = "M" + " L".join(f"{p[0]},{p[1]}" for p in pts)
    da = ' stroke-dasharray="5 4"' if dash else ""
    add(f'<path d="{d}" fill="none" stroke="{col}" stroke-opacity="{op}" stroke-width="{width}"{da} '
        f'marker-end="url(#{marker}{col[1:]})"/>')
    if label:
        tw = 5.4*len(label)+10
        rect(lx-tw/2, ly-8, tw, 15, "#0a1428", col, rx=3, sop=".45", fop=".95")
        txt(lx, ly+3.5, label, 8.5, col, 700, anchor="middle", ls=".7")


def _draw():
    """Draws the full board into the module-level `out` list. Call reset() first.
    Returns the total viewBox height (H); viewBox width is the module constant W."""
    # ---------------------------------------------------------------- geometry
    EL, ELW = 24, 124                 # external left margin
    BX, BWD = 172, 1264               # board
    ER, ERW = 1460, 236               # outputs margin

    UI_Y, UI_H = 88, 96
    MID_Y, MID_H = 212, 438
    QA_Y, QA_H = 682, 358
    H = QA_Y + QA_H + 26

    ING_X, ING_W = BX, 282
    CEN_X, CEN_W = BX+300, 604
    SRV_X, SRV_W = BX+922, 342
    CONV_Y, CONV_H = MID_Y, 210
    INT_Y, INT_H = MID_Y+228, 210

    # ================================ UI LAYER
    layer(BX, UI_Y, BWD, UI_H, "USER INTERFACE LAYER", GD, "one surface — survey, review, approve")
    cbox(BX+BWD/2-170, UI_Y+42, 340, 44, ["Migration Portal"], "Streamlit in Snowflake", GD, "#1b1a2e", hero=False)

    # ================================ INGESTION
    layer(ING_X, MID_Y, ING_W, MID_H, "INGESTION & DISCOVERY LAYER", CY)
    iw = ING_W-32
    cbox(ING_X+16, MID_Y+52,  iw, 62, ["Tableau Server Client"], "Python · REST + PAT", CY, tag="PLANNED")
    cbox(ING_X+16, MID_Y+128, iw, 62, ["Workbook Unpacker"], "Python · hyper / csv / xls", CY)
    cbox(ING_X+16, MID_Y+204, iw, 62, ["Source Resolver"], "Python · declared vs bundled", CY)
    cbox(ING_X+16, MID_Y+280, iw, 62, ["Data Model Planner"], "Python · joins, unions, blends", CY)
    cbox(ING_X+16, MID_Y+356, iw, 54, ["Discovery Report"], "Excel / Markdown", GR, "#0f2430")

    # ================================ CONVERSION
    layer(CEN_X, CONV_Y, CEN_W, CONV_H, "CONVERSION LAYER", CY)
    cw = (CEN_W-32-2*14)/3
    for i, (nm, tech) in enumerate((("Workbook Parser", "Python"), ("IR Model", "JSON"), ("App Generator", "Python"))):
        x = CEN_X+16+i*(cw+14)
        cbox(x, CONV_Y+52, cw, 66, [nm], tech, CY, hero=(i == 1), fill="#12253f" if i == 1 else "#10203c")
        if i < 2:
            edge([(x+cw+1, CONV_Y+85), (x+cw+12, CONV_Y+85)], CY, label=None)
    cbox(CEN_X+16, CONV_Y+134, CEN_W-32, 52, ["Generated Application  +  frozen runtime bundle"], "Python", CY, "#0e2338")

    # ================================ INTELLIGENCE  (the hero layer)
    layer(CEN_X, INT_Y, CEN_W, INT_H, "INTELLIGENCE & TRANSFORMATION LAYER", PU)
    cbox(CEN_X+CEN_W/2-155, INT_Y+72, 310, 84, ["Snowflake Cortex"], "COMPLETE · ANALYST · in-account", PU,
         "#221d3f", hero=True)
    for i, s in enumerate(("Calc translation", "Semantic modelling")):
        txt(CEN_X+22, INT_Y+96+i*22, s, 10.5, PU, 700)
    for i, s in enumerate(("Section comparison", "Visual comparison  (planned)")):
        txt(CEN_X+CEN_W-22, INT_Y+96+i*22, s, 10.5, PU, 700, anchor="end")
    txt(CEN_X+CEN_W/2, INT_Y+INT_H-16, "proposes only — every output must execute, then a person approves",
        10, GD, 700, anchor="middle")

    # ================================ SERVING
    layer(SRV_X, MID_Y, SRV_W, MID_H, "SEMANTIC & SERVING LAYER", CY, "inside your Snowflake account")
    sw = SRV_W-32
    cbox(SRV_X+16, MID_Y+52,  sw, 62, ["Tables & Join Views"], "Snowflake · bound in place", GR)
    cbox(SRV_X+16, MID_Y+128, sw, 62, ["Semantic View"], "Snowflake · one per estate", PU)
    cbox(SRV_X+16, MID_Y+204, sw, 62, ["Migrated Applications"], "Streamlit in Snowflake · one per dashboard", CY, "#0e2338")
    cbox(SRV_X+16, MID_Y+280, sw, 62, ["Cortex Analyst"], "natural-language questions", PU)
    rect(SRV_X+16, MID_Y+356, sw, 54, GR, rx=7, fop=".1", stroke=GR, sop=".4")
    txt(SRV_X+SRV_W/2, MID_Y+380, "Your roles, masking and audit apply", 11, GR, 700, anchor="middle")
    txt(SRV_X+SRV_W/2, MID_Y+396, "no data copied out, no second platform", 10, MUT, 400, anchor="middle")

    # ================================ QA LAYER (nested sub-groups)
    layer(BX, QA_Y, BWD, QA_H, "QUALITY ASSURANCE LAYER", GR, "every migrated dashboard passes all three")
    gw = (BWD-32-2*18)/3
    gy, gh = QA_Y+52, QA_H-72
    GROUPS = [
        ("DATA VALIDATION", GR,
         [("App SQL Results", "Snowpark"), ("Source-of-truth Read", "direct")],
         ("Data Comparator", "Python"), ("Measure Verdict", "PASS / BUG"), None),
        ("CALC VALIDATION", PU,
         [("Rule Translation", "Python · ~97%"), ("Cortex Proposal", "COMPLETE")],
         ("Execution Gate", "runs on real data"), ("Human Review", "sign-off"), None),
        ("VISUAL VALIDATION", GD,
         [("Rendered Screens", "headless PNG"), ("Tableau View Image", "REST")],
         ("Image Comparison", "Cortex vision"), ("Findings Log", "per sheet"), "PLANNED"),
    ]
    for i, (name, col, lefts, mid, right, tag) in enumerate(GROUPS):
        x = BX+16+i*(gw+18)
        rect(x, gy, gw, gh, col, col, rx=9, sop=".35", fop=".045")
        txt(x+gw/2, gy+22, name, 10.5, col, 800, anchor="middle", ls="1.8")
        lw, mwd, rw = 132, 118, 118
        lx = x+16
        for k, (nm, tech) in enumerate(lefts):
            cbox(lx, gy+40+k*68, lw, 58, [nm] if len(nm) < 17 else nm.split(" ", 1), tech, col)
        mx = lx+lw+26
        cbox(mx, gy+74, mwd, 62, mid[0].split(" "), mid[1], col,
             tag=tag if tag else None)
        rx_ = mx+mwd+26
        cbox(rx_, gy+74, rw, 62, right[0].split(" "), right[1], col, "#0f2430")
        for k in range(2):
            edge([(lx+lw+1, gy+69+k*68), (lx+lw+13, gy+69+k*68), (lx+lw+13, gy+105), (mx-3, gy+105)], col, width=1.3, op=".5")
        edge([(mx+mwd+1, gy+105), (rx_-3, gy+105)], col, width=1.3, op=".5")
        txt(x+gw/2, gy+gh-14, ["do the totals match?", "does the formula still mean the same?",
                               "does it still look like Tableau?"][i], 10, MUT, 500, anchor="middle")

    # ================================ EXTERNALS (margins, outside every layer)
    ext(EL, MID_Y+58, ELW, 104, ["Tableau", "Server / Cloud"], TAB, "cloud")
    ext(EL, MID_Y+236, ELW, 104, ["Your existing", "Snowflake data"], CY, "snow")

    layer(ER, MID_Y, ERW, QA_Y+QA_H-MID_Y, "OUTPUTS", GR)
    for i, (nm, tech) in enumerate((("Discovery Report", "Excel"), ("Migration Report", "PDF"),
                                    ("Validation Notebook", "Jupyter"), ("Findings Log", "in-app + report"),
                                    ("Deployed Application", "Snowsight URL"))):
        cbox(ER+16, MID_Y+52+i*76, ERW-32, 62, [nm], tech, GR, "#0f2430")
    txt(ER+ERW/2, MID_Y+470, "everything is a file you can hand over", 10, MUT, 500, anchor="middle")
    txt(ER+ERW/2, MID_Y+486, "— nothing is verbal", 10, MUT, 500, anchor="middle")

    # ================================ EDGES (payload-labelled, orthogonal)
    edge([(EL+ELW+2, MID_Y+110), (ING_X-4, MID_Y+110)], TAB, label="REST API", lx=(EL+ELW+ING_X)/2, ly=MID_Y+96)
    edge([(EL+ELW+2, MID_Y+288), (ING_X-4, MID_Y+288)], CY, label="existing tables", lx=(EL+ELW+ING_X)/2, ly=MID_Y+274)
    edge([(ING_X+ING_W+2, MID_Y+96), (CEN_X-4, MID_Y+96)], CY, label=".twb / .twbx", lx=(ING_X+ING_W+CEN_X)/2, ly=MID_Y+82)
    edge([(ING_X+ING_W+2, MID_Y+236), (CEN_X-14, MID_Y+236), (CEN_X-14, MID_Y+330), (SRV_X-4, MID_Y+330)],
         CY, label="tables + views", lx=(ING_X+ING_W+CEN_X)/2, ly=MID_Y+222)
    edge([(CEN_X+CEN_W+2, CONV_Y+160), (SRV_X-4, CONV_Y+160)], CY, label="Snowpark deploy",
         lx=(CEN_X+CEN_W+SRV_X)/2, ly=CONV_Y+146)
    edge([(CEN_X+CEN_W/2, INT_Y-2), (CEN_X+CEN_W/2, CONV_Y+CONV_H+4)], PU, dash=True,
         label="SQL proposals", lx=CEN_X+CEN_W/2+62, ly=INT_Y-8)
    edge([(CEN_X+CEN_W+2, INT_Y+90), (SRV_X-4, INT_Y+90)], PU, dash=True, label="semantic model",
         lx=(CEN_X+CEN_W+SRV_X)/2, ly=INT_Y+76)
    edge([(CEN_X+CEN_W/2, INT_Y+INT_H+2), (CEN_X+CEN_W/2, QA_Y-4)], PU, dash=True,
         label="comparison + narrative", lx=CEN_X+CEN_W/2+88, ly=QA_Y-10)
    edge([(BX+BWD/2-170-10, UI_Y+64), (ING_X+ING_W/2, UI_Y+64), (ING_X+ING_W/2, MID_Y-4)], GD, width=1.3, op=".4")
    edge([(BX+BWD/2+170+10, UI_Y+64), (SRV_X+SRV_W/2, UI_Y+64), (SRV_X+SRV_W/2, MID_Y-4)], GD, width=1.3, op=".4")
    edge([(SRV_X+SRV_W/2+90, MID_Y+MID_H+2), (SRV_X+SRV_W/2+90, QA_Y-4)], CY, label="app results",
         lx=SRV_X+SRV_W/2+150, ly=QA_Y-10)
    edge([(BX+BWD+2, QA_Y+QA_H/2), (ER-4, QA_Y+QA_H/2)], GR, label="reports", lx=(BX+BWD+ER)/2, ly=QA_Y+QA_H/2-14)
    edge([(SRV_X+SRV_W+2, MID_Y+240), (ER-4, MID_Y+240)], CY, label="app URL", lx=(SRV_X+SRV_W+ER)/2, ly=MID_Y+226)

    return H


def _defs_markup():
    marks = "".join(
        f'<marker id="a{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
        for c in (CY, PU, GD, GR, TAB))
    return (
        f'{marks}'
        f'<filter id="glow" x="-45%" y="-60%" width="190%" height="220%">'
        f'<feDropShadow dx="0" dy="0" stdDeviation="7" flood-color="{PU}" flood-opacity=".55"/></filter>'
    )


ARIA_LABEL = (
    "Platform architecture. External systems in the margins: Tableau Server or Cloud on the left "
    "feeding the ingestion and discovery layer by REST API, and existing Snowflake data. A user "
    "interface layer at the top holds the migration portal. The ingestion and discovery layer holds "
    "the Tableau Server client, workbook unpacker, source resolver, data model planner and discovery "
    "report. The conversion layer holds the workbook parser, the IR model and the app generator, "
    "producing the generated application. Beneath it the intelligence and transformation layer is "
    "centred on Snowflake Cortex, with calc translation, semantic modelling, section comparison and "
    "planned visual comparison. The semantic and serving layer holds tables and join views, the "
    "semantic view, the migrated applications and Cortex Analyst. A quality assurance layer at the "
    "bottom contains three sub-groups: data validation, calc validation and visual validation. "
    "Outputs are listed in the right margin."
)


def build_svg_fragment():
    """Returns (svg_markup, W, H) for the board — no page chrome, ready to inline
    into any host page (an Artifact, or the Streamlit-in-Snowflake app itself)."""
    reset()
    h = _draw()
    body = "\n    ".join(out)
    svg = (
        f'<svg viewBox="0 0 {W} {h}" xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="{html.escape(ARIA_LABEL, quote=True)}" style="display:block;width:100%;height:auto">'
        f'<defs>{_defs_markup()}</defs>'
        f'<g font-family="\'Plus Jakarta Sans\',ui-sans-serif,system-ui,\'Segoe UI\',sans-serif">'
        f'{body}'
        f'</g></svg>'
    )
    return svg, W, h


def build_standalone_page():
    """Full self-contained HTML page (title, fonts, background, legend, footer) —
    what gets written to Platform-Architecture.html for sharing outside the app."""
    svg, w, h = build_svg_fragment()
    return f"""<title>Tableau → Snowflake Migration Accelerator — Platform Architecture</title>
<style>
  :root{{--bg:#0a1428;--cyan:{CY};--purple:{PU};--gold:{GD};--green:{GR};
    --muted:rgba(255,255,255,.6);--dim:rgba(255,255,255,.3);--line:rgba(255,255,255,.09);
    --font:'Plus Jakarta Sans',ui-sans-serif,system-ui,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:#fff;font-family:var(--font);-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:1820px;margin:0 auto;padding:clamp(.9rem,2vw,2rem);position:relative}}
  .wrap::before{{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
    background:radial-gradient(ellipse 60% 34% at 50% 0%,rgba(0,212,212,.11),transparent 72%),
      radial-gradient(ellipse 34% 26% at 50% 50%,rgba(139,124,248,.15),transparent 72%),
      linear-gradient(rgba(255,255,255,.011) 1px,transparent 1px),
      linear-gradient(90deg,rgba(255,255,255,.011) 1px,transparent 1px);
    background-size:auto,auto,54px 54px,54px 54px}}
  .wrap>*{{position:relative;z-index:1}}
  .head{{text-align:center;padding-bottom:.5rem;margin-bottom:.5rem}}
  .eyebrow{{font-size:.66rem;font-weight:700;letter-spacing:.5em;color:var(--cyan);text-transform:uppercase}}
  .eyebrow span{{color:var(--dim);letter-spacing:.2em}}
  h1{{margin-top:.35rem;font-size:clamp(1.6rem,3.4vw,3rem);font-weight:800;letter-spacing:-.035em;line-height:1.02}}
  h1 .a{{color:var(--cyan)}}
  .sub{{margin:.5rem auto 0;font-size:clamp(.76rem,1vw,.9rem);color:var(--muted);max-width:88ch;line-height:1.55}}
  .diagram{{width:100%;overflow-x:auto}}
  .diagram svg{{min-width:1380px}}
  .foot{{display:flex;flex-wrap:wrap;gap:.4rem 1.3rem;margin-top:.7rem;padding-top:.6rem;
    border-top:1px solid var(--line);font-size:.6rem;color:var(--dim);letter-spacing:.05em;align-items:center}}
  .lg{{display:flex;align-items:center;gap:.4rem}}
  .sw{{width:18px;height:3px;border-radius:2px;flex:none}}
  .sw.dashp{{height:0;border-top:2px dashed var(--purple)}}
  .sw.box{{height:10px;border:1px dashed var(--gold);background:none;border-radius:2px}}
  .creed{{margin-left:auto;font-size:.9rem;font-weight:800}}
  .creed .p{{color:var(--purple)}} .creed .c{{color:var(--cyan)}}
  @media (prefers-reduced-motion:reduce){{*{{animation:none !important}}}}
</style>

<div class="wrap">
  <div class="head">
    <div class="eyebrow">— <span>BLEND360</span> · SYSTEM DESIGN —</div>
    <h1>Platform <span class="a">Architecture</span></h1>
    <p class="sub">Tableau → Streamlit-in-Snowflake migration accelerator. One engine runs once per
      workbook across the whole estate; Snowflake Cortex sits inside the account and is called by the
      engine; every migrated dashboard passes three independent validations before anyone approves it.</p>
  </div>

  <div class="diagram">{svg}</div>

  <div class="foot">
    <span class="lg"><span class="sw" style="background:{TAB}"></span>From Tableau</span>
    <span class="lg"><span class="sw" style="background:var(--cyan)"></span>Deterministic — built and verified</span>
    <span class="lg"><span class="sw dashp"></span>Snowflake Cortex — called in-account</span>
    <span class="lg"><span class="sw" style="background:var(--green)"></span>Validation &amp; outputs</span>
    <span class="lg"><span class="sw" style="background:var(--gold)"></span>Human approval</span>
    <span class="lg"><span class="sw box"></span>PLANNED — in build</span>
    <span class="creed"><span class="p">Cortex does the thinking.</span> <span class="c">You decide what goes live.</span></span>
  </div>
</div>
"""


if __name__ == "__main__":
    page = build_standalone_page()
    with open("Platform-Architecture.html", "w", encoding="utf-8") as f:
        f.write(page)
    print("wrote Platform-Architecture.html")

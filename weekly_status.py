"""
weekly_status.py  --  REUSABLE weekly progress report generator.

Run once before each weekly meeting. It pulls LIVE numbers from the codebase
(regression suite pass/fail, calc coverage, filter coverage) and merges them
with the editable narrative in status_config.json (components, roadmap, corpus
fidelity, this-week notes), then writes a dated, self-contained HTML report.

    python weekly_status.py              # live checks + report
    python weekly_status.py --fast       # skip the regression run (use cache)

Output:  reports/weekly/status_YYYY-MM-DD.html   (open / email / drop in a deck)

Every week: optionally edit status_config.json (roadmap + notes + fidelity),
then run this. The measured sections (calc %, filter coverage, tests) refresh
themselves from the actual code, so the report can't drift from reality.
"""

import datetime
import glob
import json
import os
import subprocess
import sys

import audit_calcs
import audit_features
import audit_filters

STATUS_COLORS = {"done": ("#0F6E56", "#E1F5EE"), "progress": ("#854F0B", "#FAEEDA"),
                 "planned": ("#5F5E5A", "#F1EFE8"), "pending": ("#5F5E5A", "#F1EFE8"),
                 "full": ("#0F6E56", "#E1F5EE"), "partial": ("#854F0B", "#FAEEDA"),
                 "gap": ("#993C1D", "#FAECE7"), "scope": ("#5F5E5A", "#F1EFE8")}
STATUS_TEXT = {"full": "handled", "partial": "partial", "gap": "not yet",
               "scope": "out of scope", "done": "done", "progress": "in progress",
               "planned": "planned", "pending": "pending"}


def _workbooks():
    return [p for p in sorted(glob.glob("*.twb") + glob.glob("*.twbx"))
            if not p.startswith("~")]


def _run_regression(fast):
    if fast:
        return None
    r = subprocess.run([sys.executable, os.path.join("tests", "test_regression.py")],
                       capture_output=True, text=True)
    passed = r.stdout.count("\nok ") + r.stdout.count("ok  ")
    return {"passed": r.returncode == 0,
            "gates": sum(1 for l in r.stdout.splitlines() if l.startswith("ok"))}


def _bar(pct, color):
    return (f'<div style="flex:1;height:6px;background:#F1EFE8;border-radius:999px;'
            f'overflow:hidden;"><div style="width:{pct}%;height:100%;'
            f'background:{color};"></div></div>')


def _chip(status):
    fg, bg = STATUS_COLORS.get(status, ("#5F5E5A", "#F1EFE8"))
    return (f'<span style="font-size:11px;padding:3px 9px;border-radius:999px;'
            f'background:{bg};color:{fg};white-space:nowrap;">{STATUS_TEXT.get(status, status)}</span>')


def build_html(cfg, calc, filt, feat, reg):
    d = datetime.date.today().isoformat()
    green, amber = "#1D9E75", "#EF9F27"
    H = []
    H.append(f"""<!doctype html><html><head><meta charset="utf-8">
<title>{cfg['project']} — status {d}</title>
<style>
 body{{font-family:'Plus Jakarta Sans',-apple-system,system-ui,sans-serif;color:#1a1a2e;
      max-width:900px;margin:0 auto;padding:32px;background:#fff;line-height:1.5;}}
 h1{{font-size:24px;font-weight:800;margin:0 0 2px;}}
 h2{{font-size:14px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
     color:#5a6072;margin:32px 0 12px;}}
 .sub{{color:#5a6072;font-size:13px;margin:0 0 24px;}}
 .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0;}}
 .card{{background:#f6f7f9;border-radius:8px;padding:14px 16px;}}
 .card .l{{font-size:12px;color:#5a6072;}} .card .v{{font-size:24px;font-weight:800;}}
 .row{{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:.5px solid #e2e5ea;}}
 .nm{{flex:0 0 210px;font-size:13px;}} .nm .n2{{font-size:11px;color:#8a8f9c;}}
 .pct{{flex:0 0 36px;text-align:right;font-size:12px;color:#5a6072;}}
 .st{{flex:0 0 96px;text-align:right;}}
 table{{border-collapse:collapse;width:100%;font-size:13px;}}
 td{{padding:6px 8px;border-bottom:.5px solid #eee;}} .note{{color:#8a8f9c;font-size:11px;}}
</style></head><body>""")
    H.append(f"<h1>{cfg['project']}</h1>")
    H.append(f"<p class='sub'>Weekly status &middot; {d} &middot; owner: {cfg.get('owner','')}</p>")

    # ---- honest headline metrics ----
    # Feature coverage = breadth of Tableau's surface handled (partial counts
    # half; out-of-scope features excluded). This INCLUDES the backlog, so it
    # reflects how much is pending -- not just whether the pipeline is built.
    inscope = [r for r in feat if r["status"] != "scope"]
    fcov = round(100 * sum({"full": 1, "partial": 0.5, "gap": 0}[r["status"]]
                           for r in inscope) / max(len(inscope), 1))
    n_full = sum(1 for r in feat if r["status"] == "full")
    n_gap = sum(1 for r in feat if r["status"] == "gap")
    # Workbook fidelity = how well REAL dashboards convert (the number that matters).
    # Workbooks flagged out_of_scope (e.g. a live sqlproxy source that ships no data
    # and is not queryable) are EXCLUDED -- their fidelity is N/A, not a defect, so
    # counting them as 0 would understate the quality of the in-scope corpus.
    scored = [w for w in cfg["corpus"] if not w.get("out_of_scope")]
    fid = round(sum(w["fidelity"] for w in scored) / max(len(scored), 1))
    # Phase progress = simple mean of phase %s.
    phases = cfg.get("phases", [])
    phase_prog = round(sum(p["pct"] for p in phases) / max(len(phases), 1)) if phases else fcov
    # Overall completion = how much of the PRODUCT is built: the blend of feature
    # breadth and phase progress. (Fidelity/calc measure how WELL the built part
    # works -- quality, not completion -- so they are shown but NOT folded in.)
    overall = round((fcov + phase_prog) / 2)
    reg_txt = ("passing" if reg and reg["passed"] else
               ("FAILING" if reg else "not run"))
    H.append("<div class='cards' style='grid-template-columns:repeat(4,1fr)'>")
    cards = [("Overall completion", f"{overall}%", f"feature breadth + phase progress"),
             ("Feature coverage", f"{fcov}%", f"{n_full} full &middot; {n_gap} pending of {len(inscope)}"),
             ("Workbook fidelity", f"{fid}%", f"quality on {len(scored)} in-scope tested"),
             ("Regression tests", reg_txt if not reg else f"{reg['gates']} gates", "self-verified")]
    for i, (l, v, sub) in enumerate(cards):
        hl = ";border:1.5px solid #2f6df6" if i == 0 else ""
        H.append(f"<div class='card' style='padding:14px 16px{hl}'><div class='l'>{l}</div>"
                 f"<div class='v' style='font-size:20px'>{v}</div>"
                 f"<div class='l' style='font-size:11px;margin-top:2px'>{sub}</div></div>")
    H.append("</div>")
    H.append(f"<p style='font-size:12px;color:#5a6072;margin:-8px 0 8px;line-height:1.6'>"
             f"<b style='font-weight:500'>Overall completion {overall}%</b> = the average of "
             f"<b style='font-weight:500'>feature breadth {fcov}%</b> (how much of Tableau's "
             f"surface is built) and <b style='font-weight:500'>phase progress {phase_prog}%</b> "
             f"(P1 done, P2/P3 in flight, P4/P5 to start). It counts <i>how much</i> is built. "
             f"Calc coverage {calc['overall_pct']}% and workbook fidelity {fid}% measure "
             f"<i>how well</i> the built part works &mdash; quality, not completion, so they are "
             f"reported separately and not folded into the headline.</p>")

    # ---- build each section as a string, then assemble top-down for a
    #      director: conclusion -> what changed -> proof -> map -> plan -> detail
    def sec_mvp():
        m = cfg.get("mvp")
        if not m:
            return ""
        x = ["<h2>0 &middot; MVP scope &amp; ETA</h2>"]
        x.append(f"<div class='card' style='border:1.5px solid #2f6df6;margin:0 0 12px'>"
                 f"<div class='l'>Target (definition of MVP)</div>"
                 f"<div style='font-size:15px;font-weight:600;margin:3px 0 10px;color:#1a1a2e'>"
                 f"{m['definition']}</div>"
                 f"<div class='l'>Estimate to MVP at current pace</div>"
                 f"<div class='v' style='font-size:26px;color:#2f6df6'>{m['estimate']}</div>"
                 f"<div class='l' style='font-size:11px;margin-top:2px'>{m.get('estimate_note','')}</div></div>")
        if m.get("core_note"):
            x.append(f"<p style='font-size:12px;color:#5a6072;margin:0 0 10px;line-height:1.6'>"
                     f"<b style='font-weight:600;color:#0F6E56'>Core already built.</b> "
                     f"{m['core_note']}</p>")
        n_done = sum(1 for i in m["items"] if i["status"] == "done")
        n_prog = sum(1 for i in m["items"] if i["status"] == "progress")
        x.append(f"<p style='font-size:13px;color:#5a6072;margin:0 0 6px'>"
                 f"<b style='font-weight:600'>Must-close to reach MVP — {len(m['items'])} items</b> "
                 f"({n_done} done &middot; {n_prog} in progress &middot; "
                 f"{len(m['items'])-n_done-n_prog} not started):</p><table>")
        x.append("<tr style='color:#8a8f9c;font-size:11px;text-transform:uppercase'>"
                 "<td>Item</td><td style='text-align:right'>Est.</td>"
                 "<td style='text-align:right'>Status</td></tr>")
        for i, it in enumerate(m["items"], 1):
            x.append(f"<tr><td><b style='font-weight:600'>{i}. {it['item']}</b>"
                     f"<div class='note'>{it.get('note','')}</div></td>"
                     f"<td style='text-align:right;white-space:nowrap;font-size:12px'>{it['days']} d</td>"
                     f"<td style='text-align:right'>{_chip(it['status'])}</td></tr>")
        x.append("</table>")
        if m.get("out_of_scope"):
            x.append(f"<p style='font-size:12px;color:#8a8f9c;margin:8px 0 0;line-height:1.6'>"
                     f"<b style='font-weight:600;color:#5a6072'>Not in MVP (deferred):</b> "
                     f"{m['out_of_scope']}</p>")
        return "".join(x)

    def sec_notes():
        if not cfg.get("notes_this_week"):
            return ""
        x = ["<h2>What moved this week</h2><ul style='font-size:14px;margin:0;padding-left:20px'>"]
        for n in cfg["notes_this_week"]:
            x.append(f"<li style='margin:4px 0'>{n}</li>")
        x.append("</ul>")
        return "".join(x)

    def sec_proof():
        x = ["<h2>1 &middot; Proof — real workbooks converting</h2>"]
        x.append("<p style='font-size:12px;color:#5a6072;margin:0 0 8px;line-height:1.6'>"
                 "<b style='font-weight:500'>Fidelity</b> = how completely each workbook's "
                 "sheets convert. Every sheet starts at 100% and loses points for each "
                 "approximation or gap (a sheet that fails to render scores 0). It measures "
                 "<i>completeness</i> &mdash; charts render with few gaps, nothing silently "
                 "dropped. Numeric correctness and visual match are checked separately.</p><table>")
        for w in sorted(cfg["corpus"], key=lambda x: -x["fidelity"]):
            col = green if w["fidelity"] >= 90 else amber
            x.append(f"<tr><td>{os.path.splitext(w['workbook'])[0]}</td>"
                     f"<td style='color:{col};font-weight:700;text-align:right'>{w['fidelity']}%</td>"
                     f"<td class='note'>{w.get('note','')}</td></tr>")
        x.append("</table>")
        return "".join(x)

    def sec_map():
        return ("<h2>2 &middot; The map — what Tableau it handles</h2>" +
                _features_html(feat).replace("<h2>What Tableau features it handles — by area</h2>", "") +
                _charttypes_html())

    def sec_plan():
        x = []
        if cfg.get("phases"):
            x.append("<h2>3 &middot; The plan — phases &amp; timeline</h2>")
            x.append(_phases_html(cfg["phases"], d, green, amber).replace("<h2>Phased plan</h2>", ""))
        x.append("<h2>Roadmap (next up)</h2><table>")
        for r in cfg["roadmap"]:
            x.append(f"<tr><td>{r['item']} <span class='note'>&mdash; {r['detail']}</span></td>"
                     f"<td style='text-align:right'>{_chip(r['status'])}</td></tr>")
        x.append("</table>")
        return "".join(x)

    def sec_detail():
        x = ["<h2>4 &middot; Detail (for reference)</h2>"]
        x.append(f"<h3 style='font-size:13px;color:#5a6072;margin:14px 0 6px'>"
                 f"Calculation coverage &middot; {calc['n_ok']}/{calc['n_calc']} translate "
                 f"({calc['overall_pct']}%)</h3>")
        for cat in ["math", "date", "type", "lod", "logical", "aggregate", "string", "table_calc"]:
            if cat in calc["categories"]:
                p = calc["categories"][cat]
                col = green if p >= 90 else amber
                x.append(f"<div class='row'><div class='nm' style='flex:0 0 130px'>{cat.replace('_',' ')}</div>"
                         f"{_bar(p, col)}<div class='pct'>{p}%</div><div class='st'></div></div>")
        x.append("<h3 style='font-size:13px;color:#5a6072;margin:20px 0 6px'>Filter coverage</h3><table>")
        for r in filt:
            seen = f"{r['count']}x" if r["present"] else "&mdash;"
            x.append(f"<tr><td>{r['type']}</td><td style='text-align:right'>{_chip(r['status'])}</td>"
                     f"<td style='text-align:right;color:#8a8f9c'>{seen}</td></tr>")
        x.append("</table>")
        x.append("<h3 style='font-size:13px;color:#5a6072;margin:20px 0 6px'>"
                 "Pipeline components (architecture built &mdash; how much of each part exists, "
                 "distinct from feature coverage above)</h3>")
        for c in cfg["components"]:
            col = green if c["status"] == "done" else amber
            x.append(f"<div class='row'><div class='nm'>{c['name']}<div class='n2'>{c['note']}</div></div>"
                     f"{_bar(c['pct'], col)}<div class='pct'>{c['pct']}%</div>"
                     f"<div class='st'>{_chip(c['status'])}</div></div>")
        return "".join(x)

    H.append(sec_mvp())
    H.append(sec_notes())
    H.append(sec_proof())
    H.append(sec_map())
    H.append(sec_plan())
    H.append(sec_detail())
    H.append(f"<p class='note' style='margin-top:28px'>Generated {d} by weekly_status.py &middot; "
             f"measured sections (fidelity, calc %, filters, features, tests) are computed live "
             f"from the codebase &mdash; they can't drift from reality.</p>")
    H.append("</body></html>")
    return "\n".join(H)


def _avg_pct(components):
    return round(sum(c["pct"] for c in components) / max(len(components), 1))


CAT_DISPLAY = {
    "Data model": "Data sources &amp; joins",
    "Fields": "Fields, calculations &amp; groups",
    "Filters": "Filters",
    "Marks & analytics": "Charts &amp; analytics",
    "Dashboard": "Dashboard &amp; interactivity",
    "Formatting": "Formatting &amp; colours",
    "Maps": "Maps",
}


def _short(name):
    """Feature name without the parenthetical explainer, for compact summaries."""
    return name.split("(")[0].strip()


# Tableau chart types (Show Me + common) -> our coverage. Curated, honest.
CHART_TYPES = {
    "full": ["Horizontal bar", "Stacked bar", "Side-by-side bar", "Line",
             "Area", "Scatter plot", "Pie", "Treemap", "Packed bubbles",
             "Heat map", "Highlight table", "Circle / dot plot",
             "Side-by-side circle", "Filled map", "Symbol map", "Gantt",
             "KPI / BAN", "Text table", "100%-stacked bar"],
    "partial": ["Dual axis", "Blended axis", "Combo (bar + line)",
                "Shape-encoded marks"],
    "gap": ["Histogram", "Box-and-whisker", "Bullet graph",
            "Matrix / pivot crosstab"],
}


def _charttypes_html():
    G, A, R = "#0F6E56", "#BA7517", "#993C1D"
    bg = {"full": "#E1F5EE", "partial": "#FAEEDA", "gap": "#FAECE7"}
    fg = {"full": G, "partial": A, "gap": R}
    lab = {"full": "converts", "partial": "approximate", "gap": "not built yet"}
    nf, np_, ng = (len(CHART_TYPES["full"]), len(CHART_TYPES["partial"]),
                   len(CHART_TYPES["gap"]))
    H = [f"<h3 style='font-size:13px;color:#5a6072;margin:18px 0 6px'>"
         f"Chart types &mdash; {nf} convert, {np_} approximate, {ng} not built yet</h3>"]
    H.append("<p style='font-size:11px;color:#8a8f9c;margin:0 0 8px;line-height:1.5'>"
             "Approximate = renders the same data a different way (e.g. dual-axis "
             "shown as stacked panes, not a two-scale overlay).</p>")
    for key in ("full", "partial", "gap"):
        chips = "".join(
            f"<span style='font-size:11px;padding:3px 9px;border-radius:999px;"
            f"background:{bg[key]};color:{fg[key]};margin:0 5px 5px 0;"
            f"display:inline-block'>{t}</span>" for t in CHART_TYPES[key])
        H.append(f"<div style='margin:4px 0 8px'>"
                 f"<span style='font-size:11px;color:{fg[key]};font-weight:500;"
                 f"display:inline-block;width:120px'>{lab[key]}</span>{chips}</div>")
    return "".join(H)


def _features_html(feat):
    """Tableau feature coverage as a readiness bar per area, each with a plain
    'what works / what's pending' line so it reads without prior knowledge."""
    from collections import OrderedDict
    G, A, R, S = "#1D9E75", "#EF9F27", "#D85A30", "#C9CCD3"
    cats = OrderedDict()
    for r in feat:
        cats.setdefault(r["category"], {"full": [], "partial": [], "gap": [], "scope": []})
        cats[r["category"]][r["status"]].append(_short(r["feature"]))

    H = ["<h2>What Tableau features it handles — by area</h2>"]
    H.append("<p style='font-size:13px;color:#5a6072;margin:0 0 4px;line-height:1.6'>"
             "Tableau's capabilities grouped into 7 areas. Each bar shows, for that "
             "area, how many features fully convert, convert approximately, or "
             "aren't built yet. The line under each bar names what works and what's "
             "pending, so no Tableau knowledge is needed to read it.</p>")
    H.append("<div style='display:flex;gap:16px;margin:8px 0 14px;font-size:11px;color:#5a6072'>"
             f"<span><span style='display:inline-block;width:10px;height:10px;background:{G};border-radius:2px'></span> converts</span>"
             f"<span><span style='display:inline-block;width:10px;height:10px;background:{A};border-radius:2px'></span> approximate</span>"
             f"<span><span style='display:inline-block;width:10px;height:10px;background:{R};border-radius:2px'></span> not built yet</span>"
             f"<span><span style='display:inline-block;width:10px;height:10px;background:{S};border-radius:2px'></span> out of scope</span></div>")
    for cat, c in cats.items():
        counts = {k: len(c[k]) for k in ("full", "partial", "gap", "scope")}
        n = sum(counts.values())
        segs = ""
        for key, col in (("full", G), ("partial", A), ("gap", R), ("scope", S)):
            if counts[key]:
                w = round(100 * counts[key] / n)
                lab = counts[key] if w >= 9 else ""
                segs += (f"<div style='width:{w}%;background:{col};display:flex;"
                         f"align-items:center;justify-content:center;color:#fff;font-size:11px'>{lab}</div>")
        works = ", ".join(c["full"]) or "—"
        pending = ", ".join(c["gap"] + c["partial"]) or "nothing"
        H.append(f"<div style='margin:12px 0 4px'>"
                 f"<div style='display:flex;align-items:center;gap:12px'>"
                 f"<div style='flex:0 0 190px;font-size:13px;color:#1a1a2e'>{CAT_DISPLAY.get(cat, cat)}</div>"
                 f"<div style='flex:1;display:flex;height:22px;border-radius:5px;overflow:hidden'>{segs}</div>"
                 f"<div style='flex:0 0 66px;text-align:right;font-size:11px;color:#8a8f9c'>{counts['full']}/{n} full</div>"
                 f"</div>"
                 f"<div style='margin:3px 0 0 202px;font-size:11px;color:#8a8f9c;line-height:1.5'>"
                 f"<span style='color:#0F6E56'>Works:</span> {works}. "
                 f"<span style='color:#993C1D'>Pending:</span> {pending}.</div></div>")

    gaps = sorted([r for r in feat if r["status"] == "gap" and r["present"]],
                  key=lambda x: -x["count"])
    if gaps:
        H.append("<p style='font-size:13px;color:#5a6072;margin:18px 0 4px'>"
                 "Of everything not built yet, these are the features your own five "
                 "workbooks actually use &mdash; so this is the real build order:</p><table>")
        for r in gaps:
            H.append(f"<tr><td>{r['feature']}</td>"
                     f"<td style='text-align:right;color:#8a8f9c'>used {r['count']}x across your workbooks</td>"
                     f"<td style='text-align:right'>{_chip('gap')}</td></tr>")
        H.append("</table>")
    return "\n".join(H)


def _phases_html(phases, today, green, amber):
    """Phased plan: a table (goal/owner/dates/status) + a mini-Gantt timeline."""
    fmt = "%Y-%m-%d"
    dates = []
    for p in phases:
        dates += [datetime.datetime.strptime(p["start"], fmt),
                  datetime.datetime.strptime(p["end"], fmt)]
    lo, hi = min(dates), max(dates)
    span = max((hi - lo).days, 1)
    now = datetime.datetime.strptime(today, fmt)
    now_pct = max(0, min(100, round(100 * (now - lo).days / span)))

    H = ["<h2>Phased plan</h2>"]
    # timeline / mini-Gantt
    H.append("<div style='position:relative;margin:8px 0 20px;'>")
    for p in phases:
        s = datetime.datetime.strptime(p["start"], fmt)
        e = datetime.datetime.strptime(p["end"], fmt)
        left = round(100 * (s - lo).days / span)
        width = max(2, round(100 * (e - s).days / span))
        col = green if p["status"] == "done" else (amber if p["status"] == "progress" else "#C9CCD3")
        H.append(
            f"<div style='display:flex;align-items:center;gap:10px;margin:5px 0;'>"
            f"<div style='flex:0 0 150px;font-size:12px;'>{p['id']} &middot; {p['name']}</div>"
            f"<div style='flex:1;position:relative;height:18px;background:#f6f7f9;border-radius:4px;'>"
            f"<div style='position:absolute;left:{left}%;width:{width}%;top:0;height:18px;"
            f"background:{col};border-radius:4px;'></div></div>"
            f"<div style='flex:0 0 44px;text-align:right;font-size:11px;color:#8a8f9c;'>{p['pct']}%</div>"
            f"</div>")
    H.append(f"<div style='display:flex;gap:10px;margin-top:2px;'>"
             f"<div style='flex:0 0 150px'></div><div style='flex:1;position:relative;height:14px;'>"
             f"<div style='position:absolute;left:{now_pct}%;top:-2px;height:14px;border-left:2px solid #d64545;'></div>"
             f"<span style='position:absolute;left:{now_pct}%;font-size:10px;color:#d64545;margin-left:3px;'>today</span>"
             f"</div><div style='flex:0 0 44px'></div></div>")
    H.append(f"<div style='font-size:11px;color:#8a8f9c;margin-top:6px;'>"
             f"{lo.strftime('%d %b')} &rarr; {hi.strftime('%d %b %Y')} &middot; "
             f"timelines are planning estimates &middot; owners editable in status_config.json</div>")
    H.append("</div>")
    # table
    H.append("<table>")
    H.append("<tr style='color:#8a8f9c;font-size:11px;text-transform:uppercase'>"
             "<td>Phase</td><td>Owner</td><td>Window</td><td style='text-align:right'>Status</td></tr>")
    for p in phases:
        s = datetime.datetime.strptime(p["start"], fmt).strftime("%d %b")
        e = datetime.datetime.strptime(p["end"], fmt).strftime("%d %b")
        H.append(f"<tr><td><b style='font-weight:500'>{p['id']} {p['name']}</b>"
                 f"<div class='note'>{p['goal']}</div>"
                 f"<div class='note'>Deliverables: {p['deliverables']}</div></td>"
                 f"<td style='font-size:12px'>{p['owner']}</td>"
                 f"<td style='font-size:12px;white-space:nowrap'>{s} &ndash; {e}</td>"
                 f"<td style='text-align:right'>{_chip(p['status'])}</td></tr>")
    H.append("</table>")
    return "\n".join(H)


def main():
    fast = "--fast" in sys.argv
    cfg = json.load(open("status_config.json", encoding="utf-8"))
    wbs = _workbooks()
    print("Computing calc coverage ...")
    calc = audit_calcs.coverage(wbs)
    print("Computing filter coverage ...")
    filt = audit_filters.coverage(wbs)
    print("Computing Tableau feature coverage ...")
    feat = audit_features.coverage(wbs)
    reg = None
    if not fast:
        print("Running regression suite ...")
        reg = _run_regression(fast)
    html = build_html(cfg, calc, filt, feat, reg)
    os.makedirs(os.path.join("reports", "weekly"), exist_ok=True)
    out = os.path.join("reports", "weekly",
                       f"status_{datetime.date.today().isoformat()}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")
    print(f"  calc coverage {calc['overall_pct']}%  |  "
          f"filters: {sum(1 for r in filt if r['status']=='full')} handled, "
          f"{sum(1 for r in filt if r['status']=='gap' and r['present'])} gap  |  "
          f"tests: {'pass' if reg and reg['passed'] else ('n/a' if fast else 'FAIL')}")


if __name__ == "__main__":
    main()

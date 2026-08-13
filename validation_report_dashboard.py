"""
validation_report_dashboard.py -- the CLIENT-FACING dashboard validation
report, rendered to the structure the user supplied as the target
(`dashboard_validation_report_sample.html`, 2026-08-07).

WHY A SECOND RENDERER RATHER THAN CHANGING validation_report.py: that module
is the vendored, dependency-free comparison ENGINE plus its own compact
renderer, and its output shape is locked by `test_validation_pack_adapter`.
This module renders the SAME already-validated `run` dict -- the exact
object `validation_report.validate_run()` returns, with every verdict
already decided -- into the richer per-dashboard chapter layout. It decides
NOTHING: every status, difference and tolerance shown here was computed by
the engine. Rendering and judging stay separate, the same rule the rest of
this project follows.

WHAT IT ADDS over the compact renderer: per-dashboard header statistics
(visual similarity, sheets passed, rows outside tolerance, interactions
passed, proof completeness), a chart data CONTRACT table (what was compared
at which grain, and the shape each of the three sources returned),
expandable per-chart records with the pairwise Tableau/Streamlit/Backend
verdicts, a consolidated exceptions register, an evidence & reproducibility
section, and a sign-off record.

HONESTY NOTE, deliberately preserved: the supplied sample shows a
"98.1% pixel similarity" and a "max shift 4 px" layout check. This project
CANNOT produce those today and does not pretend to -- the app-side image is
a headless re-render of the app's own chart objects (headless_render.py),
not a screenshot of the deployed SSO-gated Streamlit app, so there is no
pixel-registration between the two. What IS shown is the real structural
edge-difference score, explicitly labelled as triage-only, and -- when a
Cortex vision pass has run -- the AI verdict that can actually read the two
images. A number this project cannot honestly measure is never printed.
"""
from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

PASS = "PASS"
REVIEW = "REVIEW"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_VALIDATED = "NOT_VALIDATED"

_BADGE_CLASS = {PASS: "ok", REVIEW: "warn", FAIL: "bad",
                BLOCKED: "bad", NOT_VALIDATED: "na"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(status: Any) -> str:
    s = str(status or NOT_VALIDATED).upper()
    return (f'<span class="badge {_BADGE_CLASS.get(s, "na")}">'
            f'{_e(s.replace("_", " "))}</span>')


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _comparable(charts: Sequence[Mapping[str, Any]]) -> list:
    return [c for c in charts if not c.get("skip_reason")]


def _count_keys(mapping: Mapping[str, Any] | None) -> int:
    """Total offending keys across sources. The engine stores duplicates /
    null_keys as the KEYS themselves (a list, or sometimes a count), so
    handle both shapes rather than assuming one."""
    total = 0
    for value in (mapping or {}).values():
        if isinstance(value, (list, tuple, set, dict)):
            total += len(value)
        else:
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                pass
    return total


def _dashboard_stats(dash: Mapping[str, Any]) -> dict:
    """Header statistics for one dashboard -- all READ from the engine's
    already-decided results, never recomputed."""
    charts = list(dash.get("charts", []))
    passed = sum(1 for c in charts if c.get("status") == PASS)
    failed_cells = sum(int(c.get("failed_cells") or 0) for c in charts)
    interactions = list(dash.get("interactions", []))
    i_pass = sum(1 for i in interactions if i.get("status") == PASS)
    complete = sum(1 for c in charts if c.get("evidence_complete"))
    return {
        "similarity": (dash.get("visual") or {}).get("similarity"),
        "visual_status": dash.get("visual_status"),
        "sheets_passed": f"{passed} / {len(charts)}",
        "failed_cells": failed_cells,
        "interactions": (f"{i_pass} / {len(interactions)}"
                         if interactions else "—"),
        "proof": (f"{round(100 * complete / len(charts))}%" if charts else "—"),
        "proof_complete": bool(dash.get("proof_complete")),
    }


def _stat_strip(stats: Mapping[str, Any]) -> str:
    sim = stats["similarity"]
    sim_txt = f"{float(sim) * 100:.1f}%" if isinstance(sim, (int, float)) else "not scored"
    cells = [
        (sim_txt, "structural visual similarity"),
        (stats["sheets_passed"], "sheets passed"),
        (str(stats["failed_cells"]), "values outside tolerance"),
        (stats["interactions"], "interactions passed"),
        (stats["proof"], "proof completeness"),
    ]
    return ('<div class="stats">'
            + "".join(f'<div class="stat"><b>{_e(v)}</b><span>{_e(l)}</span></div>'
                      for v, l in cells)
            + "</div>")


def _section_a(dash: Mapping[str, Any], vision: Mapping[str, Any] | None) -> str:
    """A. Full dashboard visual comparison -- the two REAL images side by
    side, the structural score (labelled as triage), and the Cortex vision
    verdict when a vision pass has run."""
    images = dash.get("images") or {}
    visual = dash.get("visual") or {}

    def frame(kind: str, label: str, note: str) -> str:
        path = images.get(kind)
        inner = (f'<img src="{_e(path)}" alt="{_e(label)}">' if path else
                 f'<div class="missing">No {_e(label)} available</div>')
        return (f'<figure><figcaption><b>{_e(label)}</b>'
                f'<span>{_e(note)}</span></figcaption>{inner}</figure>')

    sim = visual.get("similarity")
    thr = visual.get("threshold", 0.85)
    sim_line = (f"Structural similarity {float(sim):.3f} against a {thr} "
                f"threshold" if isinstance(sim, (int, float))
                else "Structural similarity not scored")

    checks = "".join(
        f"<tr><td>{_e(c.get('name'))}</td><td>{_e(c.get('observed'))}</td>"
        f"<td>{_e(c.get('threshold'))}</td>"
        f"<td>{_badge(c.get('status', NOT_VALIDATED))}</td></tr>"
        for c in visual.get("checks", []))

    vision_html = ""
    if vision:
        verdict = str(vision.get("verdict", "UNKNOWN")).upper()
        v_status = {"PASS": PASS, "BUG": FAIL}.get(verdict, NOT_VALIDATED)
        omitted = vision.get("omitted_sheets") or []
        vision_html = (
            '<div class="vision">'
            f'<div class="vision-head"><b>Cortex vision verdict</b>'
            f'{_badge(v_status)}</div>'
            f'<p>{_e(vision.get("explanation") or "No explanation returned.")}</p>'
            + (f'<p class="note">Not compared (the static exporter cannot draw '
               f'these sheets): {_e(", ".join(omitted))}</p>' if omitted else "")
            + '<p class="note">An AI vision model read both images '
              'independently and compared them. It judges only the images it '
              'was given; it never computes a number. Shown beside the '
              'structural score, not instead of it.</p></div>')
    else:
        vision_html = ('<p class="note">Cortex vision has not been run for '
                       'this dashboard. The structural score above is an '
                       'edge-difference triage metric only — it cannot tell a '
                       'faithful migration from an unrelated chart, so it is '
                       'not sufficient evidence on its own.</p>')

    return (
        '<h3>A. Full dashboard visual comparison</h3>'
        '<p class="lead">Tableau\'s own REST-rendered view against the '
        'migrated app\'s rendered charts.</p>'
        '<div class="screens">'
        + frame("tableau", "Tableau", "Tableau REST render")
        + frame("streamlit", "Streamlit", "app chart render")
        + "</div>"
        + f'<p class="proof-line">{_e(sim_line)} · visual gate: '
          f'{_badge(dash.get("visual_status"))}</p>'
        + (f'<div class="table-wrap"><table><thead><tr><th>Visual check</th>'
           f'<th>Observed</th><th>Threshold</th><th>Status</th></tr></thead>'
           f'<tbody>{checks}</tbody></table></div>' if checks else "")
        + vision_html)


def _chart_index(charts: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for c in charts:
        counts = c.get("source_counts") or {}
        three_way = (f"{counts.get('tableau', 0)} / {counts.get('streamlit', 0)}"
                     f" / {counts.get('backend', 0)}")
        formulas = c.get("formula_results") or []
        f_txt = ("—" if not formulas else
                 ", ".join(sorted({str(f.get("classification", "")).replace("_", " ").title()
                                   for f in formulas})))
        inter = c.get("interaction_results") or []
        i_txt = ("—" if not inter else
                 f"{sum(1 for i in inter if i.get('status') == PASS)} / {len(inter)}")
        rows.append(
            f"<tr><td><a href=\"#chart-{_e(c.get('id'))}\">{_e(c.get('title'))}</a></td>"
            f"<td>{_e(', '.join(c.get('grain') or []) or '—')}</td>"
            f"<td>{_e(three_way)}</td>"
            f"<td>{_e(c.get('failed_cells', 0))}</td>"
            f"<td>{_e(f_txt)}</td><td>{_e(i_txt)}</td>"
            f"<td>{_badge(c.get('status'))}</td></tr>")
    return ('<div class="table-wrap"><table><thead><tr><th>Sheet</th>'
            '<th>Validation grain</th><th>Rows T / S / B</th>'
            '<th>Values outside tolerance</th><th>Formula</th>'
            '<th>Interactions</th><th>Status</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _chart_contract(charts: Sequence[Mapping[str, Any]]) -> str:
    """The chart data CONTRACT: what was compared, at which grain, and what
    shape each source actually returned. Makes the comparison auditable
    without opening every chart."""
    rows = []
    for c in charts:
        counts = c.get("source_counts") or {}
        measures = ", ".join(m.get("name", "") for m in c.get("measures") or []) or "—"
        if c.get("skip_reason"):
            rows.append(
                f"<tr><td>{_e(c.get('title'))}</td><td colspan=\"6\" "
                f"class=\"muted\">Not compared — {_e(c['skip_reason'])}</td>"
                f"<td>{_badge(BLOCKED)}</td></tr>")
            continue
        key_check = (f"{'keys match' if c.get('key_set_match') else 'key sets differ'}"
                     f"; {'order matches' if c.get('order_match') else 'order differs'}")
        rows.append(
            f"<tr><td>{_e(c.get('title'))}</td>"
            f"<td>{_e(', '.join(c.get('grain') or []) or '—')}</td>"
            f"<td>{_e(measures)}</td>"
            f"<td>{_e(counts.get('tableau', 0))} rows</td>"
            f"<td>{_e(counts.get('streamlit', 0))} rows</td>"
            f"<td>{_e(counts.get('backend', 0))} rows</td>"
            f"<td>{_e(key_check)}</td>"
            f"<td class=\"num\">{_e(_num(c.get('max_difference')))}</td>"
            f"<td>{_badge(c.get('status'))}</td></tr>")
    return ('<h3>Chart data contract</h3>'
            '<p class="lead">Validation follows each chart\'s own displayed '
            'grain and measures. A grand total is never used as proof for a '
            'dimensional chart.</p>'
            '<div class="table-wrap"><table><thead><tr><th>Chart</th>'
            '<th>Validation grain</th><th>Displayed measures</th>'
            '<th>Tableau</th><th>Streamlit</th><th>Backend</th>'
            '<th>Key / order check</th><th class="num">Max diff</th>'
            '<th>Status</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _pair_verdict(chart: Mapping[str, Any], left: str, right: str) -> str:
    """PASS/FAIL/BLOCKED for one SOURCE PAIR, derived from the engine's own
    per-cell pair_diffs -- not a second judgment, a regrouping of the one
    already made."""
    compared = worst = 0
    failed = False
    for row in chart.get("comparison_rows", []):
        for m in row.get("measures", []):
            diff = (m.get("pair_diffs") or {}).get(f"{left}_{right}")
            if diff is None:
                continue
            compared += 1
            try:
                worst = max(worst, float(diff))
                if float(diff) > float(m.get("tolerance") or 0):
                    failed = True
            except (TypeError, ValueError):
                pass
    if not compared:
        return (f'<div class="pair"><b>{left.title()} vs {right.title()}</b>'
                f'{_badge(BLOCKED)}<span>no comparable values — one side was '
                f'never supplied</span></div>')
    status = FAIL if failed else PASS
    return (f'<div class="pair"><b>{left.title()} vs {right.title()}</b>'
            f'{_badge(status)}<span>{compared} value(s) compared, '
            f'max difference {_num(worst)}</span></div>')


def _chart_record(chart: Mapping[str, Any]) -> str:
    """One expandable per-chart record: the actual compared values."""
    title = _e(chart.get("title"))
    head = (f'<summary><span><b>{title}</b><small>'
            f'{_e(chart.get("chart_type", "chart"))}'
            + (f' · grain: {_e(", ".join(chart.get("grain") or []))}'
               if chart.get("grain") else "")
            + f'</small></span>{_badge(chart.get("status"))}</summary>')

    if chart.get("skip_reason"):
        return (f'<details class="chart" id="chart-{_e(chart.get("id"))}" open>'
                f'{head}<div class="chart-body">'
                f'<p class="proof-line">Not validated — {_e(chart["skip_reason"])}</p>'
                '<p class="note">Listed rather than omitted: an omitted chart '
                'is indistinguishable from a validated one.</p>'
                '</div></details>')

    counts = chart.get("source_counts") or {}
    dupes = chart.get("duplicates") or {}
    nulls = chart.get("null_keys") or {}
    facts = [
        (f"{counts.get('tableau', 0)} / {counts.get('streamlit', 0)} / "
         f"{counts.get('backend', 0)}", "source row counts (T/S/B)"),
        ("yes" if chart.get("key_set_match") else "no", "key sets match"),
        ("yes" if chart.get("order_match") else "no", "visual order matches"),
        # The engine reports duplicates/null_keys as the offending KEYS
        # themselves (a list per source), not a count -- size them, don't
        # int() them.
        (str(_count_keys(dupes) + _count_keys(nulls)),
         "duplicate or null keys"),
        (_num(chart.get("max_difference")), "maximum difference"),
        (str(chart.get("failed_cells", 0)), "values outside tolerance"),
    ]
    stat_html = ('<div class="stats small">'
                 + "".join(f'<div class="stat"><b>{_e(v)}</b><span>{_e(l)}</span></div>'
                           for v, l in facts) + "</div>")

    pairs = ('<div class="pairs">'
             + _pair_verdict(chart, "tableau", "streamlit")
             + _pair_verdict(chart, "streamlit", "backend")
             + _pair_verdict(chart, "tableau", "backend") + "</div>")

    grain = list(chart.get("grain") or [])
    rows = chart.get("comparison_rows") or []
    shown = rows if len(rows) <= 20 else [r for r in rows if r.get("status") != PASS][:20]
    body = []
    for row in shown:
        for m in row.get("measures", []):
            fm = m.get("formatted") or {}
            body.append(
                "<tr>"
                + "".join(f"<td>{_e(row.get('key', {}).get(g))}</td>" for g in grain)
                + f"<td>{_e(m.get('name'))}</td>"
                + f'<td class="num">{_e(fm.get("tableau"))}</td>'
                + f'<td class="num">{_e(fm.get("streamlit"))}</td>'
                + f'<td class="num">{_e(fm.get("backend"))}</td>'
                + f'<td class="num">{_e(m.get("formatted_difference"))}</td>'
                + f'<td class="num">{_e(m.get("formatted_tolerance"))}</td>'
                + f"<td>{_badge(m.get('status'))}</td></tr>")
    table = ""
    if body:
        table = ('<div class="table-wrap"><table><thead><tr>'
                 + "".join(f"<th>{_e(g)}</th>" for g in grain)
                 + '<th>Measure</th><th class="num">Tableau</th>'
                   '<th class="num">Streamlit</th><th class="num">Backend</th>'
                   '<th class="num">Difference</th><th class="num">Tolerance</th>'
                   '<th>Status</th></tr></thead>'
                 f'<tbody>{"".join(body)}</tbody></table></div>')
        note = ("All rows shown." if len(rows) <= 20 else
                f"Showing rows needing attention; all {len(rows)} compared rows "
                f"are in the chart's comparison.csv.")
        table += f'<p class="note">{_e(note)}</p>'
    csv_link = ""
    if chart.get("comparison_csv"):
        csv_link = (f'<p><a class="button-link" href="{_e(chart["comparison_csv"])}">'
                    'Download this chart\'s full comparison CSV</a></p>')
    return (f'<details class="chart" id="chart-{_e(chart.get("id"))}"'
            + ("" if chart.get("status") == PASS else " open") + ">"
            + head + '<div class="chart-body">'
            + stat_html + pairs + table + csv_link + "</div></details>")


def _section_c(dash: Mapping[str, Any]) -> str:
    formulas = dash.get("formulas") or []
    if not formulas:
        return ('<h3>C. Logic and calculation validation</h3>'
                '<p class="note">No formula evidence was captured for this '
                'dashboard.</p>')
    rows = "".join(
        f"<tr><td>{_e(f.get('metric'))}</td>"
        f"<td><code>{_e(f.get('tableau'))}</code></td>"
        f"<td><code>{_e(f.get('streamlit'))}</code></td>"
        f"<td>{_e(str(f.get('classification', '')).replace('_', ' ').title())}</td>"
        f"<td>{_e(f.get('impact'))}</td>"
        f"<td>{_badge(f.get('status'))}</td></tr>" for f in formulas)
    return ('<h3>C. Logic and calculation validation</h3>'
            '<p class="lead">The workbook\'s own Tableau formula against the '
            'SQL the migrated app actually runs.</p>'
            '<div class="table-wrap"><table><thead><tr><th>Metric</th>'
            '<th>Tableau formula</th><th>Generated SQL</th>'
            '<th>Classification</th><th>Impact</th><th>Status</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _section_d(dash: Mapping[str, Any]) -> str:
    inter = dash.get("interactions") or []
    if not inter:
        return ('<h3>D. Dashboard filters and interactions</h3>'
                '<p class="note">No interaction proof was captured for this '
                'dashboard.</p>')
    rows = "".join(
        f"<tr><td>{_e(i.get('name'))}</td><td>{_e(i.get('tableau'))}</td>"
        f"<td>{_e(i.get('streamlit'))}</td><td>{_e(i.get('proof'))}</td>"
        f"<td>{_badge(i.get('status'))}</td></tr>" for i in inter)
    return ('<h3>D. Dashboard filters and interactions</h3>'
            '<div class="table-wrap"><table><thead><tr><th>Interaction</th>'
            '<th>Tableau</th><th>Streamlit</th><th>Proof</th><th>Status</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _exceptions(run: Mapping[str, Any]) -> str:
    """Every non-PASS finding across the workbook, in one register."""
    rows = []
    for dash in run.get("dashboards", []):
        if dash.get("visual_status") != PASS:
            rows.append((dash.get("name"), "Visual", "Full dashboard",
                         dash.get("visual_status"),
                         "Visual evidence incomplete or below threshold"))
        for c in dash.get("charts", []):
            if c.get("status") == PASS:
                continue
            detail = c.get("skip_reason") or (
                f"{c.get('failed_cells', 0)} value(s) outside tolerance"
                if c.get("failed_cells") else "evidence incomplete")
            rows.append((dash.get("name"), "Chart data", c.get("title"),
                         c.get("status"), detail))
        for f in dash.get("formulas", []):
            if f.get("status") != PASS:
                rows.append((dash.get("name"), "Formula", f.get("metric"),
                             f.get("status"), f.get("impact")))
        for i in dash.get("interactions", []):
            if i.get("status") != PASS:
                rows.append((dash.get("name"), "Interaction", i.get("name"),
                             i.get("status"), i.get("proof")))
    if not rows:
        return ('<section class="chapter" id="exceptions"><h2>Consolidated '
                'exceptions</h2><p class="ok-line">No exceptions — every '
                'dashboard, chart, formula and interaction passed.</p></section>')
    body = "".join(
        f"<tr><td>{_e(d)}</td><td>{_e(kind)}</td><td>{_e(item)}</td>"
        f"<td>{_badge(status)}</td><td>{_e(detail)}</td></tr>"
        for d, kind, item, status, detail in rows)
    return ('<section class="chapter" id="exceptions"><h2>Consolidated '
            'exceptions</h2>'
            f'<p class="lead">{len(rows)} item(s) need attention before '
            'sign-off.</p>'
            '<div class="table-wrap"><table><thead><tr><th>Dashboard</th>'
            '<th>Type</th><th>Item</th><th>Status</th><th>Detail</th>'
            '</tr></thead>'
            f'<tbody>{body}</tbody></table></div></section>')


def _evidence(run: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
    inputs = [
        ("Workbook", run.get("workbook")),
        ("Run id", run.get("run_id")),
        ("Environment", run.get("environment")),
        ("Generated at", run.get("generated_at")),
        ("Tableau source", meta.get("tableau_source")
         or "not connected — Tableau evidence absent for this run"),
        ("Backend queried", meta.get("backend") or "—"),
        ("Cortex vision", meta.get("vision_note") or "not run"),
    ]
    rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in inputs)
    return ('<section class="chapter" id="evidence"><h2>Evidence and '
            'reproducibility</h2><h3>Run inputs</h3>'
            f'<div class="table-wrap"><table><tbody>{rows}</tbody></table></div>'
            '<h3>Generated package</h3><ul>'
            '<li><code>dashboard_validation_report.html</code> — this report</li>'
            '<li><code>validation_report.html</code> — the compact engine report</li>'
            '<li><code>validation_summary.json</code> — every computed verdict</li>'
            '<li><code>issues.csv</code> — machine-readable exception list</li>'
            '<li><code>evidence/&lt;dashboard&gt;/</code> — the Tableau and app '
            'images actually compared</li>'
            '<li><code>evidence/&lt;dashboard&gt;/charts/&lt;chart&gt;/comparison.csv</code>'
            ' — every compared row at the chart\'s own grain</li>'
            '</ul><p class="note">Tolerance is derived from each measure\'s own '
            'number format in the workbook (a whole-dollar currency measure '
            'reconciles at ±$0.50), never a flat global percentage.</p>'
            '</section>')


def _signoff(run: Mapping[str, Any]) -> str:
    status = run.get("status")
    eligible = status == PASS
    return ('<section class="chapter" id="signoff"><h2>Sign-off record</h2>'
            '<div class="table-wrap"><table><thead><tr><th>Role</th>'
            '<th>Decision</th><th>Name</th><th>Date</th></tr></thead><tbody>'
            f'<tr><td>Automated validation</td><td>{_badge(status)}</td>'
            f'<td>validation_report engine</td>'
            f'<td>{_e(run.get("generated_at"))}</td></tr>'
            '<tr><td>Analytics owner</td><td class="muted">pending</td>'
            '<td class="fill"></td><td class="fill"></td></tr>'
            '<tr><td>Product owner</td><td class="muted">pending</td>'
            '<td class="fill"></td><td class="fill"></td></tr>'
            '</tbody></table></div>'
            + ('<p class="ok-line">Eligible for sign-off.</p>' if eligible else
               '<p class="warn-line">Not eligible for sign-off — clear the '
               'consolidated exceptions above first. No proof, no pass.</p>')
            + '</section>')


def _summary(run: Mapping[str, Any]) -> str:
    rows = "".join(
        f'<tr><td><a href="#dash-{_e(d.get("id"))}">{_e(d.get("name"))}</a></td>'
        f'<td>{_e(len(d.get("charts", [])))}</td>'
        f'<td>{_badge(d.get("visual_status"))}</td>'
        f'<td>{_e(_dashboard_stats(d)["sheets_passed"])}</td>'
        f'<td>{_e(sum(int(c.get("failed_cells") or 0) for c in d.get("charts", [])))}</td>'
        f'<td>{_badge(d.get("status"))}</td></tr>'
        for d in run.get("dashboards", []))
    s = run.get("summary") or {}
    return ('<section class="chapter" id="summary"><h2>Workbook summary</h2>'
            f'<div class="stats"><div class="stat"><b>{_e(s.get("dashboards", 0))}</b>'
            '<span>dashboards</span></div>'
            f'<div class="stat"><b>{_e(s.get("charts", 0))}</b><span>charts</span></div>'
            f'<div class="stat"><b>{_e(s.get("passed", 0))}</b><span>dashboards passed</span></div>'
            f'<div class="stat"><b>{_e(s.get("failed_or_blocked", 0))}</b>'
            '<span>failed or blocked</span></div></div>'
            '<div class="table-wrap"><table><thead><tr><th>Dashboard</th>'
            '<th>Charts</th><th>Visual</th><th>Sheets passed</th>'
            '<th>Values outside tolerance</th><th>Status</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></section>')


def render_dashboard_report(run: Mapping[str, Any],
                            meta: Mapping[str, Any] | None = None,
                            vision: Mapping[str, Any] | None = None) -> str:
    """Render the validated `run` (validation_report.validate_run's output)
    as the full client-facing dashboard validation report.

    `vision` maps a dashboard NAME to a parity.vision_validate_dashboard
    result; omit it and section A honestly states vision was not run."""
    meta = dict(meta or {})
    vision = dict(vision or {})
    dashboards = list(run.get("dashboards", []))

    chapters = []
    for index, dash in enumerate(dashboards, start=1):
        stats = _dashboard_stats(dash)
        charts = list(dash.get("charts", []))
        chapters.append(
            f'<section class="chapter dashboard" id="dash-{_e(dash.get("id"))}">'
            f'<div class="chapter-head"><div>'
            f'<small>Dashboard {index} of {len(dashboards)}</small>'
            f'<h2>{_e(dash.get("name"))}</h2>'
            f'<p class="lead">{_e(len(charts))} sheet(s) validated.</p></div>'
            f'<div class="verdict">{_badge(dash.get("status"))}'
            + ('<span class="ok-line">Eligible for sign-off</span>'
               if dash.get("status") == PASS else
               '<span class="warn-line">Not eligible for sign-off</span>')
            + '</div></div>'
            + _stat_strip(stats)
            + _section_a(dash, vision.get(dash.get("name")))
            + '<h3>B. Per-chart data validation</h3>'
            '<p class="lead">Open a chart to see the values actually '
            'compared.</p>'
            + _chart_index(charts)
            + _chart_contract(charts)
            + "".join(_chart_record(c) for c in charts)
            + _section_c(dash)
            + _section_d(dash)
            + "</section>")

    nav = "".join(f'<a href="#dash-{_e(d.get("id"))}">{_e(d.get("name"))}</a>'
                  for d in dashboards)
    status = run.get("status")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(run.get("workbook", "Workbook"))} — Dashboard Validation</title>
<style>
:root{{--ink:#12212e;--muted:#5f7488;--line:#d9e2ea;--soft:#f5f8fa;--paper:#fff;
--canvas:#eef2f6;--ok:#0b6f52;--ok-bg:#e4f5ee;--warn:#8a5b00;--warn-bg:#fff4dc;
--bad:#a8301f;--bad-bg:#fceae7;--accent:#1a6ca8}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--canvas);color:var(--ink);
font:14px/1.5 "Segoe UI",system-ui,Arial,sans-serif}}
.page{{max-width:1400px;margin:auto;background:var(--paper);min-height:100vh}}
header,main,footer{{padding:26px 32px}}
header{{border-bottom:1px solid var(--line)}}
h1{{margin:0 0 6px;font-size:28px}} h2{{margin:0;font-size:21px}}
h3{{margin:26px 0 8px;font-size:15px;text-transform:uppercase;
letter-spacing:.04em;color:var(--accent)}}
p{{margin:6px 0}} small{{color:var(--muted)}}
.meta{{color:var(--muted)}}
nav{{position:sticky;top:0;z-index:5;display:flex;gap:4px;padding:9px 32px;
overflow:auto;border-bottom:1px solid var(--line);background:#fff}}
nav a{{white-space:nowrap;padding:6px 10px;color:var(--ink);
text-decoration:none;border-radius:4px;font-size:13px}}
nav a:hover{{background:var(--soft)}}
.decision{{margin-top:14px;padding:14px 16px;border-left:5px solid var(--bad);
background:var(--bad-bg)}}
.decision.ok{{border-color:var(--ok);background:var(--ok-bg)}}
.chapter{{padding-top:30px;margin-top:30px;border-top:3px solid var(--ink)}}
.chapter-head{{display:flex;justify-content:space-between;gap:20px;
align-items:flex-start}}
.verdict{{text-align:right;display:flex;flex-direction:column;gap:6px;
align-items:flex-end}}
.lead{{color:var(--muted)}}
.stats{{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}}
.stat{{flex:1;min-width:130px;border:1px solid var(--line);border-radius:7px;
padding:12px 14px;background:var(--soft)}}
.stat b{{display:block;font-size:20px;line-height:1.2}}
.stat span{{font-size:11.5px;color:var(--muted);text-transform:uppercase;
letter-spacing:.03em}}
.stats.small .stat b{{font-size:16px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:7px;
margin:10px 0}}
table{{width:100%;border-collapse:collapse;min-width:680px}}
th,td{{padding:9px 11px;border-bottom:1px solid #eaeef2;text-align:left;
vertical-align:top}}
th{{background:var(--soft);font-size:11px;text-transform:uppercase;
letter-spacing:.03em;color:var(--muted)}}
tr:last-child td{{border-bottom:0}}
.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.muted{{color:var(--muted)}} .fill{{background:#fafcfd}}
.badge{{display:inline-block;padding:3px 9px;border-radius:4px;font-size:11px;
font-weight:700;letter-spacing:.03em}}
.ok{{color:var(--ok);background:var(--ok-bg)}}
.warn{{color:var(--warn);background:var(--warn-bg)}}
.bad{{color:var(--bad);background:var(--bad-bg)}}
.na{{color:#4a5b6b;background:#eef1f4}}
.screens{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
figure{{margin:0;border:1px solid var(--line);border-radius:7px;overflow:hidden}}
figure img{{display:block;width:100%;height:auto}}
figcaption{{padding:8px 11px;background:var(--soft);font-size:12px;
display:flex;justify-content:space-between;gap:10px}}
figcaption span{{color:var(--muted)}}
.missing{{min-height:220px;display:grid;place-items:center;color:var(--bad);
background:var(--bad-bg);padding:20px;text-align:center}}
.proof-line{{padding:9px 12px;background:var(--soft);border-radius:6px}}
.note{{color:var(--muted);font-size:12.5px}}
.ok-line{{color:var(--ok);font-weight:600}}
.warn-line{{color:var(--warn);font-weight:600}}
.vision{{border:1px solid var(--line);border-left:4px solid var(--accent);
border-radius:7px;padding:12px 14px;margin:12px 0;background:#f8fbfd}}
.vision-head{{display:flex;justify-content:space-between;align-items:center;
gap:12px;margin-bottom:4px}}
.pairs{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}}
.pair{{flex:1;min-width:230px;border:1px solid var(--line);border-radius:6px;
padding:10px 12px}}
.pair b{{display:block;font-size:12.5px}}
.pair span{{display:block;color:var(--muted);font-size:12px;margin-top:3px}}
details.chart{{border:1px solid var(--line);border-radius:7px;margin-top:9px}}
details.chart>summary{{cursor:pointer;padding:11px 13px;display:flex;
justify-content:space-between;gap:12px;align-items:center}}
summary small{{display:block;color:var(--muted);font-weight:400}}
.chart-body{{padding:13px;border-top:1px solid var(--line);background:#fbfdfe}}
.button-link{{display:inline-block;padding:7px 11px;border:1px solid var(--line);
border-radius:5px;color:var(--ink);text-decoration:none;font-size:12.5px}}
code{{font-family:Consolas,monospace;font-size:12px}}
ul{{margin:6px 0 6px 18px;padding:0}} li{{margin:3px 0}}
footer{{border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
@media(max-width:820px){{.screens{{grid-template-columns:1fr}}
header,main,footer{{padding:16px}}nav{{padding-left:10px}}}}
</style></head><body><div class="page">
<header><small>Automated migration assurance</small>
<h1>{_e(run.get("workbook"))} — Dashboard Validation</h1>
<p class="meta">Run {_e(run.get("run_id"))} · {_e(run.get("environment"))} ·
generated {_e(run.get("generated_at"))}</p>
<div class="decision{' ok' if status == PASS else ''}">
<b>Workbook decision: {_e(status)}</b>
<p>No proof, no pass. Status is derived from the visual gate, chart-grain
data comparison, formula classification, interaction proof and
evidence completeness. A chart that could not be compared is reported
BLOCKED with its reason, never silently omitted.</p></div></header>
<nav><a href="#summary">Summary</a>{nav}<a href="#exceptions">Exceptions</a>
<a href="#evidence">Evidence</a><a href="#signoff">Sign-off</a></nav>
<main>{_summary(run)}{"".join(chapters)}{_exceptions(run)}
{_evidence(run, meta)}{_signoff(run)}</main>
<footer>Generated by the Tableau to Streamlit-in-Snowflake accelerator.
Every verdict shown was computed by the deterministic comparison engine;
any Cortex vision verdict is labelled as such and never overrides it.</footer>
</div></body></html>"""

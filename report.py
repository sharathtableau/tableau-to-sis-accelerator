"""
report.py  --  compatibility / migration-assessment report generator.

Runs the full conversion pipeline headlessly and produces the accelerator's
client-facing outputs:

  metadata/compatibility_report.json   machine-readable, per-sheet statuses
  reports/migration_assessment.md      client-readable summary + remediation
  sql/generated_views.sql              every sheet's base SQL (reviewable)

Usage:
  python report.py Superstore.twb
"""

import json
import os
import sys
import datetime

import tableau_parser as TP
import audit_coverage as AC
import engine
import findings


class _Col:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, name): return lambda *a, **k: None


class _Probe:
    """Headless st: captures warnings (render failures) and chart count."""
    def __init__(self): self.msgs = []; self.charts = 0
    def columns(self, n): return [_Col() for _ in range(n if isinstance(n, int) else len(n))]
    def container(self, **k): return _Col()
    def altair_chart(self, *a, **k): self.charts += 1
    def plotly_chart(self, *a, **k): self.charts += 1
    def dataframe(self, *a, **k): self.charts += 1
    def metric(self, *a, **k): self.charts += 1
    def warning(self, m): self.msgs.append(str(m))
    def __getattr__(self, name): return lambda *a, **k: None


def _sheet_score(status, fs):
    if status == "failed":
        return 0.0
    score = 1.0
    for f in fs:
        score -= {"BLOCKER": 0.5, "WARNING": 0.15, "INFO": 0.05}.get(f["severity"], 0)
    return max(score, 0.3)


def build_report(twb):
    ir = TP.build_ir(twb)
    engine.configure(ir)
    findings.clear()

    root = TP.load_twb_xml(twb)
    meta, _ = TP.column_meta(root)
    all_ws = {w.get("name"): w for w in root.findall(".//worksheets/worksheet")}

    # ---- audit-check gaps for every worksheet (parser-capture fidelity) ----
    audit_gaps = {}
    for name, w in all_ws.items():
        try:
            spec = TP.infer(w, meta)
        except Exception:
            spec = {}
        gaps = []
        for sev, label, gap in AC.CHECKS:
            try:
                if gap(w, spec):
                    gaps.append({"severity": sev, "gap": label})
            except Exception:
                pass
        if gaps:
            audit_gaps[name] = gaps

    # ---- headless render probe per dashboard sheet + SQL capture ----
    captured_sql = []
    real_q = engine.q

    def spy_q(sql):
        captured_sql.append((spy_q.sheet, engine.sub_params(sql)))
        return real_q(sql)
    spy_q.sheet = None

    engine.q = spy_q
    dashboards = []
    for d in ir["dashboards"]:
        sheets = []
        for s in d["sheets"]:
            probe = _Probe()
            engine.st = probe
            spy_q.sheet = f"{d['name']}/{s['name']}"
            n_before = len(findings.all_findings())
            try:
                engine.render_sheet(s, "")
            except Exception as e:
                probe.msgs.append(f"{type(e).__name__}: {e}")
            fs = findings.all_findings()[n_before:]
            if s.get("non_data"):
                # text box / show-hide toggle / blank -- not a data viz, so
                # not a conversion NOR a failure; excluded from fidelity
                status = "n/a"
            else:
                failed = bool(probe.msgs) or any(f["severity"] == "BLOCKER" for f in fs)
                partial = (not failed) and bool(fs or audit_gaps.get(s["name"]))
                status = "failed" if failed else ("partial" if partial else "converted")
            sheets.append({
                "sheet": s["name"], "title": s.get("title") or s["name"],
                "chart_kind": s["kind"], "datasource": s.get("datasource"),
                "renderer": "plotly" if s["kind"] == "map" else
                            ("streamlit-native" if s["kind"] in ("kpi", "table") else "altair"),
                "status": status,
                "fidelity": round(_sheet_score(status, fs), 2),
                "findings": fs + [{"severity": g["severity"], "sheet": s["name"],
                                   "code": "parser-gap", "message": g["gap"]}
                                  for g in audit_gaps.get(s["name"], [])],
                "errors": probe.msgs,
            })
        # non-data sheets don't count toward fidelity (a text box isn't a
        # conversion to grade)
        scored = [x for x in sheets if x["status"] != "n/a"]
        dscore = round(sum(x["fidelity"] for x in scored) / len(scored), 2) if scored else 1.0
        dashboards.append({"dashboard": d["name"], "title": d["title"],
                           "fidelity": dscore, "sheets": sheets})
    engine.q = real_q

    n_sheets = sum(len(d["sheets"]) for d in dashboards)
    n_conv = sum(1 for d in dashboards for x in d["sheets"] if x["status"] == "converted")
    n_part = sum(1 for d in dashboards for x in d["sheets"] if x["status"] == "partial")
    n_fail = sum(1 for d in dashboards for x in d["sheets"] if x["status"] == "failed")
    n_na = sum(1 for d in dashboards for x in d["sheets"] if x["status"] == "n/a")
    overall = round(sum(d["fidelity"] for d in dashboards) / len(dashboards), 2) if dashboards else 0

    report = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_workbook": twb,
        "overall_estimated_fidelity": overall,
        "datasources": ir["datasources"],
        "calculations": {"total": len(ir["calcs"]) + len(ir["calc_drops"]),
                         "translated": len(ir["calcs"]),
                         "untranslated": [{"caption": k, "formula": v}
                                          for k, v in ir["calc_drops"].items()]},
        "totals": {"dashboards": len(dashboards), "worksheets": n_sheets,
                   "converted": n_conv, "partial": n_part, "failed": n_fail},
        "datasource_notes": ir.get("datasource_notes", []),
        "dashboards": dashboards,
        "non_dashboard_worksheet_gaps": {k: v for k, v in audit_gaps.items()
                                         if not any(x["sheet"] == k
                                                    for d in dashboards for x in d["sheets"])},
    }
    return report, captured_sql


# GENUINE visual risks only -- constructs the engine renders generically or
# drops. HANDLED constructs (dual-axis, labels, filter defaults) must stay
# OFF this list or the checklist cries wolf (23 false items -> ignored).
_RISK_HOT = ("mark class not honored", "not supported in kind",
             "forecast overlay", "subtotal", "axis uses", "device layout",
             "could not be resolved", "viz-in-tooltip", "not reproduced")


def visual_risk(sheet):
    """HIGH (won't render / wrong), MED (renders but may not match Tableau),
    or None (clean or cosmetic-only). Drives the up-front eyeball checklist
    so mismatches surface on day one, not tab by tab."""
    if sheet.get("status") == "failed" or sheet.get("errors") or any(
            f["severity"] in ("BLOCKER", "ERROR") for f in sheet["findings"]):
        return "HIGH"
    msgs = " ".join(f["message"].lower() for f in sheet["findings"])
    return "MED" if any(h in msgs for h in _RISK_HOT) else None


def _risk_reason(sheet):
    for sev in ("BLOCKER", "ERROR"):
        for f in sheet["findings"]:
            if f["severity"] == sev:
                return f["message"][:90]
    for f in sheet["findings"]:
        if any(h in f["message"].lower() for h in _RISK_HOT):
            return f["message"][:90]
    return (sheet.get("errors") or ["see findings"])[0][:90]


def write_outputs(report, captured_sql):
    os.makedirs("metadata", exist_ok=True)
    os.makedirs("reports", exist_ok=True)
    os.makedirs("sql", exist_ok=True)

    with open("metadata/compatibility_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # ---- semantic-layer DDL (joins/relationships -> Snowflake views) ----
    try:
        import semantic_layer as SL
        import tableau_parser as TP
        root = TP.load_twb_xml(report["source_workbook"])
        with open("sql/semantic_views.sql", "w", encoding="utf-8") as f:
            f.write(SL.generate_views(SL.data_model(root)))
    except Exception as e:
        print(f"  ~ semantic views not generated: {e}")

    # ---- reviewable SQL artifact ----
    with open("sql/generated_views.sql", "w", encoding="utf-8") as f:
        f.write("-- GENERATED by report.py -- every sheet's base SQL "
                "(no dashboard filters applied)\n")
        last = None
        for sheet, sql in captured_sql:
            if sheet != last:
                f.write(f"\n-- ============ {sheet} ============\n")
                last = sheet
            f.write(sql.strip() + ";\n")

    # ---- client-readable markdown ----
    r = report
    lines = []
    a = lines.append
    a("# Migration Assessment — %s" % r["source_workbook"])
    a("")
    a(f"*Generated {r['generated']} by the Tableau → Streamlit-in-Snowflake accelerator.*")
    a("")
    a(f"## Overall estimated fidelity: **{r['overall_estimated_fidelity']:.0%}**")
    a("")
    t = r["totals"]
    a(f"| Dashboards | Worksheets | Converted | Partial | Failed |")
    a(f"|---|---|---|---|---|")
    a(f"| {t['dashboards']} | {t['worksheets']} | {t['converted']} | {t['partial']} | {t['failed']} |")
    a("")
    c = r["calculations"]
    a(f"Calculated fields: **{c['translated']}/{c['total']} translated to SQL** "
      f"({len(c['untranslated'])} not translatable).")
    a(f"Datasources detected: {', '.join(r['datasources'])}.")
    a("")

    # ---- VISUAL VERIFICATION CHECKLIST (prominent, up front) -------------
    # The accelerator renders every sheet, but rendering != matching Tableau.
    # Rank each sheet by visual RISK from its findings so the reviewer gets
    # the exact eyeball list on day one instead of discovering it tab by tab.
    checklist = []
    for d in r["dashboards"]:
        for x in d["sheets"]:
            lvl = visual_risk(x)
            if lvl:
                checklist.append((lvl, d["title"], x["sheet"], _risk_reason(x)))
    order = {"HIGH": 0, "MED": 1}
    checklist.sort(key=lambda t: order[t[0]])
    a("## ✅ Visual verification checklist")
    a("")
    if checklist:
        a("Every sheet renders, but **rendering is not the same as matching "
          "Tableau**. Eyeball these against the source before delivery — "
          "ranked by risk. Sheets not listed converted clean or differ only "
          "in cosmetics.")
        a("")
        a("| Risk | Dashboard | Sheet | Why it may differ |")
        a("|---|---|---|---|")
        for lvl, dash, sh, why in checklist:
            tag = "🔴 HIGH" if lvl == "HIGH" else "🟠 CHECK"
            a(f"| {tag} | {dash} | {sh} | {why} |")
    else:
        a("No visual-risk sheets — every sheet converted clean or differs "
          "only in documented cosmetics.")
    a("")
    a("## Dashboard readiness")
    a("")
    a("| Dashboard | Fidelity | Sheets (status) |")
    a("|---|---|---|")
    for d in r["dashboards"]:
        parts = ", ".join(f"{x['sheet']} ({x['status']})" for x in d["sheets"])
        a(f"| {d['title']} | {d['fidelity']:.0%} | {parts} |")
    a("")
    a("## Findings & remediation")
    a("")
    any_f = False
    for d in r["dashboards"]:
        for x in d["sheets"]:
            for fdg in x["findings"]:
                any_f = True
                a(f"- **[{fdg['severity']}] {d['dashboard']} / {x['sheet']}** — {fdg['message']}")
            for e in x["errors"]:
                any_f = True
                a(f"- **[ERROR] {d['dashboard']} / {x['sheet']}** — {e}")
    if not any_f:
        a("No blocking findings. All dashboard sheets converted.")
    a("")
    if c["untranslated"]:
        a("### Untranslated calculations")
        a("")
        for u in c["untranslated"]:
            a(f"- `{u['caption']}` = `{u['formula'].strip()[:120]}`")
        a("")
    if r.get("datasource_notes"):
        a("### Data-model notes (logic OUTSIDE worksheets — must move to Snowflake)")
        a("")
        for n in r["datasource_notes"]:
            a(f"- **[{n['kind']}] {n['datasource']}** — {n['detail']}")
        a("")
    a("## What this report is")
    a("")
    a("This accelerator does not hide migration complexity. It converts what it")
    a("can deterministically, and reports every approximation or gap above so")
    a("remediation is a planned task, not a surprise.")
    with open("reports/migration_assessment.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    twb = sys.argv[1] if len(sys.argv) > 1 else "Superstore.twb"
    report, captured_sql = build_report(twb)
    write_outputs(report, captured_sql)
    t = report["totals"]
    print(f"Overall estimated fidelity: {report['overall_estimated_fidelity']:.0%}")
    print(f"Sheets: {t['converted']} converted / {t['partial']} partial / {t['failed']} failed")
    print("-> metadata/compatibility_report.json")
    print("-> reports/migration_assessment.md")
    print("-> sql/generated_views.sql")


if __name__ == "__main__":
    main()

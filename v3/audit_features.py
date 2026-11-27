"""
audit_features.py  --  FULL Tableau feature coverage audit.

Enumerates the entire Tableau feature surface (data model, fields, filters,
marks, dashboard/interactivity, formatting, maps), detects which features each
workbook uses, and reports the accelerator's coverage status for each. This is
the "measure the whole surface" discipline applied to ALL of Tableau, not just
calcs/colors/filters.

Coverage status:
  full     converts faithfully
  partial  converts approximately / best-effort (reported)
  gap      not converted yet (reported, on the backlog)
  scope    out of scope by design (Streamlit cannot reproduce it)

Usage:
  python audit_features.py                # all workbooks in the folder
  python audit_features.py Book.twbx
"""

import glob
import sys

import tableau_parser as TP

# (category, feature, status, detector). detector(root) -> count in that workbook.
FEATURES = [
    # ---- data model & sources ----
    ("Data model", "Extract data source (.hyper/.tde)", "full",
     lambda r: len(r.findall(".//connection[@class='hyper']"))),
    # Live Snowflake source queried directly at its own db.schema.table (2026-07-21);
    # other live classes (sqlproxy/sqlserver/...) reported honestly, not queried.
    ("Data model", "Live data source (map to table)", "full",
     lambda r: len(r.findall(".//connection[@class='snowflake']"))
               + len(r.findall(".//connection[@class='sqlserver']"))),
    ("Data model", "Joins", "partial",
     lambda r: len(r.findall(".//relation[@type='join']"))),
    # Unions materialized row-wise (UNION ALL) at onboarding (2026-07-22); file-based
    # manual unions -- wildcard/live-DB-table unions still out of scope.
    ("Data model", "Unions", "full",
     lambda r: len(r.findall(".//relation[@type='union']"))),
    # R7 (2026-07-26): any deterministic TREE now auto-models -- star AND
    # snowflake-schema chains (a dim joined to a dim), in both the view DDL and
    # the extract flatten. Genuinely ambiguous graphs (multi-fact, cyclic,
    # disconnected) still refuse with a named reason rather than guessing.
    ("Data model", "Relationships (object model)", "full",
     lambda r: len(r.findall(".//_.fcp.ObjectModelEncapsulateLegacy.false...relation"))
               or len(r.findall(".//relationships"))),
    # R7 (2026-07-26): blend link fields are extracted from the workbook's own
    # <datasource-relationship><column-mapping>, reported per sheet, offered as
    # reviewable pre-aggregate remodel SQL, and fed to the Cortex calc prompt as
    # a constraint. Still NOT auto-materialized -- a blend is a query-time link.
    ("Data model", "Data blending (multi-source sheet)", "partial",
     lambda r: sum(1 for w in r.findall(".//worksheets/worksheet")
                   if len({d.get("datasource") for d in w.findall(".//datasource-dependencies")
                           if d.get("datasource") and d.get("datasource") != "Parameters"}) > 1)),
    # Snowflake-dialect custom SQL executed verbatim as a derived table (2026-07-21,
    # execution-gated); other dialects reported honestly.
    ("Data model", "Custom SQL", "full",
     lambda r: len(r.findall(".//relation[@type='text']"))),
    ("Data model", "Data source filters", "gap",
     lambda r: len(r.findall(".//datasources/datasource/filter"))),

    # ---- fields, groups, sets, calcs ----
    ("Fields", "Calculated fields", "full",
     lambda r: sum(1 for c in r.findall(".//column/calculation[@class='tableau']"))),
    ("Fields", "Table calculations", "partial",
     lambda r: len(r.findall(".//table-calc"))),
    ("Fields", "LOD expressions (FIXED/INCLUDE/EXCLUDE)", "partial",
     lambda r: sum(f.count("{") for f in _formulas(r) if "FIXED" in f.upper()
                   or "INCLUDE" in f.upper() or "EXCLUDE" in f.upper())),
    ("Fields", "Parameters", "full",
     lambda r: len([c for c in r.findall(".//column") if c.get("param-domain-type")])),
    ("Fields", "Value aliases", "full",
     lambda r: len(r.findall(".//column/aliases"))),
    ("Fields", "Groups (member grouping)", "full",
     lambda r: _group_count(r)),
    ("Fields", "Sets", "full",
     lambda r: _set_count(r)),
    ("Fields", "Combined sets", "gap",
     lambda r: len(r.findall(".//combined-set"))),
    ("Fields", "Bins", "gap",
     lambda r: len(r.findall(".//bin"))),
    ("Fields", "Hierarchies / drill-down", "full",
     lambda r: len(r.findall(".//drill-path"))),

    # ---- filters (summarized; see audit_filters.py for detail) ----
    ("Filters", "Dimension / measure / date / interactive", "full",
     lambda r: len(r.findall(".//worksheets//filter"))),
    ("Filters", "Top / Bottom N", "full",
     lambda r: sum(1 for gf in r.findall(".//worksheets//filter//groupfilter")
                   if gf.get("function") in ("top", "top-by-field")
                   or (gf.get("function") == "end" and gf.get("count")))),
    # Context filters injected into the top-N ranking subquery (top-N WITHIN the
    # context) + dashboard-filter scoping (2026-07-22).
    ("Filters", "Context filters", "full",
     lambda r: len(r.findall(".//filter[@context='true']"))),
    ("Filters", "Action filters (cross-viz)", "scope",
     lambda r: sum(1 for f in r.findall(".//worksheets//filter")
                   if "Action (" in (f.get("column") or ""))),

    # ---- marks & analytics ----
    ("Marks & analytics", "Standard chart types (19 kinds)", "full",
     lambda r: len(r.findall(".//worksheets/worksheet"))),
    ("Marks & analytics", "Color/size/shape/label/detail encodings", "full",
     lambda r: len(r.findall(".//pane/encodings/*"))),
    ("Marks & analytics", "Dual-axis / combo charts", "full",
     lambda r: _dualaxis_count(r)),
    ("Marks & analytics", "Reference lines", "partial",
     lambda r: len(r.findall(".//reference-line"))),
    ("Marks & analytics", "Reference bands / distributions", "gap",
     lambda r: len(r.findall(".//reference-distribution"))),
    ("Marks & analytics", "Trend lines", "full",
     lambda r: len(r.findall(".//style-rule[@element='trendline']"))),
    ("Marks & analytics", "Forecasting", "gap",
     lambda r: len(r.findall(".//forecast-settings")) + len(r.findall(".//forecast"))),
    ("Marks & analytics", "Clustering", "gap",
     lambda r: len(r.findall(".//clustering"))),
    ("Marks & analytics", "Totals / subtotals", "gap",
     lambda r: len(r.findall(".//summary")) + len(r.findall(".//total"))),

    # ---- dashboard & interactivity ----
    ("Dashboard", "Zone layout (rows/columns)", "full",
     lambda r: len(r.findall(".//dashboards/dashboard//zone"))),
    ("Dashboard", "Interactive filters (quick filters)", "full",
     lambda r: len(r.findall(".//dashboards//zone[@type-v2='filter']"))),
    ("Dashboard", "Parameter controls", "full",
     lambda r: len(r.findall(".//dashboards//zone[@param]"))),
    ("Dashboard", "Dashboard actions (filter/highlight/URL)", "scope",
     lambda r: len(r.findall(".//actions/action"))),
    ("Dashboard", "Parameter / set actions", "scope",
     lambda r: len(r.findall(".//action[@class='parameter']"))
               + len(r.findall(".//action[@class='set']"))),
    ("Dashboard", "Legends", "partial",
     lambda r: len(r.findall(".//dashboards//zone[@type-v2='color']"))),
    ("Dashboard", "Viz-in-tooltip", "gap",
     lambda r: len(r.findall(".//customized-tooltip//*[@name]"))
               if r.findall(".//customized-tooltip//sheet") else 0),
    ("Dashboard", "Navigation / button objects", "gap",
     lambda r: len(r.findall(".//zone[@type-v2='layoutbasic']"))),
    ("Dashboard", "Device-specific layouts", "partial",
     lambda r: len(r.findall(".//devicelayouts/devicelayout"))),
    ("Dashboard", "Story points", "partial",
     lambda r: len(r.findall(".//story-point"))),

    # ---- formatting & maps ----
    ("Formatting", "Number formats", "full",
     lambda r: len([c for c in r.findall(".//column") if c.get("default-format")])),
    ("Formatting", "Titles / captions", "full",
     lambda r: len(r.findall(".//layout-options/title"))),
    ("Formatting", "Colors (all 6 constructs)", "full",
     lambda r: len(r.findall(".//style-rule[@element='mark']/encoding[@attr='color']"))),
    ("Formatting", "Custom rich tooltips", "gap",
     lambda r: len(r.findall(".//customized-tooltip"))),
    ("Maps", "Filled / symbol maps (US + world)", "full",
     lambda r: sum(1 for w in r.findall(".//worksheets/worksheet")
                   if w.find(".//pane/mark[@class='Multipolygon']") is not None
                   or "Latitude" in (TP._shelf_text(w, "rows") if hasattr(TP, "_shelf_text") else ""))),
    ("Maps", "Custom geocoding / map layers", "gap",
     lambda r: len(r.findall(".//map-layer"))),
]

STATUS_TEXT = {"full": "converts", "partial": "approximate",
               "gap": "not yet", "scope": "out of scope"}


def _formulas(r):
    return [c.get("formula") or "" for c in r.findall(".//column/calculation[@class='tableau']")]


def _group_count(r):
    """REAL user groups = categorical-bin columns. (Datasource <group> elements
    are mostly hidden dashboard-action link plumbing -- counting them inflated
    this to 73x; the honest count is the bin columns.)"""
    seen = set()
    for col in r.findall(".//column"):
        calc = col.find("calculation")
        if calc is not None and calc.get("class") == "categorical-bin":
            seen.add(col.get("name"))
    return len(seen)


def _set_count(r):
    """User sets: visible datasource <group> elements that are NOT action
    plumbing (hidden sheet_link) and NOT Measure Names."""
    n = 0
    for g in r.findall(".//datasource/group"):
        nm = g.get("name") or ""
        if "Measure Names" in nm or "Action (" in nm:
            continue
        if g.get("hidden") == "true" and "sheet_link" in "".join(g.attrib.values()):
            continue
        n += 1
    return n


def _dualaxis_count(r):
    import re
    n = 0
    mre = re.compile(r"\[(?:sum|avg|usr|cnt|ctd|min|max|med):")
    for w in r.findall(".//worksheets/worksheet"):
        for which in ("rows", "cols"):
            el = w.find(".//table/" + which)
            if el is not None and len(mre.findall(el.text or "")) > 1:
                n += 1
    return n


def coverage(paths):
    agg = {(cat, feat): 0 for cat, feat, _, _ in FEATURES}
    status = {(cat, feat): st for cat, feat, st, _ in FEATURES}
    for path in paths:
        try:
            root = TP.load_twb_xml(path)
        except Exception:
            continue
        for cat, feat, st, det in FEATURES:
            try:
                agg[(cat, feat)] += det(root)
            except Exception:
                pass
    rows = []
    for cat, feat, st, _ in FEATURES:
        rows.append({"category": cat, "feature": feat, "status": st,
                     "count": agg[(cat, feat)], "present": agg[(cat, feat)] > 0})
    return rows


def main():
    args = sys.argv[1:]
    paths = args or sorted(glob.glob("*.twb") + glob.glob("*.twbx"))
    paths = [p for p in paths if not p.startswith("~")]
    rows = coverage(paths)
    print(f"\nTABLEAU FEATURE COVERAGE  ({len(paths)} workbook(s))")
    print("=" * 74)
    cur = None
    for r in rows:
        if r["category"] != cur:
            cur = r["category"]
            print(f"\n{cur.upper()}")
        seen = f"{r['count']}x" if r["present"] else "-"
        print(f"  {r['feature']:<44}{STATUS_TEXT[r['status']]:<14}{seen:>6}")
    # summary + gaps present in YOUR corpus
    from collections import Counter
    by = Counter(r["status"] for r in rows)
    print("\n" + "=" * 74)
    print(f"SUMMARY: {by['full']} convert, {by['partial']} approximate, "
          f"{by['gap']} not-yet, {by['scope']} out-of-scope")
    gaps = [r for r in rows if r["status"] == "gap" and r["present"]]
    if gaps:
        print("\nGAPS PRESENT IN YOUR WORKBOOKS (prioritize):")
        for r in sorted(gaps, key=lambda x: -x["count"]):
            print(f"   {r['count']:>4}x  {r['feature']}")
    print()


if __name__ == "__main__":
    main()

"""
audit_coverage.py  --  fidelity harness for the Tableau->Streamlit accelerator.

Workbook-agnostic. For EVERY worksheet of ANY .twb/.twbx it reports:
  * the chart kind tableau_parser.infer() resolved it to,
  * which marks-card encodings are present vs actually consumed by the parser,
  * every rendering-relevant construct present in the XML that the parser does
    NOT extract yet, classified by severity:
        [CORRECTNESS] changes the numbers/rows shown (must fix)
        [APPEARANCE]  changes how it looks (should fix)
        [COSMETIC]    minor / informational
It does not change anything -- it makes the gap measurable so nothing is dropped
silently. Use it before trusting a generated app, and to drive what to extract next.

Usage:
  python audit_coverage.py Superstore.twb
  python audit_coverage.py Superstore.twbx --sheet CustomerRank
"""
import argparse
import re
import xml.etree.ElementTree as ET

import tableau_parser as TP

MEAS_RE = re.compile(r"\[(?:sum|avg|usr|cnt|ctd|min|max|med|stdev|var):")

# severity, label, gap(ws, spec) -> True only when XML HAS it but the IR MISSES it
CHECKS = [
    ("CORRECTNESS", "applied filter values (range / subset / Top-N)",
     lambda w, sp: _has_value_filter(w) and not sp.get("applied_filters")),
    ("CORRECTNESS", "table-calc (RANK / INDEX / WINDOW / % of total)",
     lambda w, sp: any(t.get("type") or t.get("rank-options")
                       for t in w.findall(".//table-calc")) and not sp.get("table_calcs")),
    ("APPEARANCE", "manual (drag) sort order",
     lambda w, sp: _has_manual_sort(w) and not sp.get("manual_sort")),
    ("APPEARANCE", "reference / distribution lines",
     lambda w, sp: bool(w.findall(".//reference-line")) and not sp.get("reflines")),
    ("APPEARANCE", "trend line",
     lambda w, sp: bool(w.findall(".//style-rule[@element='trendline']")) and not sp.get("trendline")),
    ("APPEARANCE", "size encoding",
     lambda w, sp: bool(w.findall(".//pane/encodings/size")) and not sp.get("size")),
    ("APPEARANCE", "shape encoding",
     lambda w, sp: bool(w.findall(".//pane/encodings/shape")) and not sp.get("shape")),
    ("APPEARANCE", "multiple measures on one shelf (dual / compound axis)",
     lambda w, sp: _dual_axis(w) and sp.get("kind") not in ("heatmap", "kpi", "mbar", "table")),
    ("COSMETIC", "custom tooltip",
     lambda w, sp: bool(w.findall(".//customized-tooltip")) and not sp.get("tooltip_fields")),
    # ---- fidelity checks added after user review caught silent visual gaps ----
    ("APPEARANCE", "explicit mark class not honored (Gantt/Pie/Square...)",
     lambda w, sp: _mark_mismatch(w, sp)),
    ("APPEARANCE", "text (label) encodings present but chart kind renders no labels",
     lambda w, sp: _labels_dropped(w, sp)),
    ("APPEARANCE", "color encoding present but spec carries no color",
     lambda w, sp: _color_dropped(w, sp)),
    ("APPEARANCE", "workbook defines exact value colors for a field this sheet colors by",
     lambda w, sp: _value_colors_unused(w, sp)),
    ("APPEARANCE", "fixed mark color declared but not captured",
     lambda w, sp: _mark_color_dropped(w, sp)),
    # ---- 2024.3 sample-pack features: captured for honest reporting ----
    ("APPEARANCE", "forecast overlay (ETS prediction bands) -- actuals only",
     lambda w, sp: (w.find(".//forecast-specification") is not None
                    and w.find(".//forecast-specification").get("enabled") != "false")
                   and not sp.get("forecast")),
    ("APPEARANCE", "subtotals / grand totals not rendered",
     lambda w, sp: w.find(".//subtotals") is not None and not sp.get("subtotals")),
    ("COSMETIC", "viz-in-tooltip (embedded sheet on hover)",
     lambda w, sp: bool(re.search(r'<Sheet name="', "".join(w.itertext())))
                   and not sp.get("viz_tooltip")),
]


def _mark_color_dropped(w, sp):
    declared = any(f.get("attr") == "mark-color"
                   and (f.get("value") or "").startswith("#")
                   for f in w.findall(".//pane//format"))
    return declared and not sp.get("mark_color")

# mark class -> the spec kind(s) that faithfully represent it
_MARK_KINDS = {"Gantt": {"gantt"}, "GanttBar": {"gantt"},
               "Pie": {"pie"}, "Square": {"heatmap", "table"},
               "Area": {"area"}, "Line": {"line"}}
# kinds that DO render text/labels on marks
_LABELED_KINDS = {"kpi", "table", "mbar", "pctbar", "heatmap"}


def _mark_mismatch(w, sp):
    m = w.find(".//pane/mark")
    cls = m.get("class") if m is not None else None
    want = _MARK_KINDS.get(cls)
    return bool(want) and sp.get("kind") not in want


def _labels_dropped(w, sp):
    has_text = bool(w.findall(".//pane/encodings/text"))
    return has_text and sp.get("kind") not in _LABELED_KINDS and not sp.get("text")


def _non_mn_color_encs(w):
    """Color encodings other than Measure Names (whose colors ride on the
    per-measure entries, not on a color field)."""
    return [e for e in w.findall(".//pane/encodings/color")
            if ":Measure Names" not in (e.get("column") or "")]


def _color_dropped(w, sp):
    has_color = bool(_non_mn_color_encs(w))
    carries = any(sp.get(k) for k in ("color", "color_measure", "segment"))
    return has_color and not carries and sp.get("kind") not in ("kpi", "table")


def _value_colors_unused(w, sp):
    """The workbook assigns exact per-value colors somewhere for the field this
    sheet colors by -- the IR must carry a color source for the engine to use."""
    if not _non_mn_color_encs(w):
        return False
    carries = any(sp.get(k) for k in ("color", "color_measure", "segment"))
    return not carries and sp.get("kind") not in ("kpi", "table")


def _has_manual_sort(w):
    return any((ms.get("column") or "").find(":Measure Names") < 0
               for ms in w.findall(".//manual-sort"))


CONSUMED_ENC = {"color", "lod", "detail", "text"}


def _shelf(w, which):
    el = w.find(".//table/" + which)
    return (el.text if el is not None else "") or ""


def _dual_axis(w):
    return MEAS_RE.findall(_shelf(w, "rows")).__len__() > 1 \
        or MEAS_RE.findall(_shelf(w, "cols")).__len__() > 1


def _attr_suffix(el, suffix):
    """Fetch an attribute by local name, ignoring xmlns prefix (user:ui-enumeration)."""
    for k, v in el.attrib.items():
        if k.split("}")[-1].split(":")[-1] == suffix:
            return v
    return None


def _has_value_filter(w):
    """True only for a GENUINELY applied data filter -- a quantitative range, a
    Top-N, or a categorical subset. Measure Names shelves, action filters, and
    'all members enumerated' (no effective filtering) do NOT count."""
    for f in w.findall(".//filter"):
        col = f.get("column") or ""
        if ":Measure Names" in col or "Action (" in col:
            continue
        if f.get("class") == "quantitative" and (f.find("min") is not None or f.find("max") is not None):
            return True
        for gf in f.findall(".//groupfilter"):
            fn = gf.get("function")
            if fn in ("filter", "range", "end", "top", "top-by-field"):
                return True
            if fn == "level-members":
                enum = _attr_suffix(gf, "ui-enumeration")
                if enum and enum != "all":     # a real subset was selected
                    return True
    return False


def audit(path, only=None):
    root = TP.load_twb_xml(path)
    meta, _ = TP.column_meta(root)
    sheets = root.findall(".//worksheets/worksheet")
    totals = {}
    print(f"\nCoverage audit: {path}  ({len(sheets)} worksheets)\n" + "=" * 72)
    for w in sheets:
        name = w.get("name")
        if only and name != only:
            continue
        try:
            spec0 = TP.infer(w, meta); kind = spec0.get("kind", "?")
        except Exception as e:
            kind = f"<infer error: {e}>"
        present = sorted({e.tag for e in w.findall(".//pane/encodings/*")})
        ignored_enc = [e for e in present if e not in CONSUMED_ENC and e != "geometry"]
        try:
            spec = TP.infer(w, meta)
        except Exception:
            spec = {}
        gaps = []
        for sev, label, gap in CHECKS:
            try:
                if gap(w, spec):
                    gaps.append((sev, label))
                    totals[label] = totals.get(label, 0) + 1
            except Exception:
                pass
        verdict = "OK" if not gaps else (
            "LOSSY" if any(s == "CORRECTNESS" for s, _ in gaps) else "PARTIAL")
        print(f"\n{name:<26} kind={kind:<8} [{verdict}]")
        if ignored_enc:
            print(f"   encodings present but ignored: {', '.join(ignored_enc)}")
        for sev, label in gaps:
            print(f"   [{sev}] {label}")
    print("\n" + "=" * 72)
    print("WORKBOOK TOTALS (worksheets affected):")
    for label, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>2}x  {label}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("twb")
    ap.add_argument("--sheet", default=None)
    a = ap.parse_args()
    audit(a.twb, a.sheet)


if __name__ == "__main__":
    main()

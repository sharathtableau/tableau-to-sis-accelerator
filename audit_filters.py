"""
audit_filters.py  --  filter coverage audit across one or more workbooks.

Enumerates every Tableau filter TYPE present in the corpus and reports whether
the accelerator handles it, partially handles it, or doesn't yet -- the same
"measure the whole surface" discipline used for colors and calcs.

Usage:
  python audit_filters.py                 # all workbooks in the folder
  python audit_filters.py Book.twbx
"""

import glob
import sys
import zipfile
import io
import xml.etree.ElementTree as ET

import tableau_parser as TP

# Tableau filter type -> (detector, coverage status). Detector(root,raw) -> count.
# status: "full", "partial", "gap", "scope" (out of scope by design).
FILTER_TYPES = [
    ("Dimension — member select (IN)", "full",
     lambda root, raw: _gf_count(root, "member")),
    ("Dimension — exclude (NOT IN)", "full",
     lambda root, raw: _gf_count(root, "except")),
    ("Measure — quantitative range", "full",
     lambda root, raw: len(root.findall(".//worksheets//filter[@class='quantitative']"))),
    ("Date — range / relative", "full",
     lambda root, raw: sum(1 for f in root.findall(".//worksheets//filter")
                           if f.get("class") == "quantitative"
                           and "Date" in (f.get("column") or ""))),
    ("Date — discrete part (EXTRACT)", "full",
     lambda root, raw: _gf_count(root, "range")),
    ("All-members / level-members (no-op)", "full",
     lambda root, raw: _gf_count(root, "level-members")),
    ("Union of member sets (OR)", "partial",
     lambda root, raw: _gf_count(root, "union")),
    ("Top / Bottom N", "full",
     lambda root, raw: _topn_count(root)),
    ("Context filter (order of operations)", "full",
     lambda root, raw: raw.count("context='true'") + raw.count('context="true"')),
    ("Data source filter", "gap",
     lambda root, raw: len(root.findall(".//datasources/datasource/filter"))),
    ("Wildcard / conditional (by formula)", "gap",
     lambda root, raw: _gf_count(root, "filter")),
    ("Action filter (cross-viz)", "scope",
     lambda root, raw: sum(1 for f in root.findall(".//worksheets//filter")
                           if "Action (" in (f.get("column") or ""))),
]

STATUS_LABEL = {"full": "handled", "partial": "partial",
                "gap": "not yet", "scope": "out of scope"}


def _gf_count(root, fn):
    return sum(1 for gf in root.findall(".//worksheets//filter//groupfilter")
               if gf.get("function") == fn)


def _topn_count(root):
    n = 0
    for gf in root.findall(".//worksheets//filter//groupfilter"):
        if gf.get("function") in ("top", "top-by-field") or \
           (gf.get("function") == "end" and gf.get("count")):
            n += 1
    return n


def _raw_xml(path):
    if path.lower().endswith(".twbx"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".twb"))
            return z.read(name).decode("utf-8", "ignore")
    return open(path, encoding="utf-8", errors="ignore").read()


def coverage(paths):
    """Return [{type, status, present, count}] aggregated over the workbooks."""
    agg = {label: 0 for label, _, _ in FILTER_TYPES}
    for path in paths:
        try:
            root = TP.load_twb_xml(path)
            raw = _raw_xml(path)
        except Exception:
            continue
        for label, status, det in FILTER_TYPES:
            try:
                agg[label] += det(root, raw)
            except Exception:
                pass
    rows = []
    for label, status, _ in FILTER_TYPES:
        rows.append({"type": label, "status": status,
                     "present": agg[label] > 0, "count": agg[label]})
    return rows


def main():
    args = sys.argv[1:]
    paths = args or sorted(glob.glob("*.twb") + glob.glob("*.twbx"))
    paths = [p for p in paths if not p.startswith("~")]
    rows = coverage(paths)
    print(f"\nFILTER COVERAGE  ({len(paths)} workbook(s))")
    print("=" * 64)
    print(f"{'filter type':<42}{'status':<13}{'seen':>6}")
    print("-" * 64)
    for r in rows:
        seen = f"{r['count']}x" if r["present"] else "-"
        print(f"{r['type']:<42}{STATUS_LABEL[r['status']]:<13}{seen:>6}")
    gaps = [r for r in rows if r["status"] == "gap" and r["present"]]
    if gaps:
        print("\nGAPS present in this corpus (prioritize):")
        for r in gaps:
            print(f"   {r['count']:>3}x  {r['type']}")
    print()


if __name__ == "__main__":
    main()

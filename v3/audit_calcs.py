"""
audit_calcs.py  --  calculation coverage audit across one or more workbooks.

Enumerates EVERY Tableau function used in every calculated field, classifies it
by category, and reports which translate to SQL today vs. which don't. This is
the "enumerate the whole surface, then close it" discipline (same as the color
sweep) applied to calculations -- it turns "capture all calcs" into a measured
backlog.

Usage:
  python audit_calcs.py                      # all *.twb / *.twbx in the folder
  python audit_calcs.py Book.twbx            # one workbook
"""

import glob
import re
import sys
from collections import Counter, defaultdict

import tableau_parser as TP
from calc_translator import translate_formula

# Tableau function -> category. Not exhaustive of every alias, but covers the
# functions that actually appear in real workbooks.
FUNCTION_CATEGORY = {}


def _reg(cat, *names):
    for n in names:
        FUNCTION_CATEGORY[n.upper()] = cat


_reg("aggregate", "SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN",
     "ATTR", "STDEV", "STDEVP", "VAR", "VARP", "PERCENTILE", "CORR", "COVAR")
_reg("logical", "IF", "IIF", "CASE", "WHEN", "ELSEIF", "ELSE", "AND", "OR",
     "NOT", "ISNULL", "IFNULL", "ZN", "ISDATE", "IN")
_reg("string", "LEFT", "RIGHT", "MID", "LEN", "TRIM", "LTRIM", "RTRIM",
     "UPPER", "LOWER", "CONTAINS", "STARTSWITH", "ENDSWITH", "FIND", "REPLACE",
     "SPLIT", "SUBSTITUTE", "REGEXP_MATCH", "REGEXP_EXTRACT", "REGEXP_REPLACE",
     "SPACE", "PROPER", "ASCII", "CHAR")
_reg("date", "DATEDIFF", "DATEADD", "DATEPART", "DATENAME", "DATETRUNC",
     "YEAR", "MONTH", "DAY", "QUARTER", "WEEK", "HOUR", "MINUTE", "SECOND",
     "TODAY", "NOW", "MAKEDATE", "MAKETIME", "MAKEDATETIME", "ISDATE",
     "DATELINE", "WEEKDAY")
_reg("math", "ABS", "ROUND", "CEILING", "FLOOR", "POWER", "SQRT", "SQUARE",
     "EXP", "LOG", "LN", "SIGN", "PI", "SIN", "COS", "TAN", "ATAN", "ATAN2",
     "DIV", "MOD", "MIN", "MAX")
_reg("type", "INT", "FLOAT", "STR", "DATE", "DATETIME", "BOOL")
_reg("lod", "FIXED", "INCLUDE", "EXCLUDE")
_reg("table_calc", "INDEX", "RANK", "RANK_DENSE", "RANK_UNIQUE", "RANK_MODIFIED",
     "RANK_PERCENTILE", "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MAX", "WINDOW_MIN",
     "WINDOW_COUNT", "WINDOW_MEDIAN", "WINDOW_STDEV", "WINDOW_VAR",
     "WINDOW_PERCENTILE", "WINDOW_CORR", "RUNNING_SUM", "RUNNING_AVG",
     "RUNNING_MAX", "RUNNING_MIN", "RUNNING_COUNT", "LOOKUP", "PREVIOUS_VALUE",
     "FIRST", "LAST", "SIZE", "TOTAL")

_FUNC_RE = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s*\(", re.I)
_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.I)


def _functions_in(formula):
    """Set of function names used in a formula (incl. LOD keywords)."""
    fns = {m.group(1).upper() for m in _FUNC_RE.finditer(formula or "")}
    fns |= {m.group(1).upper() for m in _LOD_RE.finditer(formula or "")}
    # IF/CASE aren't followed by '(' -- catch the keywords too
    for kw in ("IF", "CASE", "ELSEIF", "AND", "OR", "NOT", "IN"):
        if re.search(r"\b" + kw + r"\b", formula or "", re.I):
            fns.add(kw)
    return fns


def coverage(paths):
    """Structured calc coverage: {overall_pct, n_calc, n_ok, categories:
    {cat: pct}, unhandled: Counter}. Reused by the weekly status generator."""
    unhandled_func = Counter()
    cat_total = Counter()
    cat_ok = Counter()
    n_calc = n_ok = 0
    for path in paths:
        try:
            root = TP.load_twb_xml(path)
        except Exception:
            continue
        meta, _ = TP.column_meta(root)
        cd, params, aliases, pinfo = TP.extract_calcs_params(root, meta)
        merged = dict(aliases); merged.update(cd)
        for cap, formula in cd.items():
            n_calc += 1
            fns = _functions_in(formula)
            sql, _ = translate_formula(formula, params, merged,
                                       param_alias=pinfo["alias"])
            ok = sql is not None
            n_ok += ok
            for fn in fns:
                cat = FUNCTION_CATEGORY.get(fn)
                if cat is None:
                    continue
                cat_total[cat] += 1
                if ok:
                    cat_ok[cat] += 1
                elif not ok:
                    unhandled_func[fn] += 1
    cats = {c: round(100 * cat_ok[c] / cat_total[c]) for c in cat_total}
    return {"overall_pct": round(100 * n_ok / max(n_calc, 1)),
            "n_calc": n_calc, "n_ok": n_ok, "categories": cats,
            "cat_total": dict(cat_total), "unhandled": unhandled_func}


def audit(paths):
    cov = coverage(paths)
    cat_ok = {c: round(cov["categories"][c] / 100 * cov["cat_total"][c])
              for c in cov["cat_total"]}
    cat_total = cov["cat_total"]
    unhandled_func = cov["unhandled"]
    n_calc, n_ok = cov["n_calc"], cov["n_ok"]
    unknown_funcs = Counter()

    print(f"\nCALCULATION COVERAGE  ({len(paths)} workbook(s), {n_calc} calcs)")
    print("=" * 60)
    print(f"Calcs translating to SQL: {n_ok}/{n_calc}  ({n_ok/max(n_calc,1):.0%})\n")
    print(f"{'category':<14}{'used':>6}{'in-ok-calc':>12}{'coverage':>10}")
    print("-" * 44)
    for cat in ("aggregate", "logical", "math", "string", "date", "type",
                "lod", "table_calc"):
        u, o = cat_total.get(cat, 0), cat_ok.get(cat, 0)
        if u:
            print(f"{cat:<14}{u:>6}{o:>12}{o/u:>9.0%}")
    if unhandled_func:
        print("\nFunctions appearing in calcs that DON'T translate:")
        for fn, n in unhandled_func.most_common(20):
            print(f"   {n:>3}x  {fn}  ({FUNCTION_CATEGORY.get(fn,'?')})")
    if unknown_funcs:
        print("\nFunctions NOT in our taxonomy (verify handling):")
        for fn, n in unknown_funcs.most_common(15):
            print(f"   {n:>3}x  {fn}")
    print()


def main():
    args = sys.argv[1:]
    paths = args or sorted(glob.glob("*.twb") + glob.glob("*.twbx"))
    paths = [p for p in paths if not p.startswith("~")]
    audit(paths)


if __name__ == "__main__":
    main()

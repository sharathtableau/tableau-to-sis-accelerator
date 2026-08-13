"""
parity.py  --  STAGE 5 (Validation) logic: prove the converted app's numbers.

The proof is INTEGRATION parity, computed two independent ways so a real defect
shows up as a mismatch:

  * app value   -- SUM(measure) through the engine's OWN data path
                   (backend.run_sql on the datasource's CONFIGURED table). If
                   the workbook is mis-routed to the wrong table (the classic
                   "Superstore gravity" bug), this queries the wrong data and
                   the number diverges.
  * source value-- SUM(measure) from an INDEPENDENT read of the source file
                   (pandas). This is the ground truth the app must reproduce.
  * Tableau     -- when a workbook has published figures we know (e.g.
                   Superstore grand totals), a third column cross-checks the
                   real Tableau number, not just internal consistency.

Also reports row-count parity per datasource (complete load) and the calc
translation coverage (how many Tableau calcs converted vs dropped -> the ones
routed to Cortex / human review).

The same checks are emitted as a Jupyter validation notebook (build_notebook)
following the dashboard-validation methodology: source query, comparison
table, per-metric verdict, roll-up summary.
"""

import datetime as _dt
import json
import os
import re
from collections import Counter

import cortex_semantic as CS
from backend import _normalize_columns, _read_source_file, run_sql
from calc_translator import to_phys

# Published Tableau ground truth for demo workbooks (grand totals), keyed by
# datasource caption -> {physical col: expected value}. Extend as workbooks are
# verified against Tableau; absence just means the Tableau column is blank.
TABLEAU_TRUTH = {
    "Sample - Superstore": {"SALES": 2326534, "PROFIT": 292297, "QUANTITY": 38654},
}
# Same idea for CALCULATED metrics (keyed by the metric's phys name from
# cortex_semantic.build_metrics), where a raw-column cross-check isn't
# possible -- (low, high) inclusive bound rather than exact value, since these
# are already-rounded/derived figures verified by eyeball against Tableau.
TABLEAU_TRUTH_METRIC = {
    "C_DAYS_TO_SECOND_PURCHASE": (66, 68),   # regression-locked: Tableau shows 67
}
TOL = 0.01          # relative tolerance for a PASS (rounding across engines)


def _phys(ir, cap):
    return to_phys(ir.get("colmap", {}).get(cap, cap))


def _rel_ok(a, b):
    if a is None or b is None:
        return False
    if abs(b) < 1e-9:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= TOL


def check_calc_metrics(ir):
    """Validate the AGGREGATING CALCULATED FIELDS (Tableau LODs/ratios/etc.,
    not raw columns) -- the measures a workbook like E-Commerce is built almost
    entirely from (88 calcs, 0 raw facts there), which the two-path column
    check above cannot see at all.

    A calc has no independent second computation path in general (that would
    mean re-implementing arbitrary Tableau formula semantics in pandas), so
    the proof here is EXECUTION against the real table (same class of gate the
    Cortex calc-fallback uses) plus a cross-check against a known Tableau
    figure where TABLEAU_TRUTH_METRIC has one. A metric that fails to execute,
    or executes but lands outside its known Tableau bound, is a BUG."""
    import config
    all_metrics, _ = CS.build_metrics(ir)
    ds_fields = CS.collect(ir)
    rows, placed = [], set()
    for cap, fields in ds_fields.items():
        entry = config.DATASOURCES.get(cap)
        table = entry.get("table") if entry else None
        if not table:
            continue
        table_cols = CS._table_columns(entry) or \
            {_phys(ir, c) for grp in fields.values() for c in grp}
        for m in all_metrics:
            if id(m) in placed or not m["cols"] <= table_cols:
                continue
            placed.add(id(m))
            app_v = err = None
            try:
                app_v = run_sql(f"SELECT {m['sql']} AS V FROM {table}")["V"][0]
                try:
                    app_v = float(app_v)
                except (TypeError, ValueError):
                    pass                      # non-numeric metric (CASE label etc.)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            bound = TABLEAU_TRUTH_METRIC.get(m["name"])
            if err is not None:
                verdict = "BUG"
            elif bound is not None:
                lo, hi = bound
                verdict = "PASS" if (isinstance(app_v, float) and lo <= app_v <= hi) else "BUG"
            else:
                verdict = "EXECUTED"          # runs clean; no independent truth to compare
            rows.append({"datasource": cap, "metric": m["synonyms"][0],
                         "name": m["name"], "sql": m["sql"], "value": app_v,
                         "tableau_bound": bound, "error": err, "verdict": verdict})
    return rows


def check_workbook(ir):
    """Return the validation result: per-datasource row counts, per-measure
    parity rows, and calc-coverage. Pure -- no UI."""
    import config
    ds_fields = CS.collect(ir)
    truth = TABLEAU_TRUTH
    rows, ds_rows = [], []
    for cap, fields in ds_fields.items():
        entry = config.DATASOURCES.get(cap)
        table = entry.get("table") if entry else None
        path = entry.get("local_file") if entry else None
        # row-count parity (complete load / correct routing)
        src_n = app_n = None
        src_df = None
        if path and os.path.exists(path):
            try:
                src_df = _normalize_columns(_read_source_file(path))
                src_n = len(src_df)
            except Exception:
                src_df = None
        if table:
            try:
                app_n = int(run_sql(f"SELECT COUNT(*) AS N FROM {table}")["N"][0])
            except Exception:
                app_n = None
        ds_rows.append({"datasource": cap, "table": table,
                        "app_rows": app_n, "source_rows": src_n,
                        "match": app_n == src_n if (app_n is not None
                                                    and src_n is not None) else None})
        # per-measure grand-total parity
        for m_cap in sorted(fields["facts"]):
            phys = _phys(ir, m_cap)
            app_v = src_v = tab_v = None
            source_kind = "file"
            has_col = src_df is not None and phys in src_df.columns
            if has_col:
                try:
                    src_v = float(src_df[phys].astype(float).sum())
                except Exception:
                    src_v = None
            if table and (has_col or src_df is None):
                try:
                    app_v = float(run_sql(
                        f"SELECT SUM({phys}) AS V FROM {table}")["V"][0])
                except Exception:
                    app_v = None
            if src_df is None and table and app_v is not None:
                # No local source FILE in this environment -- e.g. a hyper-only
                # datasource whose .hyper never decoded here, reused instead
                # from a table pre-loaded elsewhere (exactly the Regional/
                # Global-in-Snowsight case). Falling straight to app_v-vs-None
                # made _rel_ok() return False for EVERY such measure -- a false
                # "BUG" on a workbook that actually converted correctly, not a
                # real defect. Fall back to an INDEPENDENT client-side re-pull
                # + sum of the SAME table (server aggregate vs. client
                # aggregate of a raw pull) -- a genuinely different code path
                # that still catches real defects (wrong column, wrong
                # aggregation, type/NULL handling), though it cannot catch a
                # bad LOAD into that table the way a true external-file check
                # can. Tagged with source_kind so the report never overstates
                # this as equivalent to a check against the original extract.
                try:
                    raw = run_sql(f"SELECT {phys} AS V FROM {table}")
                    src_v = float(raw["V"].astype(float).sum())
                    source_kind = "table-repull"
                except Exception:
                    source_kind = "unavailable"
            # a measure with no physical column on either side (count-of-records
            # / non-column measure) is not a SUM-parity target -- the row-count
            # check covers COUNT(*); skip rather than cry BUG.
            if app_v is None and src_v is None:
                continue
            tab_v = truth.get(cap, {}).get(phys)
            if src_v is not None:
                verdict = "PASS" if _rel_ok(app_v, src_v) else "BUG"
            else:
                verdict = "EXECUTED"      # ran clean; no independent check available
            if tab_v is not None and not _rel_ok(app_v, tab_v):
                verdict = "BUG"             # disagrees with real Tableau figure
            rows.append({"datasource": cap, "measure": m_cap, "column": phys,
                         "app": app_v, "source": src_v, "source_kind": source_kind,
                         "tableau": tab_v, "verdict": verdict})
    # calculated-metric validation (execution-gated; the E-Commerce class of
    # workbook has ~0 raw-column measures and ~90 calc measures, so skipping
    # this would leave the frontier workbook with nothing validated at all)
    calc_rows = check_calc_metrics(ir)

    # calc translation coverage
    n_calc = len({id(v) for v in ir.get("calcs", {}).values()})
    drops = list(ir.get("calc_drops", {}) or {})
    n_bug = (sum(1 for r in rows if r["verdict"] == "BUG")
             + sum(1 for r in calc_rows if r["verdict"] == "BUG"))
    summary = {"datasources": len(ds_fields),
               "measures_checked": len(rows) + len(calc_rows),
               "measures_pass": sum(1 for r in rows if r["verdict"] in ("PASS", "EXECUTED"))
               + sum(1 for r in calc_rows if r["verdict"] in ("PASS", "EXECUTED")),
               "measures_bug": n_bug,
               "calcs_translated": n_calc, "calcs_dropped": len(drops),
               "dropped_names": drops}
    return {"datasources": ds_rows, "measures": rows, "calc_metrics": calc_rows,
            "summary": summary}


def _raw_formulas(source_file):
    """caption -> raw Tableau calc formula, parsed straight from the .twb (the
    'Tableau TWB formula' side of the comparison; the IR only keeps the
    TRANSLATED sql, not the original formula text)."""
    import tableau_parser as TP
    try:
        root = TP.load_twb_xml(source_file)
    except Exception:
        return {}
    out = {}
    for col in root.findall(".//column"):
        c = col.find("calculation")
        if c is not None and c.get("class") == "tableau" and c.get("formula"):
            cap = col.get("caption") or (col.get("name") or "").strip("[]")
            out.setdefault(cap, c.get("formula"))
    return out


def _parse_view_csv(csv_text):
    """Parse Tableau's rendered-view CSV export -- a plain comma CSV, stdlib
    only. Returns (headers, rows-of-dicts)."""
    import csv as _csv
    import io as _io
    reader = _csv.DictReader(_io.StringIO(csv_text))
    rows = list(reader)
    return reader.fieldnames or [], rows


def _normalize_truth_header(h):
    """Strip Tableau's aggregation wrapper (SUM(...), AGG(...), CNT(...)) so
    a CSV column header can be compared against a calc_metrics caption --
    Tableau's Query View Data export labels columns like 'SUM(Sales)' or
    'AGG(Profit Ratio)', not the bare field name."""
    m = re.match(r"^[A-Za-z][A-Za-z ]*\((.+)\)$", h.strip())
    inner = m.group(1) if m else h
    return re.sub(r"\s+", " ", inner).strip().lower()


_PURE_SUM_RE = re.compile(r"^\s*SUM\(\s*[A-Za-z0-9_.\"]+\s*\)\s*$", re.IGNORECASE)


def _is_pure_sum_sql(sql):
    """True only for a bare SUM(col) expression. Summing a matched CSV
    column across every row of a multi-row view is mathematically valid
    ONLY for a plain sum -- WRONG for a ratio/LOD/compound formula (summing
    per-row profit ratios across rows is not the grand-total profit ratio).
    This is the correctness guard that makes the approximate-sum fallback
    below safe to apply automatically rather than a guess dressed up as
    math."""
    return bool(_PURE_SUM_RE.match(sql or ""))


def _parse_numeric(raw_v):
    try:
        return float(str(raw_v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _rows_partition_data(headers, rows):
    """Do this view's rows PARTITION the underlying data -- i.e. is each row a
    DISTINCT slice, so that summing a measure column across them yields a real
    grand total rather than double-counting?

    Returns (ok, reason_when_not_ok).

    WHY THIS EXISTS -- a real, measured failure, 2026-07-30. The first live run
    of the approximate-sum tier produced `Quantity = 231,924` against an app
    value of `38,654`: EXACTLY 6x. The view's 24 rows did not partition the
    data (the same underlying quantity appeared six times over), so summing its
    column multiplied the total instead of reconstructing it. The verdict guard
    held -- an approximate value never decides a verdict, so nothing was
    falsely failed -- but presenting an authoritative-looking 231,924 next to
    the true 38,654 is worse than presenting nothing, which is what this check
    fixes.

    TWO SIGNALS, both read from the CSV alone -- deliberately NOT a comparison
    against the app's own value, which would be circular (an independent
    reference validated by the thing it is meant to check is not independent):

      1. A Measure Names / Measure Values column -- Tableau's own marker for a
         measure crosstab, where every dimension row REPEATS once per measure.
      2. Duplicate dimension tuples -- if the non-numeric (dimension) columns
         do not uniquely identify each row, the rows repeat by construction. A
         view with NO dimension column at all but multiple rows cannot be a
         clean breakdown either.
    """
    norm = [_normalize_truth_header(h) for h in headers]
    for h, n in zip(headers, norm):
        if n in ("measure names", "measure values"):
            return False, (f"a '{h}' column means this is a measure crosstab "
                           "-- every row repeats once per measure, so summing "
                           "a column double-counts")
    dim_cols = []
    for h in headers:
        vals = [r.get(h, "") for r in rows]
        if not all(_parse_numeric(v) is not None for v in vals if str(v).strip()):
            dim_cols.append(h)
    if not dim_cols:
        return False, ("no dimension column -- multiple all-numeric rows "
                       "cannot be a clean per-row breakdown")
    keys = [tuple(r.get(c, "") for c in dim_cols) for r in rows]
    if len(set(keys)) < len(keys):
        dupes = len(keys) - len(set(keys))
        return False, (f"{dupes} repeated dimension row(s) -- the rows do not "
                       "partition the data, so summing a column double-counts")
    return True, None


def truth_from_view_csv(csv_text, calc_metrics):
    """R2 -- map ONE view's rendered CSV to a {internal_name: {"value":
    float, "approx": bool, "rows": int}} dict, matching CSV column headers
    to calc_metrics captions.

    EXACT match (approx=False): the CSV has exactly one data row -- a true
    single-KPI/grand-total view, already aggregated by Tableau itself the
    same way it renders on screen. Values come back exactly as Tableau
    rendered them; nothing here re-aggregates.

    APPROXIMATE match (approx=True) -- 2026-07-29, added after the
    exact-only rule yielded ZERO ground truth for a real multi-dimensional
    workbook (every view had multiple rows: order-level, product-level,
    customer-level breakdowns, no single-KPI view anywhere), user-directed
    trade-off: when the CSV has MULTIPLE rows, sum the matched column
    across every row as a best-effort grand total. Restricted to metrics
    whose SQL is a bare SUM(col) (_is_pure_sum_sql) -- summing rows is only
    valid arithmetic for a plain sum, never for a ratio/LOD. Still a real,
    disclosed risk even for a pure sum: if the view itself has a hidden
    filter/exclusion, the summed total won't match Tableau's true grand
    total -- this is why every downstream consumer (the UI column, the
    Cortex judge prompt, the notebook) must show this is approximate, never
    present it identically to a verified exact match.

    Returns (truth_dict, note_or_None). note is only set when NOTHING at
    all could be extracted."""
    headers, rows = _parse_view_csv(csv_text)
    if not rows:
        return {}, "view has no data rows"
    by_norm = {}
    for h in headers:
        by_norm.setdefault(_normalize_truth_header(h), h)

    if len(rows) == 1:
        row = rows[0]
        truth = {}
        for m in calc_metrics:
            h = by_norm.get(m["metric"].strip().lower())
            if h is None:
                continue
            v = _parse_numeric(row.get(h, ""))
            if v is not None:
                truth[m["name"]] = {"value": v, "approx": False, "rows": 1}
        return truth, (None if truth else
                       "single row but no header matched any metric caption")

    # MULTI-ROW VIEWS ARE NO LONGER SUMMED (user decision, 2026-07-30, after
    # the sum tier produced a 6x-inflated Quantity on its first live run).
    # Summing a rendered view's column is only correct when the view's rows
    # partition the data, and that is NOT reliably decidable from the CSV: a
    # measure crosstab repeats rows (inflates), a view filter drops them
    # (deflates), and a legitimate DETAIL listing has repeated dimension
    # values while still being summable. Accepting a sum only when it already
    # agrees with the app makes it circular -- a reference that can never
    # disagree validates nothing. TRUE aggregates now come from Tableau's own
    # engine via the VizQL Data Service (tableau_server.pull_tableau_
    # aggregates -> pull_vds_tableau_truth below), which needs no view to
    # display a total and cannot be inflated by row repetition.
    partitions, why_not = _rows_partition_data(headers, rows)
    detail = f" ({why_not})" if not partitions else ""
    return {}, (f"{len(rows)} rows -- a dimension breakdown, not a grand "
                f"total{detail}. Only a single-row view is used as an exact "
                "reference; true totals come from the VizQL Data Service.")


def pull_vds_tableau_truth(server_url, site_content_url, calc_metrics,
                            measure_captions=(), datasource_hints=(),
                            token_name=None, token_secret=None):
    """TRUE Tableau ground truth via the VizQL Data Service (option 1, chosen
    by the user 2026-07-30 over the unsound "sum a rendered view" tier).

    Asks TABLEAU'S OWN ENGINE for SUM(field) with no grouping, so the answer
    is a real grand total computed by Tableau against the governed
    datasource. Unlike a view-CSV sum it does not require any dashboard to
    display a total, cannot be inflated by a crosstab repeating rows, and
    cannot be deflated by a view's own filter -- there is no view involved.
    Everything it returns is therefore EXACT (approx=False); there is no
    approximate tier here by design.

    Only PURE-SUM metrics are requested: a ratio/LOD is not SUM(field) and
    asking VDS to sum it would be the same category error the old tier made.

    Returns (truth, notes) in the SAME shape pull_live_tableau_truth uses, so
    both callers and the UI stay identical."""
    import tableau_server as TS
    wanted = {}                       # caption -> truth key
    for cap in measure_captions:
        wanted.setdefault(cap, _raw_truth_key_from_caption(cap))
    for m in calc_metrics or []:
        if _is_pure_sum_sql(m.get("sql")):
            wanted.setdefault(m["metric"], m["name"])
    if not wanted:
        return {}, [{"datasource": "-", "error":
                     "no plain-SUM measure to ask Tableau for"}]
    res = TS.pull_tableau_aggregates(
        server_url, site_content_url, sorted(wanted),
        datasource_name_hints=list(datasource_hints) or None,
        token_name=token_name, token_secret=token_secret)
    truth = {}
    for cap, val in (res.get("values") or {}).items():
        key = wanted.get(cap)
        if key is not None:
            truth[key] = {"value": val, "approx": False, "rows": 1,
                          "source": "VDS"}
    return truth, (res.get("notes") or [])


def _raw_truth_key_from_caption(caption):
    """VDS answers by FIELD CAPTION and has no idea which datasource caption
    this project filed the measure under, so raw measures are keyed by caption
    alone here and reconciled in apply_live_truth_to_measures."""
    return f"rawcap::{caption}"


def _raw_truth_key(datasource, measure):
    """Stable truth-dict key for a RAW-COLUMN measure, namespaced so it can
    never collide with a cortex_semantic calculated-metric name."""
    return f"raw::{datasource}::{measure}"


def raw_measure_metrics(result):
    """Adapt check_workbook()'s RAW-COLUMN measure rows into the very same
    {name, metric, sql} shape truth_from_view_csv already matches against, so
    ONE matcher serves both raw columns and calculated fields.

    WHY THIS EXISTS (real gap, found 2026-07-30 from a live Stage 5 run): the
    raw-column table's "Tableau" column was fed ONLY by the hardcoded module
    -level TABLEAU_TRUTH dict -- literally one datasource ("Sample -
    Superstore") and three columns (SALES/PROFIT/QUANTITY), typed in by hand
    back when Superstore was the only demo workbook. So Discount showed "-"
    on the very workbook the dict covers, and EVERY measure on EVERY other
    workbook showed "-" permanently, no matter how live the Tableau REST
    connection was. R1/R2 had already built the dynamic machinery to pull
    real per-view Tableau values, but it was only ever handed calc_metrics --
    the raw columns, which are the EASIEST thing to match (a view exporting
    `SUM(Discount)` maps straight onto the Discount measure), were never
    offered to it at all.

    A raw-column measure is a bare SUM(col) by construction, so it passes
    _is_pure_sum_sql and is eligible for the approximate multi-row path too --
    which matters, because real client workbooks are full of multi-row
    dimension views and short on single-row KPI views (exactly what the
    2026-07-29 live test found)."""
    out = []
    for m in result.get("measures", []):
        col = m.get("column")
        if not col:
            continue
        out.append({"name": _raw_truth_key(m["datasource"], m["measure"]),
                    "metric": m["measure"],
                    "sql": f"SUM({col})"})
    return out


def _resummarize(result):
    """Recompute the roll-up after verdicts change (apply_live_truth_to_
    measures can flip a measure to BUG). The summary is what Stage 5's
    headline metrics and the PASS/FAIL stage label read from -- leaving it
    stale would show '13/13 measures pass' above a table containing a BUG."""
    rows = result.get("measures", [])
    calc_rows = result.get("calc_metrics", [])
    s = result["summary"]
    s["measures_checked"] = len(rows) + len(calc_rows)
    s["measures_pass"] = (
        sum(1 for r in rows if r["verdict"] in ("PASS", "EXECUTED"))
        + sum(1 for r in calc_rows if r["verdict"] in ("PASS", "EXECUTED")))
    s["measures_bug"] = (sum(1 for r in rows if r["verdict"] == "BUG")
                         + sum(1 for r in calc_rows if r["verdict"] == "BUG"))
    return s


def apply_live_truth_to_measures(result, truth):
    """Fold REST-pulled Tableau values into check_workbook()'s RAW-COLUMN
    measure rows, then re-judge and re-summarize. Returns the number of
    measures that got a real Tableau reference.

    THE EXACT/APPROXIMATE DISTINCTION DECIDES THE VERDICT, deliberately:

      * EXACT (a single-row Tableau view -- a true grand-total/KPI view,
        aggregated by Tableau itself exactly as it renders on screen) is
        treated like the hand-verified known figure it replaces: a real
        disagreement with the app's value flips the verdict to BUG.

      * APPROXIMATE (a multi-row view whose matched column was summed across
        rows) NEVER decides the verdict. A view can legitimately carry its
        own filter, so its column total legitimately differs from the app's
        unfiltered grand total -- calling that a BUG would manufacture
        precisely the false-BUG class this project has now been bitten by
        twice (R2's literal "unknown" reference, R8's partial app render).
        It is recorded, shown as approximate everywhere, and left for a human
        or Cortex to weigh -- surfaced, never silently dropped, never
        promoted to a defect it cannot prove."""
    applied = 0
    for m in result.get("measures", []):
        # Either keying wins: per-datasource (view-CSV path, which knows the
        # datasource) or caption-only (VDS path, which answers by field
        # caption and has no notion of this project's datasource captions).
        info = (truth.get(_raw_truth_key(m["datasource"], m["measure"]))
                or truth.get(_raw_truth_key_from_caption(m["measure"])))
        if not info:
            continue
        applied += 1
        m["tableau"] = info["value"]
        m["tableau_source"] = info.get("source") or "REST"
        m["tableau_approx"] = bool(info.get("approx"))
        m["tableau_rows"] = info.get("rows")
        agrees = _rel_ok(m.get("app"), info["value"])
        if m["tableau_approx"]:
            m["tableau_note"] = (
                f"approximate — summed across {info.get('rows')} rows"
                + ("" if agrees else
                   "; differs from the app value, which a filtered view can "
                   "legitimately cause — review, not a confirmed defect"))
        else:
            m["tableau_note"] = "exact (single-row Tableau view)"
            if not agrees:
                m["verdict"] = "BUG"
    if applied:
        _resummarize(result)
    return applied


def pull_live_tableau_truth(server_url, site_content_url, workbook_id, calc_metrics,
                             token_name=None, token_secret=None):
    """R2 -- the REAL per-section ground truth for tableau_truth, pulled live
    over REST (tableau_server.pull_all_view_csvs, one session for the whole
    workbook) and mapped to calc_metrics via truth_from_view_csv.

    Merges across every view in the workbook: a VERIFIED EXACT match always
    wins over an APPROXIMATE one, regardless of view order (upgrading an
    earlier approximate match if a later view turns out to have the real
    single-row figure); among matches of the SAME confidence tier, the
    FIRST view to resolve a metric wins -- a later view naming the same
    metric again does not silently overwrite an already-resolved value, so
    an ambiguous workbook (the same calc shown on two different dashboards)
    can't flip-flop which number gets trusted depending on view iteration
    order.

    Returns (truth, notes). notes is a list of {"view", "rows", "matched",
    "skipped"} -- one entry per view -- so the caller can show EXACTLY which
    view supplied which number and why any view contributed nothing, never a
    black box."""
    import tableau_server as TS
    view_csvs = TS.pull_all_view_csvs(server_url, site_content_url, workbook_id,
                                      token_name, token_secret)
    truth, notes = {}, []
    for vc in view_csvs:
        if vc["error"]:
            notes.append({"view": vc["view"], "rows": 0, "matched": [],
                          "skipped": vc["error"]})
            continue
        vt, reason = truth_from_view_csv(vc["csv"], calc_metrics)
        matched = []
        for name, info in vt.items():
            existing = truth.get(name)
            if existing is None or (existing["approx"] and not info["approx"]):
                truth[name] = info
                matched.append(name)
        headers_, rows_ = _parse_view_csv(vc["csv"])
        # The view's ACTUAL exported column headers. Without these, "why did
        # measure X not get a Tableau value?" is unanswerable from the UI and
        # turns into guesswork -- the columns Tableau really published are the
        # whole answer (if no view exports a Discount column, "—" is the
        # honest result, not a matching failure).
        notes.append({"view": vc["view"], "rows": len(rows_),
                      "columns": list(headers_),
                      "matched": matched, "skipped": reason})
    return truth, notes


def _sql_lit(s):
    """A single-quoted Snowflake string literal: collapse whitespace + double
    single-quotes so a formula/SQL fragment embeds safely in a CORTEX.COMPLETE
    call."""
    return "'" + " ".join(str(s).split()).replace("'", "''") + "'"


def _section_prompt(cap, formula, app_sql, tv_lit):
    """The ONE narration prompt -- used both by the downloadable section
    notebook's Cortex cell and by any live in-app call (cortex_narrate_section
    below). One canonical prompt, not two independently-maintained copies."""
    return (f"A Tableau metric {cap} (TWB formula: {formula}) was migrated to "
            f"Streamlit-in-Snowflake as this SQL: {app_sql}. The Tableau "
            f"reference value/range is {tv_lit}. In ONE sentence, state whether "
            "the migrated value should match Tableau and, if there is any risk "
            "of divergence, the most likely cause.")


def cortex_narrate_section(session, cap, formula, app_sql, tv_lit,
                            model="claude-sonnet-4-5"):
    """LIVE, in-app Cortex narration for one section/metric -- runs
    SNOWFLAKE.CORTEX.COMPLETE for real, inside the caller's Snowflake session,
    instead of only ever shipping as an inert cell in the downloadable
    notebook. Soft-fails: returns (None, 0, reason) rather than raising, so a
    Cortex hiccup never breaks Stage 5's proof.

    Returns (narrative_text_or_None, estimated_tokens, error_or_None)."""
    prompt = _section_prompt(cap, formula, app_sql, tv_lit)
    try:
        rows = session.sql(
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE({model!r}, "
            f"{_sql_lit(prompt)}) AS R").collect()
        text = (str(rows[0][0]).strip() if rows and rows[0][0] is not None else "")
        tokens = (len(prompt) + len(text)) // 4   # ~4 chars/token estimate
        return (text or None), tokens, (None if text else "empty response")
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"


def _extract_json_obj(text):
    """First decodable JSON OBJECT in mixed model output (odd whitespace,
    accidental markdown fences) -- dict analogue of cortex_calc_fallback's
    json_payload (which hunts for a JSON ARRAY instead).

    ALSO handles a DOUBLE-ENCODED payload: the whole response being a JSON
    *string* that itself contains the JSON object --
    `"{\\"verdict\\": \\"BUG\\", ...}"` rather than `{"verdict": "BUG", ...}`.
    FOUND LIVE 2026-07-29: `AI_COMPLETE` returns a VARIANT, and stringifying a
    VARIANT yields its JSON REPRESENTATION (quoted + backslash-escaped), not
    its bare text. Every R8 vision verdict came back "UNKNOWN" with the raw
    escaped JSON dumped into the note column, because raw_decode() hit the
    `\\"` escape immediately after the opening brace and failed on every
    candidate `{`. The verdicts Cortex actually returned were perfectly
    well-formed -- they were never being read. Unwrapping is bounded (a few
    layers, not `while True`) so a pathological response cannot spin here."""
    raw = (text or "").strip()
    if not raw:
        return None
    dec = json.JSONDecoder()
    for _ in range(3):
        try:
            outer = json.loads(raw)
        except Exception:
            break
        if isinstance(outer, dict):
            return outer
        if isinstance(outer, str):
            raw = outer.strip()      # one layer of string-encoding peeled
            continue
        break                        # a list/number/bool is not our object
    for m in re.finditer(r"\{", raw):
        try:
            val, _ = dec.raw_decode(raw[m.start():])
            if isinstance(val, dict):
                return val
        except Exception:
            continue
    return None


def _judge_prompt(cap, formula, app_sql, app_value, tv_lit):
    return (
        "You are validating a Tableau-to-Snowflake migration. Compare these "
        "two ALREADY-COMPUTED values for the same metric and judge whether "
        "they agree within reasonable rounding/float tolerance. Do not "
        "recompute either value yourself -- both are given.\n\n"
        f"Metric: {cap}\n"
        f"Tableau's rendered value (pulled from Tableau's own REST API -- "
        f"ground truth): {tv_lit}\n"
        f"Streamlit app's computed value (from executing this SQL: "
        f"{app_sql}): {app_value}\n"
        f"Original Tableau formula (context only, do not recompute): "
        f"{formula}\n\n"
        "Respond with ONLY a JSON object, no markdown fences, no other "
        'text: {"verdict": "PASS" or "BUG", "explanation": "one sentence, '
        'naming the most likely cause if BUG"}.')


def cortex_judge_section(session, cap, formula, app_sql, app_value, tv_lit,
                          model="claude-sonnet-4-5"):
    """R2 -- Cortex OWNS the PASS/BUG verdict for one section (2026-07-28,
    explicit user decision; supersedes this project's earlier 'Cortex only
    narrates' rule FOR R2 SPECIFICALLY -- check_calc_metrics and every other
    execution-gated AI path in this project, e.g. the calc fallback, are
    UNCHANGED and still keep AI out of the decision entirely).

    The boundary that keeps this safe: app_value and tv_lit are both handed
    in ALREADY COMPUTED -- app_value came from actually executing app_sql
    against the migrated table, tv_lit came from
    tableau_server.query_view_data_csv() (Tableau's own rendered number, over
    REST, never derived by an LLM). Cortex's only job is a JUDGMENT call on
    two known numbers, never a computation of either one -- it can misjudge a
    comparison, it cannot fabricate a figure it was never asked to produce.

    Returns (verdict, explanation, tokens_est, error). verdict is one of
    "PASS" / "BUG" / "UNKNOWN" -- UNKNOWN (never a silent PASS) whenever the
    response can't be parsed or the call fails, so a Cortex hiccup surfaces
    as a visible gap, never a false clean bill of health."""
    prompt = _judge_prompt(cap, formula, app_sql, app_value, tv_lit)
    try:
        rows = session.sql(
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE({model!r}, "
            f"{_sql_lit(prompt)}) AS R").collect()
        text = (str(rows[0][0]).strip() if rows and rows[0][0] is not None else "")
    except Exception as e:
        return "UNKNOWN", None, 0, f"{type(e).__name__}: {e}"
    tokens = (len(prompt) + len(text)) // 4
    obj = _extract_json_obj(text)
    if not obj or "verdict" not in obj:
        return "UNKNOWN", (text or None), tokens, \
            "could not parse a verdict from Cortex's response"
    verdict = str(obj.get("verdict", "")).strip().upper()
    if verdict not in ("PASS", "BUG"):
        return "UNKNOWN", text, tokens, f"unrecognized verdict {verdict!r}"
    explanation = str(obj.get("explanation", "")).strip() or None
    return verdict, explanation, tokens, None


def ensure_vision_stage(session, stage_fqn):
    """R8 -- create the Cortex-vision staging area if it doesn't exist, with
    the ENCRYPTION type AI_COMPLETE's file input actually requires.
    CONFIRMED LIVE 2026-07-29: Snowflake's DEFAULT stage encryption (client-
    side) is REJECTED by AI_COMPLETE -- 'Input files from stages with Client
    Side Encryption is not supported.' CREATE STAGE with SNOWFLAKE_SSE must
    be used explicitly, never the bare default.

    Idempotent via IF NOT EXISTS -- deliberately does NOT touch an already-
    existing stage. If a stage with this name already exists with the WRONG
    encryption (created before this requirement was known), this function
    will not silently replace it -- that could drop files a caller doesn't
    expect gone. The AI_COMPLETE call itself will surface the exact same
    clear error in that case, which is the correct place for it to surface,
    not a guess made here."""
    session.sql(
        f"CREATE STAGE IF NOT EXISTS {stage_fqn} "
        "ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')").collect()


def _stage_png(session, png_bytes, stage_fqn, filename):
    """Upload PNG bytes straight to a stage -- no temp file on disk, same
    'never touch disk unnecessarily' discipline as R1's
    download_workbook_bytes."""
    import io
    session.file.put_stream(io.BytesIO(png_bytes), f"@{stage_fqn}/{filename}",
                            auto_compress=False, overwrite=True)


def _unwrap_variant_text(text):
    """`AI_COMPLETE` returns a VARIANT; stringifying it yields the value's
    JSON REPRESENTATION, so plain prose arrives wrapped in quotes with its
    newlines escaped (`"## Overall Layout\\n\\nThe dashboard..."`) rather than
    as real text. OBSERVED LIVE 2026-07-30 against the real account. Left
    as-is, every vision description carries literal `\\n` sequences and
    surrounding quotes into the comparison prompt, degrading the comparison
    for no reason. Decodes one layer when the payload really is a JSON
    string; otherwise returns the text untouched."""
    raw = (text or "").strip()
    if len(raw) > 1 and raw[0] == '"' and raw[-1] == '"':
        try:
            val = json.loads(raw)
            if isinstance(val, str):
                return val.strip()
        except Exception:
            pass
    return raw


def _describe_image_via_cortex(session, stage_fqn, filename, focus,
                                model="claude-opus-5"):
    """R8 -- one Cortex vision call describing ONE already-staged image.
    Uses the call shape CONFIRMED WORKING LIVE 2026-07-29: PROMPT() needs an
    EXPLICIT {0} placeholder marking where the file binds in -- omitting it
    does not error, it silently returns NULL (found live; easy to
    misdiagnose as 'vision unsupported' when it is actually a syntax gap).

    Returns (description_or_None, tokens_est, error_or_None)."""
    prompt = (f"{focus} Describe the dashboard's KPI numbers, chart types, "
              "and key values precisely -- exact figures where visible, not "
              "vague summaries. Image: {0}")
    try:
        rows = session.sql(
            f"SELECT AI_COMPLETE({model!r}, PROMPT({_sql_lit(prompt)}, "
            f"TO_FILE({_sql_lit('@' + stage_fqn)}, {_sql_lit(filename)}))) "
            "AS R").collect()
        text = (str(rows[0][0]).strip() if rows and rows[0][0] is not None else "")
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"
    tokens = (len(prompt) + len(text)) // 4
    if not text or text.lower() == "none":
        return None, tokens, "AI_COMPLETE returned an empty/null response"
    return _unwrap_variant_text(text), tokens, None


def _omitted_sheets(app_render_notes):
    """Sheet names headless_render could NOT draw into the app-side image
    (KPI/text-only tiles, Plotly/map sheets) -- absent from image B BY
    DESIGN, not because the migration lost them."""
    return [n.get("sheet") for n in (app_render_notes or [])
            if not n.get("rendered") and n.get("sheet")]


def _compare_descriptions_via_cortex(session, dashboard_name, desc_a, desc_b,
                                      model="claude-opus-5",
                                      app_render_notes=None):
    """R8 -- compare two INDEPENDENTLY Cortex-generated text descriptions of
    two images (Tableau original vs. the migrated app) via a PLAIN-TEXT
    AI_COMPLETE call. Deliberately NOT a single multi-image PROMPT() call --
    only the single-image PROMPT()+TO_FILE() shape was live-verified this
    session; comparing two already-generated text descriptions stays inside
    CONFIRMED territory instead of assuming an untested multi-image binding
    works the same way.

    Returns (verdict, explanation, tokens_est, error) -- same shape as
    cortex_judge_section, same UNKNOWN-never-a-silent-PASS rule.

    app_render_notes (headless_render's per-sheet notes) is NOT optional in
    practice, it is the fix for a real false-BUG class found live 2026-07-29:
    headless_render draws Altair CHARTS only, so a dashboard's KPI tiles and
    Plotly/map sheets are legitimately ABSENT from the app-side image. Without
    being told that, Cortex faithfully reported "the migrated app is missing
    the KPI summary panel" as a BUG on essentially every dashboard that has
    KPI tiles -- describing a RENDERER scope limit as a MIGRATION defect. The
    app itself renders those tiles correctly in the live app; only the PNG
    exporter cannot. Naming the omissions explicitly keeps the comparison
    honest in both directions: the judgment covers what image B could
    actually show, and what it could not is stated rather than quietly
    excluded."""
    omitted = _omitted_sheets(app_render_notes)
    caveat = ""
    if omitted:
        caveat = (
            "\n\nCRITICAL CONTEXT -- image B was produced by a chart-only "
            "exporter that CANNOT draw these sheets of the dashboard: "
            + ", ".join(f"'{s}'" for s in omitted)
            + ". Their absence from description B is EXPECTED and is NOT a "
            "bug -- the live app does render them; only the static exporter "
            "cannot. Do NOT report them as missing, omitted, or lost. Judge "
            "ONLY the content image B was actually able to render, and if "
            "the only differences you can find are these known-absent "
            "sheets, the verdict is PASS.")
    prompt = (
        "Two descriptions were independently generated by AI vision from "
        f"two images of the SAME dashboard, '{dashboard_name}' -- one from "
        "the original Tableau dashboard, one from a migrated Streamlit app "
        "that is SUPPOSED to reproduce it exactly. Compare them: do they "
        "describe the same KPI numbers, chart types, and overall story? "
        "Minor wording/styling/color differences between the TEXT "
        "descriptions are expected and NOT a bug -- flag only genuine DATA "
        "or STRUCTURAL differences (different numbers, a missing chart, "
        "wrong chart type)."
        + caveat + "\n\n"
        f"Description A (Tableau original):\n{desc_a}\n\n"
        f"Description B (migrated Streamlit app):\n{desc_b}\n\n"
        "Respond with ONLY a JSON object, no markdown fences, no other "
        'text: {"verdict": "PASS" or "BUG", "explanation": "one or two '
        'sentences naming the specific difference if BUG"}.')
    try:
        rows = session.sql(
            f"SELECT AI_COMPLETE({model!r}, {_sql_lit(prompt)}) AS R").collect()
        text = (str(rows[0][0]).strip() if rows and rows[0][0] is not None else "")
    except Exception as e:
        return "UNKNOWN", None, 0, f"{type(e).__name__}: {e}"
    tokens = (len(prompt) + len(text)) // 4
    obj = _extract_json_obj(text)
    if not obj or "verdict" not in obj:
        return "UNKNOWN", (text or None), tokens, \
            "could not parse a verdict from Cortex's response"
    verdict = str(obj.get("verdict", "")).strip().upper()
    if verdict not in ("PASS", "BUG"):
        return "UNKNOWN", text, tokens, f"unrecognized verdict {verdict!r}"
    explanation = str(obj.get("explanation", "")).strip() or None
    return verdict, explanation, tokens, None


def vision_validate_dashboard(session, stage_fqn, dashboard_name,
                               tableau_png, app_png, model="claude-opus-5",
                               app_render_notes=None):
    """R8 -- the full per-dashboard vision-validation flow: stage both
    images to an SSE-encrypted stage (confirmed hard requirement, live,
    2026-07-29), describe each independently via Cortex vision, then
    compare the two text descriptions via a third, plain-text call.

    ARCHITECTURE (same rule as R2's cortex_judge_section): Cortex is handed
    two ALREADY-REAL artifacts -- the actual staged Tableau image and the
    actual staged app-rendered image -- and judges/describes; it computes
    neither. tableau_png should come from tableau_server.query_view_image
    or pull_all_view_images (Tableau's own REST-rendered pixels, never a
    screenshot of Tableau's UI); app_png from headless_render.
    render_dashboard_to_png (the app's own chart objects, never a
    screenshot of the SSO-gated deployed app).

    Returns {"verdict", "explanation", "tableau_description",
    "app_description", "tokens", "errors"}. verdict is "UNKNOWN" (never a
    silent PASS) if either image fails to describe or the comparison call
    fails -- errors is a list of every step's failure message, so a partial
    failure is never silently swallowed."""
    ensure_vision_stage(session, stage_fqn)
    slug = re.sub(r"[^0-9A-Za-z]+", "_", dashboard_name).strip("_") or "dashboard"
    tableau_fname, app_fname = f"tableau_{slug}.png", f"app_{slug}.png"
    _stage_png(session, tableau_png, stage_fqn, tableau_fname)
    _stage_png(session, app_png, stage_fqn, app_fname)

    errors = []
    desc_a, tok_a, err_a = _describe_image_via_cortex(
        session, stage_fqn, tableau_fname,
        "This is a screenshot of a Tableau dashboard.", model)
    if err_a:
        errors.append(f"Tableau image: {err_a}")
    desc_b, tok_b, err_b = _describe_image_via_cortex(
        session, stage_fqn, app_fname,
        "This is a screenshot of a migrated Streamlit dashboard.", model)
    if err_b:
        errors.append(f"App image: {err_b}")

    if desc_a is None or desc_b is None:
        return {"verdict": "UNKNOWN", "explanation": None,
               "tableau_description": desc_a, "app_description": desc_b,
               "tokens": tok_a + tok_b, "errors": errors}

    verdict, explanation, tok_c, err_c = _compare_descriptions_via_cortex(
        session, dashboard_name, desc_a, desc_b, model,
        app_render_notes=app_render_notes)
    if err_c:
        errors.append(f"comparison: {err_c}")
    return {"verdict": verdict, "explanation": explanation,
           "tableau_description": desc_a, "app_description": desc_b,
           "omitted_sheets": _omitted_sheets(app_render_notes),
           "tokens": tok_a + tok_b + tok_c, "errors": errors}


# =============================================================================
# DASHBOARD-SECTION validation -- the dashboard-validation methodology (one
# combined LIVE query per DASHBOARD, not per calculated field; a deterministic
# TWB-vs-app formula comparison per measure; ONE Cortex narrative for the whole
# section; a final cross-section bug rollup). Mirrors a WBR-style validation
# report's shape: section = a functional area of the dashboard, not a metric.
# =============================================================================

# The COMPLETE closed set of where a field can live on a sheet -- enumerated
# from engine.py's own `sh.get(...)` reads (the rendering engine IS the
# ground truth for where a pill can be, since it has to read every one of
# these to draw the chart). The first version of this scan only looked at
# x/y/color/labels/text_fields -- correct for a simple bar/scatter sheet, but
# an mbar (multi-measure bar) sheet stores its WHOLE measure list under
# `measures` (a list of dicts) and its breakdown dimension under a bare `dim`
# string, neither of which lives in x/y/color. That silently dropped an
# entire sheet's measures (Region, Count of Customers, Quantity, Sales per
# Customer all missing from Customer Analysis) -- the exact "enumerate the
# whole schema surface, don't patch one instance" mistake this project has
# been burned by before, now fixed the same way: the closed set, not another
# partial guess.
_PILL_KEYS = ("x", "y", "ys", "y_dims", "ydim", "color", "color_measure",
             "labels", "text")
_MEASURE_ONLY_KEYS = ("measure", "measures")
# Always-dimension keys (never a measure regardless of agg) -- used for the
# MEASURE-vs-DIMENSION classification below.
_DIM_ONLY_KEYS = ("dim", "dims", "detail", "segment", "facet_col")
# Of those, only these EXPLICITLY declare "this is the chart's breakdown"
# (an mbar sheet's `dim`, a faceted `facet_col`, a `segment`). `detail` is
# deliberately EXCLUDED: it identifies individual points/rows (e.g. one dot
# per customer on a scatter) -- a real dimension, but not a declared
# breakdown -- so it's WEAK, the same tier as a bare axis label.
_STRONG_DIM_KEYS = ("dim", "dims", "segment", "facet_col")
_NAME_LIST_KEYS = ("text_fields", "tooltip_fields")
_PILL_SENTINELS = {"Multiple Values"}

# The canonical aggregation-token vocabulary (calc_translator.AGGS is the
# single source of truth this project already keys everything off of --
# reused here rather than re-guessing the token spelling a second time; the
# first version of this file used "cntd", which does not exist -- the real
# token is "ctd", and that one wrong letter silently dropped every COUNT
# DISTINCT measure, e.g. Customer Analysis's "Count of Customers").
from calc_translator import AGGS as _MEASURE_AGGS


def _pill_caption_agg(value):
    """Normalize ANY shelf value shape -- a single pill dict, a list of pill
    dicts, a bare caption string, or a list of caption strings -- into
    (caption, agg_or_None, kind_or_None, label_or_None) tuples. `label` is a
    display override an mbar sheet's `measures` entry often carries (e.g.
    'Count of Customers' for a plain 'Customer Name' caption aggregated with
    ctd) -- shown in the report instead of the raw field name."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [(value, None, None, None)]
    if isinstance(value, dict):
        cap = value.get("caption")
        return [(cap, value.get("agg"), value.get("kind"), value.get("label"))] if cap else []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_pill_caption_agg(item))
        return out
    return []


def _sheet_pill_captions(sheet):
    """Every field a sheet actually uses, split into (measure captions -> the
    aggregation token they were used with, STRONG dimension candidates, WEAK
    dimension candidates) -- scanned across the full closed set of shelf keys
    above, not just x/y/color. Classification: a real aggregation token
    (calc_translator.AGGS) or a measure-ONLY key means MEASURE. A dimension is
    STRONG when it comes from a key that EXPLICITLY declares "this is the
    breakdown" (dim/dims/segment/facet_col, or a pill tagged kind='dimension')
    -- e.g. an mbar sheet's bare `dim: 'Region'`. It is WEAK when it's merely
    incidental -- a bare y-axis label or a `detail` pill used to identify
    individual points (e.g. 'Customer Name' on a scatter), or a name pulled
    from a tooltip/text field with no aggregation info at all. This
    distinction matters: on Superstore's real Customer Analysis dashboard,
    'Customer Name' appears as a WEAK candidate twice (a rank chart's bare
    y-axis, a scatter's per-point detail) while 'Region' appears once as a
    STRONG one (the overview chart's actual declared breakdown) -- a plain
    occurrence count picked Customer Name (800 rows, one per customer) over
    Region (the dashboard's own explicitly-declared grain), which is not what
    'this sheet groups by Region' means. A caption confirmed as a measure
    ANYWHERE on the sheet wins over any dimension-candidate appearance
    elsewhere. The agg token is kept (not just the bare caption) because a
    plain physical field's aggregation is NOT always SUM -- 'Customer Name'
    used with agg='ctd' (Count of Customers) must resolve to
    COUNT(DISTINCT CUSTOMER_NAME), never SUM(CUSTOMER_NAME) (a real query
    error found live: Snowflake can't SUM a text column)."""
    measures, strong_dims, weak_dims = {}, set(), set()
    for key in _PILL_KEYS + _MEASURE_ONLY_KEYS + _DIM_ONLY_KEYS:
        for cap, agg, kind, label in _pill_caption_agg(sheet.get(key)):
            if key in _MEASURE_ONLY_KEYS or agg in _MEASURE_AGGS or \
                    (agg == "usr" and kind != "dimension"):
                if cap not in measures or agg in _MEASURE_AGGS:
                    measures[cap] = (agg, label)  # a real agg token wins over a bare/'usr' one
            elif key != "detail" and (key in _STRONG_DIM_KEYS or kind == "dimension"):
                strong_dims.add(cap)
            elif key in _DIM_ONLY_KEYS or agg in (None, "none", "usr"):
                weak_dims.add(cap)
    for key in _NAME_LIST_KEYS:
        for cap in (sheet.get(key) or []):
            if cap and cap not in _PILL_SENTINELS:
                weak_dims.add(cap)
    strong_dims -= set(measures)
    weak_dims -= set(measures) | strong_dims
    return measures, strong_dims, weak_dims


def collect_dashboard_section(ir, dashboard):
    """One dashboard -> its dominant datasource, real table, the measure
    captions its sheets actually use (each with its real aggregation token),
    and a GROUP BY dimension IF one resolves to a real column on that table
    (never invented -- None means the section validates as one combined
    row). An EXPLICITLY-declared breakdown dimension (a sheet's own `dim`/
    `segment`/etc., see _sheet_pill_captions) always outranks a merely
    incidental one (a bare axis label or a scatter's per-point detail), and
    within each tier the MOST-USED candidate wins (not just the first found
    in set-iteration order, which isn't even deterministic).
    Returns None if the dashboard has no datasource with a real table."""
    import config
    ds_counter = Counter(s.get("datasource") for s in dashboard["sheets"]
                         if s.get("datasource"))
    if not ds_counter:
        return None
    primary_ds = ds_counter.most_common(1)[0][0]
    entry = config.DATASOURCES.get(primary_ds)
    table = entry.get("table") if entry else None
    if not table:
        return None
    measures = {}
    strong_counts, weak_counts = Counter(), Counter()
    for s in dashboard["sheets"]:
        if s.get("datasource") != primary_ds:
            continue
        m, strong_d, weak_d = _sheet_pill_captions(s)
        for cap, (agg, label) in m.items():
            if cap not in measures or agg in _MEASURE_AGGS:
                measures[cap] = (agg, label)
        strong_counts.update(strong_d)
        weak_counts.update(weak_d)
    # A caption confirmed as a measure on ANY sheet in this dashboard must
    # never win the GROUP BY slot on the strength of an incidental dimension
    # appearance on a DIFFERENT sheet in the same dashboard -- _sheet_pill_
    # captions only de-dupes WITHIN one sheet. A real bug found live:
    # "Profit" is a measure everywhere on Executive Overview, but slipped
    # into the weak-dimension pool via one sheet's tooltip/text mention,
    # winning group_dim and producing "GROUP BY raw Profit value" -- 7,575
    # nonsensical one-row-per-value groups instead of a real breakdown.
    for cap in measures:
        strong_counts.pop(cap, None)
        weak_counts.pop(cap, None)
    table_cols = CS._table_columns(entry) or set()
    group_dim = None
    for dim_counts in (strong_counts, weak_counts):
        for cap, _n in dim_counts.most_common():
            phys = to_phys(cap)
            if phys in table_cols:
                group_dim = (cap, phys)
                break
        if group_dim:
            break
    return {"datasource": primary_ds, "table": table, "measure_caps": measures,
            "table_cols": table_cols, "group_dim": group_dim}


def _resolve_measure_sql(ir, cap, agg, table_cols, all_metrics):
    """caption (+ the agg token it was used with) -> (app_sql_expr, kind,
    ref) or None if it can't be resolved against this table's real columns.
    kind='calc' reuses the SAME metric resolution check_calc_metrics/
    check_workbook already use (one canonical answer to 'is this a calc');
    kind='column' is a plain field, aggregated via engine._agg_expr -- the
    SAME pill-to-SQL translator the actual generated app uses, reused rather
    than a second hand-rolled copy that could (and did: this file's first
    version always assumed SUM) silently diverge from what the app really
    does."""
    for m in all_metrics:
        if cap in m["synonyms"] and m["cols"] <= table_cols:
            return m["sql"], "calc", m["name"]
    phys = to_phys(cap)
    if phys in table_cols:
        import engine as _engine
        return _engine._agg_expr(phys, agg), "column", phys
    return None


_AGG_RE = re.compile(r"\b(SUM|AVG|COUNT|COUNTD|MIN|MAX|MEDIAN|STDDEV|VARIANCE)\b")
_OP_RE = re.compile(r"[/*+\-]")
_COUNT_DISTINCT_RE = re.compile(r"COUNT\s*\(\s*DISTINCT\b")


def _formula_match(twb, app_sql, kind):
    """Deterministic (no AI) formula-equivalence check -- decided the same
    way every other verdict in this file is: by comparing what the formulas
    actually DO, never guessed and never left to Cortex. A plain physical
    column always matches (both sides reference the same field by
    definition). A calc's Tableau formula and the generated SQL legitimately
    use different field NAMES (caption vs physical column), so this compares
    the numeric SHAPE -- which TRUE aggregations and operators are used --
    which is exactly what catches a real bug like a different denominator
    field, without false-positiving on every calc whose column got renamed.

    NULLIF/CASE are deliberately EXCLUDED from the aggregation set: the
    deterministic translator adds NULLIF(..., 0) as a divide-by-zero guard on
    plenty of correct ratio calcs (e.g. Profit Ratio), and that guard changes
    NOTHING about the aggregation Tableau performs -- counting it as a
    'different aggregation function' was a real false positive caught while
    building this (Profit Ratio flagged as a bug it never was).

    COUNT(DISTINCT x) is normalized to COUNTD before comparison: Tableau's
    own formula spells it countD, but the generated SQL always expands it to
    COUNT(DISTINCT ...) -- two spellings of the exact same operation. Without
    this, EVERY calc using countD (Sales per Customer, Profit per Order, ...)
    was flagged as a bug for using "COUNT" instead of "COUNTD" -- a second
    real false positive found live on the same run as the NULLIF one."""
    if kind == "column":
        return True, None
    def shape(s):
        s = _COUNT_DISTINCT_RE.sub("COUNTD(", s.upper())
        return (sorted(_AGG_RE.findall(s)),
                sorted(_OP_RE.findall(s)))
    twb_aggs, twb_ops = shape(twb)
    app_aggs, app_ops = shape(app_sql)
    if twb_aggs != app_aggs:
        return False, (f"different aggregation functions -- Tableau uses "
                       f"{twb_aggs or 'none'}, the app uses {app_aggs or 'none'}")
    if twb_ops != app_ops:
        return False, (f"different operators -- Tableau: "
                       f"{''.join(twb_ops) or 'none'}, app: {''.join(app_ops) or 'none'}")
    return True, None


def _narrate_section_summary(session, title, columns, rows, formula_rows,
                             model="claude-sonnet-4-5"):
    """ONE live Cortex call for a WHOLE section: given its real query result
    and its formula-comparison table, produce a short Summary of Findings.
    ARCHITECTURE RULE (unchanged): every Match?/BUG verdict was already
    decided by _formula_match before this is ever called; Cortex explains,
    it never re-decides. Soft-fails like cortex_narrate_section."""
    sample = rows[:5]
    data_txt = "; ".join(
        ", ".join(f"{c}={r.get(c)}" for c in columns) for r in sample) or "(no rows)"
    mismatches = [f"{r['metric']}: {r['impact']}" for r in formula_rows if not r["match"]]
    formula_txt = "all formulas match" if not mismatches else "; ".join(mismatches)
    prompt = (f"Section '{title}' of a migrated Tableau dashboard. Sample data "
             f"from the live query just run: {data_txt}. Formula comparison "
             f"result: {formula_txt}. In 1-2 sentences, summarize whether this "
             "section's data validates cleanly, and if not, name the specific "
             "issue and its likely business impact.")
    try:
        r = session.sql(f"SELECT SNOWFLAKE.CORTEX.COMPLETE({model!r}, "
                        f"{_sql_lit(prompt)}) AS R").collect()
        text = str(r[0][0]).strip() if r and r[0][0] is not None else ""
        tokens = (len(prompt) + len(text)) // 4
        return (text or None), tokens, (None if text else "empty response")
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"


def _value_lookup_from_check_result(res):
    """check_workbook()'s result already computes a REAL three-way value per
    measure -- App (the generated SQL, executed), Source/Backend (an
    INDEPENDENT recomputation: a direct read of the local extract file, or a
    client-side re-pull+sum of the same table when no local file exists --
    see check_workbook's own docstring), and Tableau (a known-figure bound,
    or a REST/VDS-pulled real Tableau value when apply_live_truth_to_measures
    has already run). This builds a {(datasource, caption_or_metric_name):
    row} lookup so a dashboard SECTION can show the SAME already-computed
    values instead of a second, potentially-divergent computation -- the
    project's standing rule against two paths disagreeing on one number."""
    lookup = {}
    for m in (res or {}).get("measures", []):
        lookup[(m["datasource"], m["measure"])] = {
            "app": m.get("app"), "source": m.get("source"),
            "source_kind": m.get("source_kind"), "tableau": m.get("tableau"),
            "tableau_note": m.get("tableau_note"), "verdict": m.get("verdict")}
    for m in (res or {}).get("calc_metrics", []):
        bound = m.get("tableau_bound")
        tab_txt = f"{bound[0]}–{bound[1]}" if bound else None
        row = {"app": m.get("value"), "source": None, "source_kind": None,
               "tableau": tab_txt, "tableau_note": "known-figure bound" if bound else None,
               "verdict": m.get("verdict")}
        lookup[(m["datasource"], m["metric"])] = row
        lookup[(m["datasource"], m["name"])] = row      # calc's internal name, for ref-based lookup
    return lookup


def compute_interaction_proof(dashboard, table, table_cols):
    """R11 -- REAL automated Interaction Proof for one dashboard: FILTER and
    TOOLTIP, the two interaction classes this project can actually prove
    deterministically without a browser.

    FILTER: drives engine.build_where() through its OWN real code path
    (headless_render._mocked_widgets(pick_real=True) picks an ACTUAL filter
    value instead of "All"), then verifies with two independent live
    queries against THIS dashboard's own table that the resulting WHERE
    clause truly restricts to that value -- not just that build_where ran
    without raising. `table_cols` scopes this to columns that exist on this
    dashboard's OWN table: engine.build_where reads the single global
    config.ORDERS, which can legitimately differ from a specific
    dashboard's datasource on a multi-datasource workbook; a filter part
    for a column this table doesn't have is SKIPPED (matching engine.
    _parts_for_sheet's own real per-sheet scoping), never reported as a
    false failure.

    TOOLTIP: captures each sheet's REAL rendered Altair chart (the exact
    object engine.py hands Streamlit, via headless_render.capture_sheet_
    chart) and checks whether Tableau's declared tooltip_fields captions
    (parsed straight from the .twb) actually appear as the chart's real
    tooltip labels (headless_render.extract_tooltip_titles) -- not
    inferred from the sheet's shelf pills.

    HONEST SCOPE, stated in every row: this proves the APP'S OWN
    interaction mechanics work correctly and deterministically. It does
    NOT observe a live Tableau click -- no browser automation of Tableau
    exists in this project (see R1/R8's own docstrings for why that was
    rejected) -- so this is never presented as a live Tableau-vs-Streamlit
    comparison, only as app-side proof.

    Returns a list of rows: {interaction, tableau, streamlit, proof, status}."""
    import headless_render as HR
    import engine as _engine
    from backend import run_sql as _run_sql

    rows = []
    try:
        with HR._mocked_widgets(pick_real=True):
            parts = _engine.build_where(dashboard)
    except Exception as e:
        parts = []
        rows.append({
            "interaction": "Filter widgets",
            "tableau": "Dashboard filter zone(s) restrict dependent sheets to the selected value.",
            "streamlit": f"engine.build_where() raised {type(e).__name__}: {e}",
            "proof": "App-side proof only -- no live Tableau click observed.",
            "status": "FAIL"})

    seen_cols = set()
    for p in parts:
        if not isinstance(p, dict) or not p.get("clause"):
            continue
        col, clause, cap = p["col"], p["clause"], p["caption"]
        if col in seen_cols:
            continue
        if col not in table_cols:
            continue          # this filter part belongs to a different datasource's table
        seen_cols.add(col)
        try:
            total = int(_run_sql(f"SELECT COUNT(*) AS N FROM {table}")["N"].iloc[0])
            filtered = int(_run_sql(f"SELECT COUNT(*) AS N FROM {table} WHERE {clause}")["N"].iloc[0])
            in_range = 0 <= filtered <= total
            # THE ACTUAL RESTRICTION CHECK -- shape-aware, never a blanket
            # "distinct raw column == 1" (a real bug caught building this: a
            # date-part/date-range clause LEGITIMATELY has many distinct raw
            # dates even when it correctly restricts to one year/range --
            # the naive check produced a false FAIL on every date filter).
            cat_m = re.fullmatch(rf"{re.escape(col)} = '(.*)'", clause)
            ex_m = re.fullmatch(r"EXTRACT\((\w+) FROM (\w+)\) = (\d+)", clause)
            between_m = re.fullmatch(
                rf"{re.escape(col)} BETWEEN '([^']+)' AND '([^']+)'", clause)
            if cat_m:
                distinct = _run_sql(f"SELECT DISTINCT {col} AS V FROM {table} "
                                    f"WHERE {clause}")["V"].tolist()
                restrict_ok = len(distinct) <= 1
                detail = f"distinct {col} under the filter = {distinct}"
            elif ex_m:
                part, expr_col, target = ex_m.group(1), ex_m.group(2), ex_m.group(3)
                bounds = _run_sql(
                    f"SELECT MIN(EXTRACT({part} FROM {expr_col})) AS LO, "
                    f"MAX(EXTRACT({part} FROM {expr_col})) AS HI "
                    f"FROM {table} WHERE {clause}")
                lo, hi = bounds["LO"].iloc[0], bounds["HI"].iloc[0]
                restrict_ok = (lo == hi == int(target)) or filtered == 0
                detail = f"EXTRACT({part} FROM {expr_col}) under the filter ranges {lo}-{hi} (target {target})"
            elif between_m:
                lo_v, hi_v = between_m.group(1), between_m.group(2)
                viol = int(_run_sql(
                    f"SELECT COUNT(*) AS N FROM {table} WHERE {clause} "
                    f"AND ({col} < '{lo_v}' OR {col} > '{hi_v}')")["N"].iloc[0])
                restrict_ok = viol == 0
                detail = f"{viol} row(s) inside the filter but outside [{lo_v}, {hi_v}]"
            else:
                # An unrecognized clause shape -- engine.build_where() only
                # ever emits exactly these three shapes (categorical, date-
                # part, date-range); anything else FAILS closed rather than
                # getting a free pass, per this project's own "no proof, no
                # pass" rule -- a REAL gap found building this: the first
                # version silently PASSED anything it didn't recognize,
                # which let a deliberately malformed clause slip through
                # unverified in its own test.
                restrict_ok = False
                detail = ("clause shape doesn't match any known build_where() pattern -- "
                         "failing closed rather than passing unverified")
            ok = in_range and restrict_ok
            rows.append({
                "interaction": f"Filter: {cap}",
                "tableau": "Quick filter restricts dependent sheets to the selected value.",
                "streamlit": (f"build_where() clause `{clause}` -> {filtered}/{total} row(s) "
                              f"kept; {detail}."),
                "proof": "App-side proof only -- live queries executed against the real "
                        "table (count + a shape-appropriate independent restriction check); "
                        "no live Tableau click observed.",
                "status": "PASS" if ok else "FAIL"})
        except Exception as e:
            rows.append({
                "interaction": f"Filter: {cap}",
                "tableau": "Quick filter restricts dependent sheets to the selected value.",
                "streamlit": f"Verification query failed -- {type(e).__name__}: {e}",
                "proof": "App-side proof only -- no live Tableau click observed.",
                "status": "FAIL"})

    for s in dashboard.get("sheets", []):
        declared = s.get("tooltip_fields")
        if not declared:
            continue
        chart, reason = HR.capture_sheet_chart(s)
        if chart is None:
            rows.append({
                "interaction": f"Tooltip: {s['name']}",
                "tableau": f"Declared tooltip field(s): {', '.join(declared)}.",
                "streamlit": f"Could not inspect this sheet's chart -- {reason}.",
                "proof": "App-side proof only -- no live Tableau click observed.",
                "status": "NOT VALIDATED"})
            continue
        shown = HR.extract_tooltip_titles(chart)
        missing = [f for f in declared if f not in shown]
        rows.append({
            "interaction": f"Tooltip: {s['name']}",
            "tableau": f"Declared tooltip field(s): {', '.join(declared)}.",
            "streamlit": (f"Chart tooltip label(s) actually rendered: {', '.join(shown) or '(none)'}."
                          + (f" Missing Tableau caption(s): {', '.join(missing)}." if missing else "")),
            "proof": "App-side proof only -- the REAL captured Altair chart's tooltip "
                    "encoding was inspected, not guessed from shelf pills.",
            "status": "PASS" if not missing else "WARNING"})
    return rows


def _compute_section_data(ir, session, dashboard, all_metrics, raw, value_lookup=None,
                          interaction_proof=False):
    """Everything DETERMINISTIC and REAL about one dashboard section: its
    resolvable measures, one live combined query, real result rows, the
    deterministic formula-match verdict per measure, and (when value_lookup
    is given) the REAL three-way App/Source/Tableau values for those same
    measures, reusing check_workbook's already-computed numbers rather than
    a second computation. Shared by build_dashboard_section_report (one
    generic Cortex line per section) and build_cortex_dashboard_validation_
    report (one rich, skill-driven Cortex call per section) so the two paths
    can never compute different numbers for the same workbook -- exactly the
    "two paths disagreeing" bug class this project keeps a standing rule
    against.
    Returns a dict; {'title':..., 'skipped': reason} if nothing resolves."""
    title = dashboard.get("title") or dashboard["name"]
    info = collect_dashboard_section(ir, dashboard)
    if info is None or not info["measure_caps"]:
        return {"title": title, "skipped": "no measure pills resolve to a real Snowflake table"}
    resolved, col_labels = {}, {}
    for cap in sorted(info["measure_caps"]):
        agg, label = info["measure_caps"][cap]
        r = _resolve_measure_sql(ir, cap, agg, info["table_cols"], all_metrics)
        if r:
            resolved[cap] = r
            if label:
                col_labels[cap] = label
    if not resolved:
        return {"title": title,
               "skipped": "measures found, but none resolve to this table's real columns"}

    select_cols = []
    if info["group_dim"]:
        select_cols.append(f"{info['group_dim'][1]} AS GRP")
    for cap, (sql_expr, kind, ref) in resolved.items():
        select_cols.append(f"{sql_expr} AS {to_phys(cap)}")
    group_by = " GROUP BY 1" if info["group_dim"] else ""
    combined_sql = f"SELECT {', '.join(select_cols)} FROM {info['table']}{group_by}"
    columns = ([info["group_dim"][0]] if info["group_dim"] else []) + list(resolved.keys())
    try:
        result_rows = session.sql(combined_sql).collect()
        data_rows = []
        for r in result_rows:
            row, idx = {}, 0
            if info["group_dim"]:
                row[info["group_dim"][0]] = r[0]
                idx = 1
            for j, cap in enumerate(resolved.keys()):
                row[cap] = r[idx + j]
            data_rows.append(row)
        query_error = None
    except Exception as e:
        data_rows, query_error = [], f"{type(e).__name__}: {e}"

    formula_rows = []
    value_rows = []
    lut = value_lookup or {}
    for cap, (sql_expr, kind, ref) in resolved.items():
        twb = raw.get(cap, f"raw column ({ref})" if kind == "column"
                      else "— (formula not found in .twb)")
        match, impact = _formula_match(twb, sql_expr, kind)
        formula_rows.append({"metric": cap, "twb": twb, "app_sql": sql_expr,
                             "match": match, "impact": impact})
        # the REAL three-way value comparison (App / Backend-Source /
        # Tableau) -- looked up from check_workbook's already-computed
        # numbers, keyed by caption first (raw columns) then by the calc's
        # internal name (ref) as a fallback, since a calc's caption can
        # differ from the name check_workbook indexed it under.
        vrow = lut.get((info["datasource"], cap)) or lut.get((info["datasource"], ref))
        if vrow:
            value_rows.append({"metric": cap, **vrow})

    interaction_rows = (compute_interaction_proof(dashboard, info["table"], info["table_cols"])
                        if interaction_proof else [])

    return {"title": title, "table": info["table"], "sql": combined_sql,
           "columns": columns, "column_labels": col_labels, "rows": data_rows,
           "query_error": query_error, "formula_rows": formula_rows,
           "value_rows": value_rows, "interaction_rows": interaction_rows}


def build_dashboard_section_report(ir, session, model="claude-sonnet-4-5"):
    """LIVE, per-DASHBOARD-section validation. For every dashboard: one real
    combined SQL query computes every measure the dashboard's sheets actually
    use (grouped by a detected dimension when one resolves against the real
    table), each measure's Tableau TWB formula is deterministically compared
    to the generated Streamlit SQL, and ONE Cortex call narrates the whole
    section's findings -- not one sentence per metric. A dashboard with no
    resolvable measures is reported as skipped, with a reason, never silently
    omitted.

    Returns (sections, bug_rollup) -- plain data; dashboard_report_to_notebook
    / dashboard_report_to_html render it."""
    raw = _raw_formulas(ir.get("source_file", ""))
    all_metrics, _ = CS.build_metrics(ir)
    sections, bug_rollup = [], []

    for d in ir.get("dashboards", []):
        sec = _compute_section_data(ir, session, d, all_metrics, raw)
        if sec.get("skipped"):
            sections.append(sec)
            continue
        for row in sec["formula_rows"]:
            if not row["match"]:
                bug_rollup.append({"section": sec["title"], **row})
        cortex_text, cortex_tokens, cortex_err = _narrate_section_summary(
            session, sec["title"], sec["columns"], sec["rows"],
            sec["formula_rows"], model=model)
        sec.update({"cortex_summary": cortex_text, "cortex_tokens": cortex_tokens,
                   "cortex_error": cortex_err})
        sections.append(sec)

    return sections, bug_rollup


# =============================================================================
# SKILL-DRIVEN validation -- applies the dashboard-validation skill's exact
# methodology (comparison table + diagnostic-on-anomaly + confirmed-bug-vs-
# intentional-difference categorization + explicit verdict, every section)
# by handing Cortex the REAL, already-executed section data and asking IT to
# write the investigative content, instead of a rigid Python template trying
# to reproduce that judgment deterministically. ARCHITECTURE RULE, unchanged
# and load-bearing here more than anywhere else in this file: every number
# Cortex is given was ALREADY computed by _compute_section_data (a real live
# query, a real deterministic formula-match verdict); Cortex explains,
# categorizes and writes prose -- it is never the source of a number and
# never allowed to overturn a match/mismatch already decided.
#
# NOTE ON SCHEMA: the skill's own SQL template (region/TIME_FLAG/YEAR_FLAG
# TY-vs-LY/PLAN_LE Vs-LE pivot) is written for the WBR retail mart
# (ENTERPRISE_PRD_DB...MRT_SALES_BY_COUNTRY_CHANNEL); a migrated Tableau
# workbook's real table has none of those columns. Per the skill's own rule
# ("confirm real schema, never invent it"), this applies the skill's
# STRUCTURE (source audit, comparison table, diagnostics, categorized bug
# summary, explicit verdicts) to whatever schema the workbook actually has,
# not the WBR-specific column names. Testing Plan alignment and the Tooltip
# Completeness Audit reference documents (TESTING_PLAN.md, tooltip metadata)
# that don't exist for a Tableau-sourced migration -- reported as honestly
# not-applicable rather than fabricated.
# =============================================================================

def _rows_md_table(columns, column_labels, rows, limit=15):
    """The REAL live query result as an actual markdown table -- built
    directly from the executed rows, never from Cortex's own retelling of
    them. A user reading the rendered report must see the real numbers in a
    real table, not prose that merely CITES a couple of them as examples."""
    if not rows:
        return "_(no rows returned)_"
    labels = column_labels or {}
    heads = [labels.get(c, c) for c in columns]
    lines = ["| " + " | ".join(heads) + " |",
             "|" + "|".join(["---"] * len(heads)) + "|"]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(str(r.get(c)) for c in columns) + " |")
    if len(rows) > limit:
        lines.append(f"\n_... {len(rows) - limit} more row(s) not shown "
                     f"(showing {limit} of {len(rows)} total)._")
    return "\n".join(lines)


def _formula_md_table(formula_rows):
    """The Metric | Tableau TWB Formula | Streamlit App SQL | Match? | Impact
    comparison table the skill mandates -- built directly from _formula_
    match's ALREADY-DECIDED verdicts, never left to Cortex to reproduce in
    its own markdown (an LLM formatting a table is one more place a real
    verdict could get garbled between decision and display)."""
    if not formula_rows:
        return "_(no measures resolved against this table)_"
    lines = ["| Metric | Tableau TWB Formula | Streamlit App SQL | Match? | Impact |",
             "|---|---|---|---|---|"]
    for r in formula_rows:
        match = "✅ Match" if r["match"] else "❌ BUG"
        impact = r["impact"] or "—"
        lines.append(f"| **{r['metric']}** | `{r['twb']}` | `{r['app_sql']}` "
                    f"| {match} | {impact} |")
    return "\n".join(lines)


def _fmt_val(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _value_md_table(value_rows):
    """THE THREE-WAY DATA comparison: Streamlit App | Backend/Source |
    Tableau, side by side with the SAME real numbers and verdict
    check_workbook already computed (never a second computation) -- this is
    the actual DATA match, distinct from _formula_md_table's FORMULA-SHAPE
    match above it. A metric can have matching formula shapes and still
    diverge in value (a wrong column bound, a bad load) -- and the inverse:
    two differently-shaped formulas can agree numerically by coincidence.
    Both tables are shown because neither one alone proves the other."""
    if not value_rows:
        return ("_(no independent App/Source/Tableau values available for "
               "this section's measures)_")
    lines = ["| Metric | Streamlit App | Backend/Source | Tableau | Status |",
             "|---|---|---|---|---|"]
    _icon = {"PASS": "✅ MATCH", "BUG": "❌ MISMATCH", "EXECUTED": "☑ no independent check"}
    for r in value_rows:
        src = _fmt_val(r.get("source"))
        if r.get("source_kind") == "table-repull":
            src += " (repull)"
        tab = _fmt_val(r.get("tableau"))
        if r.get("tableau_note"):
            tab += f" _{r['tableau_note']}_"
        status = _icon.get(r.get("verdict"), r.get("verdict") or "—")
        lines.append(f"| **{r['metric']}** | {_fmt_val(r.get('app'))} | {src} "
                    f"| {tab} | {status} |")
    return "\n".join(lines)


def _interaction_md_table(interaction_rows):
    """R11 -- Interaction Proof table (Filter/Tooltip), built directly from
    compute_interaction_proof's already-decided rows. Every row states
    plainly this is app-side proof only -- no live Tableau click observed."""
    if not interaction_rows:
        return ("_Not validated -- no automated interaction/filter test "
               "harness ran for this section. Formula and data proof above "
               "are real; interaction behavior is not yet automatically "
               "verified and should not be read as passing._")
    _icon = {"PASS": "✅ PASS", "WARNING": "⚠️ WARNING", "FAIL": "❌ FAIL",
             "NOT VALIDATED": "⚪ NOT VALIDATED"}
    lines = ["| Interaction | Tableau Behavior | Streamlit Behavior | Proof | Status |",
             "|---|---|---|---|---|"]
    for r in interaction_rows:
        lines.append(f"| **{r['interaction']}** | {r['tableau']} | {r['streamlit']} "
                    f"| {r['proof']} | {_icon.get(r['status'], r['status'])} |")
    return "\n".join(lines)


def cortex_generate_section_report(session, sec, model="claude-sonnet-4-5"):
    """ONE Cortex call per section, narrowed to what Cortex actually adds:
    a diagnostic investigation IF (and only if) the real data shows a
    genuine anomaly, and a closing verdict categorized as CONFIRMED BUG or
    INTENTIONAL DIFFERENCE. The comparison table itself is NOT asked of
    Cortex anymore -- it is built deterministically by _formula_md_table /
    _rows_md_table and rendered separately, because an LLM re-typing a table
    that was already decided is one more place a real MATCH/MISMATCH could
    get garbled between decision and display; a user reading the report
    should see the real table, not Cortex's paraphrase of it.
    Soft-fails: returns (None, 0, reason) rather than raising.
    Returns (markdown_text_or_None, estimated_tokens, error_or_None)."""
    if sec.get("query_error"):
        data_txt = f"QUERY FAILED: {sec['query_error']}"
    else:
        labels = sec.get("column_labels", {})
        heads = [labels.get(c, c) for c in sec["columns"]]
        lines = [" | ".join(heads)] + [
            " | ".join(str(r.get(c)) for c in sec["columns"])
            for r in sec["rows"][:15]
        ]
        data_txt = "\n".join(lines)
        if len(sec["rows"]) > 15:
            data_txt += f"\n... ({len(sec['rows']) - 15} more row(s))"
    measure_txt = "\n".join(
        f"- {r['metric']}: Tableau=`{r['twb']}`  App=`{r['app_sql']}`  "
        f"Verdict={'MATCH' if r['match'] else 'MISMATCH'}"
        + (f"  Reason={r['impact']}" if r["impact"] else "")
        for r in sec["formula_rows"]
    )
    prompt = (
        "You are validating one section of a migrated Tableau-to-Snowflake "
        "dashboard. The comparison table has ALREADY been rendered "
        "separately -- do not write one. Your job is only:\n"
        "1. ONLY IF the real data below shows something genuinely anomalous "
        "(a value blank or zero everywhere it shouldn't be, an impossible "
        "value, or a formula mismatch with a real quantified impact you can "
        "compute from the rows given), write a short Diagnostic: Finding, "
        "Root Cause, Fix.\n"
        "2. End with exactly one verdict sentence. If there is a real issue, "
        "categorize it as EXACTLY one of: 'Confirmed Bug' (the formulas "
        "genuinely disagree and would produce a wrong number) or "
        "'Intentional Difference' (a legitimate, defensible difference, not "
        "a defect). If everything matches, say 'No bugs found.'\n\n"
        "HARD RULES: use ONLY the real data given below -- never invent a "
        "number, a row, or a value not shown here. The MATCH/MISMATCH "
        "verdicts listed are ALREADY DECIDED by a separate deterministic "
        "check and must not be changed -- explain and categorize them, do "
        "not re-judge them. Do not repeat the comparison table.\n\n"
        f"SECTION: {sec['title']}\n\n"
        f"REAL LIVE QUERY RESULT ({len(sec.get('rows', []))} row(s) total, "
        f"showing up to 15):\n{data_txt}\n\n"
        f"MEASURES (Tableau formula vs generated app SQL, verdict already "
        f"decided):\n{measure_txt}\n"
    )
    try:
        r = session.sql(f"SELECT SNOWFLAKE.CORTEX.COMPLETE({model!r}, "
                        f"{_sql_lit(prompt)}) AS R").collect()
        text = str(r[0][0]).strip() if r and r[0][0] is not None else ""
        tokens = (len(prompt) + len(text)) // 4
        return (text or None), tokens, (None if text else "empty response")
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"


def _cortex_bug_rollup(session, book_name, confirmed, ok_titles,
                       model="claude-sonnet-4-5"):
    """ONE Cortex call synthesizing every section's ALREADY-DECIDED verdicts
    into the skill's mandatory closing structure ("Summary of All Bugs &
    Potential Fixes"). Never re-litigates a verdict -- only organizes and
    writes up what _formula_match already found. Soft-fails."""
    bug_txt = "\n".join(
        f"- {title}: " + "; ".join(
            f"{r['metric']} (Tableau=`{r['twb']}`, App=`{r['app_sql']}`, {r['impact']})"
            for r in rows)
        for title, rows in confirmed
    ) or "(none)"
    ok_txt = ", ".join(ok_titles) or "(none)"
    prompt = (
        f"Write a 'Summary of All Bugs & Potential Fixes' closing section "
        f"for a dashboard migration validation report, workbook "
        f"'{book_name}'. Confirmed formula mismatches, by section:\n{bug_txt}\n"
        f"\nSections with no issues found: {ok_txt}\n\n"
        "Produce a short markdown table (# | Section | Issue | Fix) listing "
        "ONLY the mismatches given above, then one closing sentence "
        "confirming the clean sections. Do not invent any issue not listed.")
    try:
        r = session.sql(f"SELECT SNOWFLAKE.CORTEX.COMPLETE({model!r}, "
                        f"{_sql_lit(prompt)}) AS R").collect()
        text = str(r[0][0]).strip() if r and r[0][0] is not None else ""
        tokens = (len(prompt) + len(text)) // 4
        return (text or None), tokens, (None if text else "empty response")
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"


def build_cortex_dashboard_validation_report(ir, session, book_name,
                                              model="claude-sonnet-4-5", res=None,
                                              interaction_proof=True, narrate=True):
    """The dashboard-validation skill's methodology, applied to THIS
    workbook's real schema. For every dashboard section: real live query,
    real deterministic formula-match verdicts (_compute_section_data --
    shared with build_dashboard_section_report, so the two never disagree
    on a number), the REAL three-way App/Backend-Source/Tableau VALUE
    comparison (reused from check_workbook via `res` -- the SAME numbers
    Stage 5's own measure-parity table already shows, never a second
    computation), then ONE rich Cortex call writes the diagnostic-if-
    warranted + categorized verdict (cortex_generate_section_report; the
    comparison tables themselves are rendered deterministically, not
    authored by Cortex). A final Cortex call rolls every section's already-
    decided verdicts into the skill's mandatory closing "Summary of All Bugs
    & Potential Fixes".

    `res` = check_workbook(ir)'s result (ideally after apply_live_truth_to_
    measures has folded in real REST/VDS Tableau values) -- when omitted,
    sections still render with the formula-shape comparison and the live
    query result, just without the three-way value table (no independent
    App/Source/Tableau numbers to show).

    `interaction_proof` (R11, default True): also runs compute_interaction_
    proof per section -- real FILTER and TOOLTIP checks, app-side only (see
    that function's docstring for the honest scope statement). Set False to
    skip it (e.g. a quick re-run where only the value/formula tables changed).

    `narrate` (default True): when False, skips BOTH Cortex calls
    (cortex_generate_section_report per section + the closing bug rollup) --
    `sections` still carries every DETERMINISTIC field (`formula_rows`,
    `interaction_rows`, the live query result) untouched, just with
    `cortex_report`/`cortex_tokens`/`cortex_error` left unset and the rollup
    text/tokens as None/0. Added so a caller that only needs the
    deterministic per-section data (e.g. the R12 proof-first validation
    pack's formula/interaction evidence) never spends AI tokens or needs a
    Cortex-capable session for something it doesn't display.

    Testing Plan alignment and the Tooltip Completeness Audit are reported
    as honestly not-applicable (no TESTING_PLAN.md or tooltip metadata
    exists for a Tableau-sourced migration) rather than fabricated -- the
    skill's own rule is to confirm schema/inputs, never invent them.

    Returns (sections, rollup_text, rollup_tokens)."""
    raw = _raw_formulas(ir.get("source_file", ""))
    all_metrics, _ = CS.build_metrics(ir)
    value_lookup = _value_lookup_from_check_result(res) if res else {}
    sections = []

    for d in ir.get("dashboards", []):
        sec = _compute_section_data(ir, session, d, all_metrics, raw, value_lookup,
                                    interaction_proof=interaction_proof)
        if not sec.get("skipped") and narrate:
            text, tokens, err = cortex_generate_section_report(session, sec, model=model)
            sec["cortex_report"] = text
            sec["cortex_tokens"] = tokens
            sec["cortex_error"] = err
        sections.append(sec)

    if not narrate:
        return sections, {"text": None, "tokens": 0, "error": None}

    confirmed, ok_titles = [], []
    for sec in sections:
        if sec.get("skipped"):
            continue
        bugs = [r for r in sec.get("formula_rows", []) if not r["match"]]
        if bugs:
            confirmed.append((sec["title"], bugs))
        else:
            ok_titles.append(sec["title"])
    rollup_text, rollup_tokens, rollup_err = _cortex_bug_rollup(
        session, book_name, confirmed, ok_titles, model=model)

    return sections, {"text": rollup_text, "tokens": rollup_tokens, "error": rollup_err}


def dashboard_validation_report_to_notebook(sections, rollup, book_name):
    """Render build_cortex_dashboard_validation_report's ALREADY-EXECUTED
    results as an nbformat notebook, in the skill's mandated section order:
    header -> source-file note -> per-section Cortex-authored content ->
    bug rollup -> completion checklist -> honest Testing-Plan/Tooltip
    not-applicable notes."""
    today = _dt.date.today().isoformat()
    cells = [_md(
        f"# {book_name} — Dashboard Validation\n\n"
        "**Method:** dashboard-validation skill methodology, applied to "
        "this workbook's real schema. Each section below is ONE dashboard: "
        "a real combined live query, a deterministic Tableau-vs-generated-"
        "SQL formula check per measure, then Cortex writes the comparison "
        "table, any diagnostic the real data warrants, and a categorized "
        "verdict (Confirmed Bug / Intentional Difference / No bugs found). "
        "Cortex explains and categorizes what was already decided -- it "
        "never invents a number or overturns a verdict.\n\n"
        f"**Date:** {today}")]
    cells.append(_md(
        "## 0. Source File Audit\n\n"
        "This migration's generated app (`app_<workbook>.py`) embeds the "
        "parsed workbook as one data structure and calls a single shared "
        "rendering engine (`engine.py`) -- there is no per-metric line of "
        "hand-written code to cite the way a hand-authored dashboard file "
        "has. The honest equivalent citation for a formula is the exact SQL "
        "expression shown in each section's comparison table below; if a "
        "formula is genuinely wrong, the fix lives in the shared translator "
        "(`calc_translator.py` / `engine.py`), not in a per-workbook file."))
    n = 1
    for sec in sections:
        cells.append(_md(f"## {n}. {sec['title']}"))
        if sec.get("skipped"):
            cells.append(_md(f"*Skipped — {sec['skipped']}*"))
            n += 1
            continue
        cells.append(_code(f"-- live combined query -- already run against "
                          f"the real account\n{sec['sql']};"))
        # Real query result and real formula comparison, as ACTUAL tables --
        # built directly from the executed rows / the already-decided
        # verdicts, never from Cortex's retelling of them.
        if sec.get("query_error"):
            cells.append(_md(f"**Query failed:** `{sec['query_error']}`"))
        else:
            cells.append(_md(
                f"### {n}.1 Live query result "
                f"({min(len(sec.get('rows', [])), 15)} of "
                f"{len(sec.get('rows', []))} row(s) shown)\n\n"
                + _rows_md_table(sec["columns"], sec.get("column_labels"), sec["rows"])))
        cells.append(_md(f"### {n}.2 TWB vs Streamlit — Formula Comparison\n\n"
                         + _formula_md_table(sec["formula_rows"])))
        cells.append(_md(
            f"### {n}.3 App vs Backend vs Tableau — Data Comparison\n\n"
            + _value_md_table(sec.get("value_rows"))))
        cells.append(_md(
            f"### {n}.4 Interaction Proof (Filter / Tooltip, app-side)\n\n"
            + _interaction_md_table(sec.get("interaction_rows"))))
        if sec.get("cortex_report"):
            cells.append(_md(f"### {n}.5 Diagnostic & verdict (Cortex)\n\n"
                             + sec["cortex_report"]))
        else:
            cells.append(_md(f"*Cortex diagnostic unavailable — {sec.get('cortex_error')}*"))
        n += 1
    cells.append(_md(f"## {n}. Summary of All Bugs & Potential Fixes"))
    cells.append(_md(rollup.get("text") or f"*Rollup unavailable — {rollup.get('error')}*"))
    n += 1
    checklist = "\n".join(
        # A skipped section is neither a pass nor a failure -- marking it ✅
        # (a real bug found reading this report live) reads as a false
        # clean bill of health next to text that says "not resolved."
        f"- {'⚠️' if sec.get('skipped') else '✅'} {sec['title']}: "
        + (sec["skipped"] if sec.get("skipped")
           else ("no bugs found" if all(r["match"] for r in sec.get("formula_rows", []))
                 else f"{sum(1 for r in sec['formula_rows'] if not r['match'])} issue(s) found"))
        for sec in sections)
    cells.append(_md(f"## ✅ {book_name} Validation Complete\n\n{checklist}"))
    cells.append(_md(
        f"## {n + 1}. Alignment with Testing Plan\n\n"
        "Not applicable — no `TESTING_PLAN.md` (or equivalent test-ID "
        "document) exists for this Tableau-sourced migration. Per the "
        "skill's own rule (confirm inputs, never invent them), this is "
        "reported honestly rather than fabricating test IDs."))
    cells.append(_md(
        f"## {n + 2}. Tooltip Completeness Audit\n\n"
        "Not applicable — this accelerator does not model per-metric "
        "tooltip metadata (Definition / Nuances / Data Source / "
        "Limitations / Golden Report Reference) for a Tableau-sourced "
        "workbook. Reported honestly rather than fabricated."))
    nb = {"cells": cells, "metadata": {"language_info": {"name": "python"},
          "kernelspec": {"name": "python3", "display_name": "Python 3"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1)


def dashboard_validation_report_to_html(sections, rollup, book_name):
    """Same already-executed data as dashboard_validation_report_to_notebook,
    rendered as a standalone HTML report. The query-result table and the
    formula-comparison table are REAL <table> elements built directly from
    the executed rows / the already-decided verdicts -- not Cortex's prose
    ABOUT them. Cortex's own text is rendered ONLY for the diagnostic-if-
    warranted + verdict, its actual job now (cortex_generate_section_report
    is no longer asked to write the comparison table at all)."""
    import html as _html
    CY = "#00d4d4"

    def esc(s):
        return _html.escape(str(s), quote=True)

    def rows_html_table(columns, column_labels, rows, limit=15):
        if not rows:
            return '<p class="skip-note">(no rows returned)</p>'
        labels = column_labels or {}
        heads = "".join(f"<th>{esc(labels.get(c, c))}</th>" for c in columns)
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{esc(r.get(c))}</td>" for c in columns) + "</tr>"
            for r in rows[:limit])
        note = (f'<p class="skip-note">showing {min(len(rows), limit)} of '
               f'{len(rows)} row(s)</p>' if len(rows) > limit else "")
        return (f'<table class="data-tbl"><thead><tr>{heads}</tr></thead>'
               f'<tbody>{body_rows}</tbody></table>{note}')

    def formula_html_table(formula_rows):
        if not formula_rows:
            return '<p class="skip-note">(no measures resolved)</p>'
        rows_html = "".join(
            f"<tr class=\"{'row-bug' if not r['match'] else ''}\">"
            f"<td><b>{esc(r['metric'])}</b></td>"
            f"<td><code>{esc(r['twb'])}</code></td>"
            f"<td><code>{esc(r['app_sql'])}</code></td>"
            f"<td>{'✅ Match' if r['match'] else '❌ BUG'}</td>"
            f"<td>{esc(r['impact'] or '—')}</td></tr>"
            for r in formula_rows)
        return (f'<table class="cmp-tbl"><thead><tr><th>Metric</th>'
               f"<th>Tableau TWB Formula</th><th>Streamlit App SQL</th>"
               f"<th>Match?</th><th>Impact</th></tr></thead>"
               f"<tbody>{rows_html}</tbody></table>")

    def value_html_table(value_rows):
        if not value_rows:
            return ('<p class="skip-note">no independent App/Source/Tableau '
                   "values available for this section's measures</p>")
        _icon = {"PASS": "✅ MATCH", "BUG": "❌ MISMATCH",
                "EXECUTED": "☑ no independent check"}
        rows_html = []
        for r in value_rows:
            src = _fmt_val(r.get("source"))
            if r.get("source_kind") == "table-repull":
                src += " (repull)"
            tab = _fmt_val(r.get("tableau"))
            note = f' <i>{esc(r["tableau_note"])}</i>' if r.get("tableau_note") else ""
            status = _icon.get(r.get("verdict"), esc(r.get("verdict") or "—"))
            rows_html.append(
                f"<tr class=\"{'row-bug' if r.get('verdict') == 'BUG' else ''}\">"
                f"<td><b>{esc(r['metric'])}</b></td>"
                f"<td>{esc(_fmt_val(r.get('app')))}</td>"
                f"<td>{esc(src)}</td>"
                f"<td>{esc(tab)}{note}</td>"
                f"<td>{status}</td></tr>")
        return (f'<table class="cmp-tbl"><thead><tr><th>Metric</th>'
               f"<th>Streamlit App</th><th>Backend/Source</th>"
               f"<th>Tableau</th><th>Status</th></tr></thead>"
               f"<tbody>{''.join(rows_html)}</tbody></table>")

    def interaction_html_table(interaction_rows):
        if not interaction_rows:
            return ('<p class="skip-note">Not validated -- no automated interaction/filter '
                   "test harness ran for this section.</p>")
        _icon = {"PASS": "✅ PASS", "WARNING": "⚠️ WARNING", "FAIL": "❌ FAIL",
                "NOT VALIDATED": "⚪ NOT VALIDATED"}
        rows_html = "".join(
            f"<tr class=\"{'row-bug' if r['status']=='FAIL' else ''}\">"
            f"<td><b>{esc(r['interaction'])}</b></td>"
            f"<td>{esc(r['tableau'])}</td>"
            f"<td>{esc(r['streamlit'])}</td>"
            f"<td>{esc(r['proof'])}</td>"
            f"<td>{_icon.get(r['status'], esc(r['status']))}</td></tr>"
            for r in interaction_rows)
        return (f'<table class="cmp-tbl"><thead><tr><th>Interaction</th>'
               f"<th>Tableau Behavior</th><th>Streamlit Behavior</th>"
               f"<th>Proof</th><th>Status</th></tr></thead>"
               f"<tbody>{rows_html}</tbody></table>")

    body = []
    for i, sec in enumerate(sections, 1):
        if sec.get("skipped"):
            body.append(f'<section class="sec skip"><h2>{i}. {esc(sec["title"])}</h2>'
                        f'<p class="skip-note">Skipped — {esc(sec["skipped"])}</p></section>')
            continue
        report = esc(sec.get("cortex_report") or f"Cortex diagnostic unavailable — {sec.get('cortex_error')}")
        data_block = (f'<p class="skip-note">Query failed: {esc(sec["query_error"])}</p>'
                     if sec.get("query_error")
                     else rows_html_table(sec["columns"], sec.get("column_labels"), sec["rows"]))
        body.append(f"""
        <section class="sec">
          <h2>{i}. {esc(sec['title'])}</h2>
          <h4>Live combined query</h4>
          <pre class="sql">{esc(sec['sql'])}</pre>
          <h4>Live query result</h4>
          {data_block}
          <h4>TWB vs Streamlit — Formula Comparison</h4>
          {formula_html_table(sec["formula_rows"])}
          <h4>App vs Backend vs Tableau — Data Comparison</h4>
          {value_html_table(sec.get("value_rows"))}
          <h4>Interaction Proof (Filter / Tooltip, app-side)</h4>
          {interaction_html_table(sec.get("interaction_rows"))}
          <h4>Diagnostic &amp; verdict (Cortex)</h4>
          <pre class="cortex-md">{report}</pre>
        </section>""")
    rollup_txt = esc(rollup.get("text") or f"Rollup unavailable — {rollup.get('error')}")
    checklist = "".join(
        f"<li>{'⚠️' if sec.get('skipped') else '✅'} {esc(sec['title'])}: "
        + esc(sec["skipped"] if sec.get("skipped")
              else ("no bugs found" if all(r["match"] for r in sec.get("formula_rows", []))
                    else f"{sum(1 for r in sec['formula_rows'] if not r['match'])} issue(s) found"))
        + "</li>"
        for sec in sections)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(book_name)} — Dashboard Validation</title>
<style>
body{{background:#0a1428;color:#fff;font-family:'Segoe UI',sans-serif;max-width:1100px;
     margin:0 auto;padding:2rem 1.2rem;line-height:1.5}}
h1{{font-size:1.6rem}} h2{{font-size:1.2rem;color:{CY};margin-top:2rem}}
h4{{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:rgba(255,255,255,.55);
   margin:1rem 0 .4rem}}
section.sec{{background:#111d38;border:1px solid rgba(255,255,255,.08);border-radius:8px;
            padding:1rem 1.3rem;margin-bottom:1.2rem}}
section.skip{{opacity:.6}}
.skip-note{{color:rgba(255,255,255,.5);font-style:italic}}
pre.sql{{background:#0d1830;padding:.7rem;border-radius:6px;overflow-x:auto;font-size:.74rem;
        color:{CY};font-family:ui-monospace,Consolas,monospace}}
pre.cortex-md{{background:rgba(139,124,248,.06);border:1px solid rgba(139,124,248,.28);
              border-radius:6px;padding:.8rem 1rem;white-space:pre-wrap;font-family:'Segoe UI',sans-serif;
              font-size:.86rem;line-height:1.55}}
.rollup{{background:rgba(52,211,153,.06);border:1px solid rgba(52,211,153,.28);border-radius:8px;
        padding:1rem 1.3rem;white-space:pre-wrap;font-size:.86rem}}
.na{{opacity:.65;font-style:italic;font-size:.85rem}}
ul{{padding-left:1.2rem}}
table.data-tbl,table.cmp-tbl{{width:100%;border-collapse:collapse;font-size:.78rem;margin-bottom:.6rem;
     overflow-x:auto;display:block}}
table.data-tbl thead,table.cmp-tbl thead{{display:table;width:100%;table-layout:fixed}}
table.data-tbl tbody,table.cmp-tbl tbody{{display:table;width:100%;table-layout:fixed}}
table.data-tbl th,table.cmp-tbl th{{text-align:left;color:{CY};font-weight:600;
     border-bottom:1px solid rgba(255,255,255,.18);padding:.35rem .5rem}}
table.data-tbl td,table.cmp-tbl td{{padding:.32rem .5rem;border-bottom:1px solid rgba(255,255,255,.06)}}
table.cmp-tbl td code{{font-size:.74rem;color:rgba(255,255,255,.85)}}
table.cmp-tbl tr.row-bug{{background:rgba(255,90,90,.08)}}
</style></head><body>
<h1>{esc(book_name)} — Dashboard Validation</h1>
<p style="color:rgba(255,255,255,.6)">Dashboard-validation skill methodology. Every live query
result Cortex was shown is real; Cortex explains and categorizes, it never invents a number.</p>
{"".join(body)}
<h2>Summary of All Bugs &amp; Potential Fixes</h2>
<div class="rollup">{rollup_txt}</div>
<h2>✅ {esc(book_name)} Validation Complete</h2>
<ul>{checklist}</ul>
<h2>Alignment with Testing Plan</h2>
<p class="na">Not applicable — no TESTING_PLAN.md exists for this Tableau-sourced migration.</p>
<h2>Tooltip Completeness Audit</h2>
<p class="na">Not applicable — tooltip metadata is not modeled for a Tableau-sourced workbook.</p>
</body></html>"""


def _issues_register(sections):
    """Auto-derived issue list -- organizes what the ALREADY-DECIDED formula
    (_formula_match) and value (check_workbook) verdicts found, plus honest
    coverage gaps (skipped sections). Never a new judgment: a section here
    exists only because a verdict computed elsewhere already flagged it."""
    issues = []
    for sec in sections:
        if sec.get("skipped"):
            issues.append({
                "severity": "LOW", "location": sec["title"], "type": "Coverage",
                "issue": sec["skipped"], "proof": "Skipped before validation ran.",
                "fix": "Confirm the dashboard's dimension/measure pills resolve "
                       "to real table columns.", "owner": "Pending"})
            continue
        for r in sec.get("formula_rows", []):
            if not r["match"]:
                issues.append({
                    "severity": "MEDIUM", "location": f"{sec['title']} / {r['metric']}",
                    "type": "Formula", "issue": r["impact"] or "Formula shape differs.",
                    "proof": f"Tableau: {r['twb']}  |  App: {r['app_sql']}",
                    "fix": "Review the calc translation for this metric.",
                    "owner": "Pending"})
        for r in sec.get("value_rows", []):
            if r.get("verdict") == "BUG":
                other = r.get("source") if r.get("source") is not None else r.get("tableau")
                other_label = "backend/source" if r.get("source") is not None else "Tableau"
                issues.append({
                    "severity": "HIGH", "location": f"{sec['title']} / {r['metric']}",
                    "type": "Data",
                    "issue": f"App value {_fmt_val(r.get('app'))} disagrees with "
                            f"{other_label} value {_fmt_val(other)} beyond tolerance.",
                    "proof": "See Data Comparison table in the section below.",
                    "fix": "Investigate the SQL translation or source load for this measure.",
                    "owner": "Pending"})
    return issues


def build_migration_report_html(sections, rollup, book_name, run_id=None,
                                app_screenshots=None, backend_note=None):
    """A polished, executive-readable migration validation report: overall
    status banner, executive summary, a coverage/status-rules legend, a
    top-level Dashboard Validation Matrix, an auto-derived Issues Register,
    then per-dashboard Chart-Level Proof (Visual / Data / Formula proof,
    each with real numbers -- never a placeholder). Every status shown here
    was decided elsewhere (_formula_match, check_workbook's verdict) --
    this function only organizes and displays already-real numbers, per the
    project's standing "two paths must never disagree on one number" rule.

    `app_screenshots`: optional {dashboard_title: png_bytes} -- the app's
    OWN rendered chart image (headless_render.render_dashboard_to_png),
    embedded as real evidence when supplied. There is deliberately no
    fabricated placeholder image: a dashboard with no screenshot states
    that plainly instead of showing a fake box."""
    import base64
    import html as _html
    esc = lambda s: _html.escape(str(s), quote=True)
    app_screenshots = app_screenshots or {}
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = run_id or f"VAL-{_dt.datetime.now().strftime('%Y-%m-%d-%H%M')}"

    resolved = [s for s in sections if not s.get("skipped")]
    skipped = [s for s in sections if s.get("skipped")]
    formula_total = sum(len(s.get("formula_rows", [])) for s in resolved)
    formula_bugs = sum(1 for s in resolved for r in s["formula_rows"] if not r["match"])
    value_total = sum(len(s.get("value_rows", [])) for s in resolved)
    value_bugs = sum(1 for s in resolved for r in s.get("value_rows", []) if r.get("verdict") == "BUG")
    issues = _issues_register(sections)
    critical = sum(1 for i in issues if i["severity"] == "HIGH")
    warnings = sum(1 for i in issues if i["severity"] in ("MEDIUM", "LOW"))
    if critical:
        overall, status_class = "FAIL", "fail"
        reason = f"{critical} data mismatch(es) exceed tolerance -- do not sign off."
    elif warnings:
        overall, status_class = "PASS WITH WARNINGS", "warn"
        reason = (f"{formula_bugs} formula warning(s)"
                 + (f", {len(skipped)} section(s) not validated" if skipped else "") + ".")
    else:
        overall, status_class = "PASS", "pass"
        reason = "All formula and data checks passed within tolerance."

    def status_badge(status):
        cls = {"PASS": "pass", "WARNING": "warn", "FAIL": "fail",
              "NOT VALIDATED": "na", "INCOMPLETE": "na"}.get(status, "na")
        return f'<span class="badge {cls}">{esc(status)}</span>'

    def sec_status(sec):
        if sec.get("skipped"):
            return "INCOMPLETE"
        bugs = sum(1 for r in sec.get("formula_rows", []) if not r["match"])
        vbugs = sum(1 for r in sec.get("value_rows", []) if r.get("verdict") == "BUG")
        ifails = sum(1 for r in sec.get("interaction_rows", []) if r["status"] == "FAIL")
        iwarns = sum(1 for r in sec.get("interaction_rows", []) if r["status"] == "WARNING")
        if vbugs or ifails:
            return "FAIL"
        if bugs or iwarns:
            return "WARNING"
        return "PASS"

    def anchor(title):
        return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

    # --- Dashboard Validation Matrix (top-level, one row per section) -----
    matrix_rows = []
    for sec in sections:
        title = sec["title"]
        if sec.get("skipped"):
            matrix_rows.append(
                f'<tr><td><a href="#{anchor(title)}">{esc(title)}</a></td>'
                f'<td class="num">-</td><td class="num">Not run</td>'
                f'<td>Unsupported fields</td><td>Not run</td>'
                f'<td>Skip reason</td><td>{status_badge("INCOMPLETE")}</td></tr>')
            continue
        vbugs = sum(1 for r in sec.get("value_rows", []) if r.get("verdict") == "BUG")
        fbugs = sum(1 for r in sec.get("formula_rows", []) if not r["match"])
        n_val = len(sec.get("value_rows", []))
        data_proof = (f"{vbugs} mismatch(es) / {n_val} metric(s)" if n_val
                     else "no independent value check")
        formula_proof = "Exact or equivalent" if not fbugs else f"{fbugs} warning(s)"
        irows = sec.get("interaction_rows") or []
        ifails = sum(1 for r in irows if r["status"] == "FAIL")
        interactions = (f"{len(irows) - ifails} / {len(irows)} pass" if irows
                       else "Not run")
        matrix_rows.append(
            f'<tr><td><a href="#{anchor(title)}">{esc(title)}</a></td>'
            f'<td class="num">-</td><td class="num">{esc(data_proof)}</td>'
            f'<td>{esc(formula_proof)}</td><td>{esc(interactions)}</td>'
            f'<td>Live SQL, formula comparison, interaction proof, notebook</td>'
            f'<td>{status_badge(sec_status(sec))}</td></tr>')

    # --- Issues Register ---------------------------------------------------
    if issues:
        issue_rows = "".join(
            f'<tr><td><span class="badge {"fail" if i["severity"]=="HIGH" else "warn" if i["severity"]=="MEDIUM" else "na"}">'
            f'{esc(i["severity"])}</span></td>'
            f'<td>{esc(i["location"])}</td><td>{esc(i["type"])}</td>'
            f'<td>{esc(i["issue"])}</td><td>{esc(i["proof"])}</td>'
            f'<td>{esc(i["fix"])}</td><td>{esc(i["owner"])}</td></tr>'
            for i in issues)
    else:
        issue_rows = ('<tr><td colspan="7" style="text-align:center;color:var(--muted)">'
                     "No issues found.</td></tr>")

    # --- Chart-Level Proof (per dashboard) ---------------------------------
    articles = []
    for sec in sections:
        title = sec["title"]
        if sec.get("skipped"):
            articles.append(f"""
            <article id="{anchor(title)}" class="panel chart-card">
              <div class="chart-head">
                <div><h3>{esc(title)}</h3>
                <p>This section is listed separately so a skipped validation is visible
                and cannot be mistaken for a pass.</p></div>
                {status_badge("INCOMPLETE")}
              </div>
              <div class="chart-body">
                <div class="table-wrap"><table><thead><tr>
                  <th>Check</th><th>Result</th><th>Reason</th><th>Required Action</th>
                </tr></thead><tbody>
                  <tr><td>Data validation</td><td>{status_badge("NOT VALIDATED")}</td>
                  <td>{esc(sec["skipped"])}</td>
                  <td>Add field mapping so this dashboard's measures resolve to real columns.</td></tr>
                </tbody></table></div>
              </div>
            </article>""")
            continue

        fbugs = sum(1 for r in sec["formula_rows"] if not r["match"])
        vbugs = sum(1 for r in sec.get("value_rows", []) if r.get("verdict") == "BUG")
        n_val = len(sec.get("value_rows", []))

        # Visual proof -- REAL screenshot when supplied, honest "not
        # available" note otherwise. Never a fabricated placeholder image.
        shot = app_screenshots.get(title)
        if shot:
            b64 = base64.b64encode(shot).decode("ascii")
            visual_html = f"""
            <div class="subsection">
              <h3>A. Visual Proof</h3>
              <div class="proof-strip" style="grid-template-columns:1fr">
                <figure>
                  <img src="data:image/png;base64,{b64}" style="width:100%;border:1px solid var(--line);border-radius:8px" />
                  <figcaption>Streamlit app's own rendered chart (headless_render, this run).
                  No Tableau-side screenshot in this run -- pull the workbook via
                  Discover &amp; Scope -&gt; Pull from Tableau Server/Cloud to enable a
                  side-by-side Tableau capture.</figcaption>
                </figure>
              </div>
            </div>"""
        else:
            visual_html = """
            <div class="subsection">
              <h3>A. Visual Proof</h3>
              <p class="status-note">Not captured this run -- no rendered screenshot was
              supplied for this dashboard. This is stated honestly rather than shown as
              a placeholder image.</p>
            </div>"""

        # Data proof -- real App/Backend/Tableau values + a real numeric
        # diff against whichever independent reference exists.
        if sec.get("value_rows"):
            drows = []
            for r in sec["value_rows"]:
                ref = r.get("source") if r.get("source") is not None else r.get("tableau")
                diff = (f"{abs(r['app'] - ref):,.2f}"
                       if isinstance(r.get("app"), (int, float)) and isinstance(ref, (int, float))
                       else "—")
                status = {"PASS": "PASS", "BUG": "FAIL",
                         "EXECUTED": "NOT VALIDATED"}.get(r.get("verdict"), "NOT VALIDATED")
                drows.append(
                    f"<tr><td>{esc(r['metric'])}</td>"
                    f"<td class=\"num\">{esc(_fmt_val(r.get('app')))}</td>"
                    f"<td class=\"num\">{esc(_fmt_val(r.get('source')))}</td>"
                    f"<td class=\"num\">{esc(_fmt_val(r.get('tableau')))}</td>"
                    f"<td class=\"num\">{esc(diff)}</td>"
                    f"<td class=\"num\">±{TOL*100:.0f}% rel.</td>"
                    f"<td>{status_badge(status)}</td></tr>")
            data_html = f"""
            <div class="subsection">
              <h3>B. Data Proof</h3>
              <div class="table-wrap"><table><thead><tr>
                <th>Metric</th><th class="num">Streamlit App</th>
                <th class="num">Backend/Source</th><th class="num">Tableau</th>
                <th class="num">Diff</th><th class="num">Tolerance</th><th>Status</th>
              </tr></thead><tbody>{"".join(drows)}</tbody></table></div>
            </div>"""
        else:
            data_html = """
            <div class="subsection">
              <h3>B. Data Proof</h3>
              <p class="status-note">No independent App/Backend/Tableau value comparison
              available for this section's measures.</p>
            </div>"""

        # Formula proof -- real Tableau-formula-vs-app-SQL comparison
        frows = "".join(
            f"<tr><td>{esc(r['metric'])}</td><td><code>{esc(r['twb'])}</code></td>"
            f"<td><code>{esc(r['app_sql'])}</code></td>"
            f"<td>{'Semantic Equivalent' if r['match'] else 'Formula Mismatch'}</td>"
            f"<td>{esc(r['impact'] or 'None')}</td>"
            f"<td>{status_badge('PASS' if r['match'] else 'WARNING')}</td></tr>"
            for r in sec["formula_rows"])
        formula_html = f"""
        <div class="subsection">
          <h3>C. Formula Proof</h3>
          <div class="table-wrap"><table><thead><tr>
            <th>Metric</th><th>Tableau Formula</th><th>Streamlit SQL</th>
            <th>Category</th><th>Risk / Impact</th><th>Status</th>
          </tr></thead><tbody>{frows}</tbody></table></div>
        </div>"""

        irows = sec.get("interaction_rows") or []
        if irows:
            _iicon = {"PASS": "PASS", "WARNING": "WARNING", "FAIL": "FAIL",
                     "NOT VALIDATED": "NOT VALIDATED"}
            iproof_rows = "".join(
                f"<tr><td>{esc(r['interaction'])}</td><td>{esc(r['tableau'])}</td>"
                f"<td>{esc(r['streamlit'])}</td><td>{esc(r['proof'])}</td>"
                f"<td>{status_badge(_iicon.get(r['status'], r['status']))}</td></tr>"
                for r in irows)
            interaction_html = f"""
            <div class="subsection">
              <h3>D. Interaction Proof</h3>
              <p class="status-note">App-side proof only -- these rows drive the app's OWN
              filter/tooltip mechanics through their real code path and verify them with
              live queries or the real rendered chart spec. No live Tableau click was
              observed (no browser automation of Tableau exists in this project).</p>
              <div class="table-wrap"><table><thead><tr>
                <th>Interaction</th><th>Tableau Behavior</th><th>Streamlit Behavior</th>
                <th>Proof</th><th>Status</th>
              </tr></thead><tbody>{iproof_rows}</tbody></table></div>
            </div>"""
        else:
            interaction_html = """
            <div class="subsection">
              <h3>D. Interaction Proof</h3>
              <p class="status-note">Not validated -- this dashboard had no placed filter
              zone and no sheet with declared tooltip fields to check, or interaction
              proof was not run this pass.</p>
            </div>"""

        cortex_block = (f"""
        <details>
          <summary>Cortex diagnostic &amp; verdict</summary>
          <div class="details-body"><pre>{esc(sec.get('cortex_report') or sec.get('cortex_error') or '')}</pre></div>
        </details>""" if sec.get("cortex_report") or sec.get("cortex_error") else "")

        articles.append(f"""
        <article id="{anchor(title)}" class="panel chart-card">
          <div class="chart-head">
            <div><h3>{esc(title)}</h3>
            <p>Compares the Streamlit app's own SQL result, an independent
            backend/source recomputation, and Tableau (where available) at this
            dashboard's own grain.</p></div>
            {status_badge(sec_status(sec))}
          </div>
          <div class="chart-body">
            <div class="proof-summary">
              <div class="mini-proof"><strong>{n_val - vbugs} / {n_val}</strong><span>Data metrics matched</span></div>
              <div class="mini-proof"><strong>{len(sec['formula_rows']) - fbugs} / {len(sec['formula_rows'])}</strong><span>Formulas equivalent</span></div>
              <div class="mini-proof"><strong>{vbugs}</strong><span>Data mismatches</span></div>
              <div class="mini-proof"><strong>{fbugs}</strong><span>Formula warnings</span></div>
            </div>
            {visual_html}
            {data_html}
            {formula_html}
            {interaction_html}
            <details>
              <summary>View backend SQL</summary>
              <div class="details-body"><pre>{esc(sec['sql'])};</pre></div>
            </details>
            {cortex_block}
          </div>
        </article>""")

    rollup_txt = esc(rollup.get("text") or f"Rollup unavailable — {rollup.get('error')}")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{esc(book_name)} Migration Validation Report</title>
<style>
:root {{
  --bg:#f5f7fb; --panel:#fff; --panel-soft:#f9fafc; --text:#182033; --muted:#667085;
  --line:#d9dee8; --line-soft:#edf0f5; --pass:#0f766e; --pass-bg:#e7f6f3;
  --warn:#a15c07; --warn-bg:#fff3da; --fail:#b42318; --fail-bg:#fde7e4;
  --info:#31579d; --info-bg:#eaf0ff; --na:#565e6d; --na-bg:#eceff3;
  --shadow:0 10px 26px rgba(24,32,51,.07);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:"Segoe UI",Arial,sans-serif; line-height:1.5; }}
a {{ color:var(--info); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.page {{ max-width:1220px; margin:0 auto; padding:24px; }}
header {{ display:grid; grid-template-columns:1fr auto; gap:18px; align-items:start;
         padding:22px 0 18px; border-bottom:1px solid var(--line); }}
h1,h2,h3 {{ margin:0; line-height:1.2; }}
h1 {{ font-size:28px; }} h2 {{ font-size:20px; margin-top:32px; margin-bottom:12px; }}
h3 {{ font-size:16px; margin-bottom:10px; }}
.subtitle {{ margin:8px 0 0; color:var(--muted); font-size:14px; }}
.status-banner {{ min-width:250px; border:1px solid #f2d18f; background:var(--warn-bg);
                  border-radius:8px; padding:14px 16px; }}
.status-banner.pass {{ border-color:#9fd8cd; background:var(--pass-bg); }}
.status-banner.fail {{ border-color:#f3b4ac; background:var(--fail-bg); }}
.status-banner .label {{ color:var(--warn); font-size:12px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; }}
.status-banner.pass .label {{ color:var(--pass); }}
.status-banner.fail .label {{ color:var(--fail); }}
.status-banner .value {{ display:block; margin-top:4px; font-size:22px; font-weight:750; color:var(--warn); }}
.status-banner.pass .value {{ color:var(--pass); }}
.status-banner.fail .value {{ color:var(--fail); }}
.status-banner .reason {{ margin-top:6px; font-size:13px; color:#6c4307; }}
nav {{ display:flex; gap:8px; flex-wrap:wrap; padding:16px 0; border-bottom:1px solid var(--line); }}
nav a {{ border:1px solid var(--line); background:var(--panel); border-radius:999px; padding:7px 11px; font-size:13px; color:var(--text); }}
.section {{ margin-top:20px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }}
.panel-pad {{ padding:16px; }}
.grid {{ display:grid; gap:12px; }}
.grid-4 {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
.grid-3 {{ grid-template-columns:repeat(3,minmax(0,1fr)); }}
.metric-card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; }}
.metric-card .value {{ display:block; font-size:24px; font-weight:750; }}
.metric-card .label {{ margin-top:4px; color:var(--muted); font-size:13px; }}
.meta-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 24px; margin:0; padding:0; list-style:none; font-size:14px; }}
.meta-list span {{ display:block; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.badge {{ display:inline-flex; align-items:center; justify-content:center; min-height:24px; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:700; white-space:nowrap; }}
.pass {{ background:var(--pass-bg); color:var(--pass); }}
.warn {{ background:var(--warn-bg); color:var(--warn); }}
.fail {{ background:var(--fail-bg); color:var(--fail); }}
.info {{ background:var(--info-bg); color:var(--info); }}
.na {{ background:var(--na-bg); color:var(--na); }}
.status-note {{ color:var(--muted); font-size:13px; margin:8px 0 0; }}
.table-wrap {{ width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--panel); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line-soft); text-align:left; vertical-align:top; }}
th {{ background:var(--panel-soft); color:#394155; font-size:12px; font-weight:750; text-transform:uppercase; letter-spacing:.04em; }}
tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.chart-card {{ margin-top:14px; overflow:hidden; }}
.chart-head {{ display:grid; grid-template-columns:1fr auto; gap:12px; padding:16px; border-bottom:1px solid var(--line); background:var(--panel); }}
.chart-head p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }}
.chart-body {{ padding:16px; }}
.proof-strip {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
figure {{ margin:0; }}
figcaption {{ margin-top:6px; font-size:12px; color:var(--muted); }}
.proof-summary {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:0 0 16px; }}
.mini-proof {{ border:1px solid var(--line); border-radius:8px; padding:10px; background:var(--panel-soft); }}
.mini-proof strong {{ display:block; font-size:18px; font-variant-numeric:tabular-nums; }}
.mini-proof span {{ display:block; color:var(--muted); font-size:12px; }}
.subsection {{ margin-top:18px; }}
details {{ border:1px solid var(--line); border-radius:8px; background:var(--panel); margin-top:10px; }}
summary {{ cursor:pointer; padding:11px 13px; font-weight:700; font-size:13px; }}
.details-body {{ border-top:1px solid var(--line); padding:12px 13px; background:var(--panel-soft); }}
pre {{ margin:0; padding:12px; overflow-x:auto; background:#182033; color:#f8fafc; border-radius:6px; font-size:12px; line-height:1.45; white-space:pre-wrap; }}
.callout {{ border:1px solid var(--line); border-left:4px solid var(--info); border-radius:8px; padding:12px 14px; background:var(--panel); font-size:14px; }}
.callout strong {{ display:block; margin-bottom:4px; }}
.footer {{ color:var(--muted); font-size:12px; margin:28px 0 8px; padding-top:14px; border-top:1px solid var(--line); }}
@media (max-width:900px) {{ header,.chart-head,.grid-4,.grid-3,.proof-strip,.proof-summary,.meta-list {{ grid-template-columns:1fr; }} .status-banner {{ min-width:0; }} .page {{ padding:16px; }} }}
</style></head>
<body><div class="page">
  <header>
    <div><h1>{esc(book_name)} Migration Validation Report</h1>
    <p class="subtitle">Tableau to Streamlit-in-Snowflake validation: real live queries,
    formula comparison, and (where available) independent backend/Tableau data checks.</p></div>
    <div class="status-banner {status_class}"><span class="label">Overall Status</span>
    <span class="value">{esc(overall)}</span><div class="reason">{esc(reason)}</div></div>
  </header>
  <nav>
    <a href="#summary">Summary</a><a href="#coverage">Coverage</a>
    <a href="#matrix">Validation Matrix</a><a href="#issues">Issues</a>
    <a href="#chart-details">Chart Details</a><a href="#evidence">Evidence</a>
  </nav>
  <main>
    <section id="summary" class="section">
      <h2>1. Executive Summary</h2>
      <div class="grid grid-4">
        <div class="metric-card"><span class="value">{len(resolved)} / {len(sections)}</span><div class="label">Dashboards validated</div></div>
        <div class="metric-card"><span class="value">{formula_total - formula_bugs} / {formula_total}</span><div class="label">Formulas equivalent</div></div>
        <div class="metric-card"><span class="value">{critical}</span><div class="label">Critical (data) failures</div></div>
        <div class="metric-card"><span class="value">{warnings}</span><div class="label">Warnings / not validated</div></div>
      </div>
      <div class="panel panel-pad" style="margin-top:12px">
        <ul class="meta-list">
          <li><span>Workbook</span>{esc(book_name)}</li>
          <li><span>Run ID</span>{esc(run_id)}</li>
          <li><span>Generated At</span>{esc(now)}</li>
          <li><span>Tableau Source</span>Tableau workbook XML (formulas) + live/known figures where available</li>
          <li><span>Streamlit App</span>Generated app (engine.py + this workbook's IR)</li>
          <li><span>Backend</span>{esc(backend_note or 'Snowflake')}</li>
          <li><span>Validation Mode</span>Live SQL, deterministic formula comparison, real backend recomputation</li>
          <li><span>Numeric Tolerance</span>Relative: {TOL*100:.0f}%</li>
          <li><span>Independent Data Checks</span>{value_total - value_bugs} / {value_total} metric(s) matched an independent App/Backend/Tableau value</li>
        </ul>
      </div>
    </section>

    <section id="coverage" class="section">
      <h2>2. Coverage and Status Rules</h2>
      <div class="grid grid-3">
        <div class="callout"><strong>Validated Areas</strong>Live query execution, formula
        comparison (Tableau TWB vs generated SQL), and independent backend/Tableau data
        reconciliation where a second source exists.</div>
        <div class="callout"><strong>No Proof, No Pass</strong>Any check without independent
        evidence is marked NOT VALIDATED, never silently passed.</div>
        <div class="callout"><strong>Accepted Difference</strong>A formula that differs in
        SHAPE but agrees in VALUE (e.g. implicit-vs-explicit aggregation) is a WARNING, not
        a FAIL -- shown with its real numbers so a human can judge risk.</div>
      </div>
      <div class="table-wrap" style="margin-top:12px"><table><thead><tr>
        <th>Status</th><th>Meaning</th><th>Can Sign Off?</th>
      </tr></thead><tbody>
        <tr><td>{status_badge('PASS')}</td><td>Evidence exists and values are within tolerance.</td><td>Yes</td></tr>
        <tr><td>{status_badge('WARNING')}</td><td>Behavior is equivalent or acceptable, but has a documented risk.</td><td>Yes, with review</td></tr>
        <tr><td>{status_badge('FAIL')}</td><td>Data or logic differs beyond tolerance.</td><td>No</td></tr>
        <tr><td>{status_badge('NOT VALIDATED')}</td><td>Required proof is missing, skipped, or unsupported.</td><td>No</td></tr>
      </tbody></table></div>
    </section>

    <section id="matrix" class="section">
      <h2>3. Dashboard Validation Matrix</h2>
      <div class="table-wrap"><table><thead><tr>
        <th>Dashboard</th><th class="num">Visual</th><th class="num">Data Proof</th>
        <th>Formula Proof</th><th>Interactions</th><th>Evidence</th><th>Status</th>
      </tr></thead><tbody>{"".join(matrix_rows)}</tbody></table></div>
    </section>

    <section id="issues" class="section">
      <h2>4. Issues Register</h2>
      <div class="table-wrap"><table><thead><tr>
        <th>Severity</th><th>Location</th><th>Type</th><th>Issue</th>
        <th>Proof</th><th>Suggested Fix</th><th>Owner Decision</th>
      </tr></thead><tbody>{issue_rows}</tbody></table></div>
    </section>

    <section id="chart-details" class="section">
      <h2>5. Chart-Level Proof</h2>
      {"".join(articles)}
    </section>

    <section id="evidence" class="section">
      <h2>6. Evidence Appendix</h2>
      <div class="grid grid-3">
        <div class="panel panel-pad"><h3>Human Report</h3>
        <p class="status-note">This HTML file: sign-off status, key proof numbers, issue
        register, and expandable per-dashboard evidence.</p></div>
        <div class="panel panel-pad"><h3>Machine Notebook</h3>
        <p class="status-note">The paired .ipynb carries the same live queries and
        comparison tables as real, runnable cells.</p></div>
        <div class="panel panel-pad"><h3>Cortex Rollup</h3>
        <p class="status-note">{rollup_txt}</p></div>
      </div>
    </section>
  </main>
  <div class="footer">Generated by the Tableau to Streamlit-in-Snowflake migration
  accelerator. A check is marked PASS only when real proof exists and the result is
  within tolerance -- never inferred.</div>
</div></body></html>"""


def dashboard_report_to_notebook(sections, bug_rollup, book_name):
    """Render build_dashboard_section_report's ALREADY-EXECUTED results as an
    nbformat notebook -- the real query + real Cortex summary are baked in
    directly (this was executed live when the data was gathered, not a
    template waiting to be run)."""
    today = _dt.date.today().isoformat()
    cells = [_md(
        f"# {book_name} — Dashboard Section Validation\n\n"
        "**Method:** each dashboard is validated as ONE section — a single "
        "combined query pulls every measure the section's sheets use "
        "(grouped by a detected dimension when one resolves), each measure's "
        "Tableau TWB formula is compared to the generated Streamlit SQL, and "
        "Cortex narrates the WHOLE section's findings in one summary.\n\n"
        f"**Date:** {today}")]
    n = 1
    for sec in sections:
        cells.append(_md(f"## {n}. {sec['title']}"))
        if sec.get("skipped"):
            cells.append(_md(f"*Skipped — {sec['skipped']}*"))
            n += 1
            continue
        cells.append(_code(f"-- live combined query -- already run against "
                          f"the real account, result below\n{sec['sql']};"))
        if sec.get("query_error"):
            cells.append(_md(f"**Query error:** {sec['query_error']}"))
        else:
            cols = sec["columns"]
            labels = sec.get("column_labels", {})
            heads = [labels.get(c, c) for c in cols]
            tab = ("| " + " | ".join(heads) + " |\n|" + "---|" * len(cols) + "\n")
            for r in sec["rows"][:20]:
                tab += "| " + " | ".join(str(r.get(c)) for c in cols) + " |\n"
            cells.append(_md(f"**Live result** ({len(sec['rows'])} row(s)):\n\n{tab}"))
        ftab = ("| Metric | Tableau TWB Formula | Streamlit App SQL | Match? | Impact |\n"
               "|---|---|---|---|---|\n")
        for r in sec["formula_rows"]:
            icon = "✅ Match" if r["match"] else "❌ BUG"
            ftab += (f"| {r['metric']} | `{r['twb']}` | `{r['app_sql']}` | "
                    f"{icon} | {r['impact'] or 'None'} |\n")
        cells.append(_md(f"### {n}.1 TWB vs Streamlit — formula comparison\n\n{ftab}"))
        summary = sec.get("cortex_summary") or f"Cortex narration unavailable — {sec.get('cortex_error')}"
        cells.append(_md(f"### {n}.2 Summary of Findings\n\n{summary}"))
        n += 1
    if bug_rollup:
        rt = "| # | Section | Metric | Impact |\n|---|---|---|---|\n"
        for i, b in enumerate(bug_rollup, 1):
            rt += f"| {i} | {b['section']} | {b['metric']} | {b['impact']} |\n"
        cells.append(_md(f"## {n}. Summary of All Bugs\n\n{rt}"))
    else:
        cells.append(_md(f"## {n}. Summary of All Bugs\n\n✅ **No bugs found "
                        "across any validated section.**"))
    nb = {"cells": cells, "metadata": {"language_info": {"name": "python"},
          "kernelspec": {"name": "python3", "display_name": "Python 3"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1)


def dashboard_report_to_html(sections, bug_rollup, book_name):
    """Same already-executed data as dashboard_report_to_notebook, rendered
    as a standalone, self-contained HTML report -- no Jupyter/Snowflake
    needed to view it."""
    import html as _html
    CY, GR, CO = "#00d4d4", "#34d399", "#e05a4e"

    def esc(s):
        return _html.escape(str(s), quote=True)

    body = []
    for i, sec in enumerate(sections, 1):
        if sec.get("skipped"):
            body.append(f'<section class="sec skip"><h2>{i}. {esc(sec["title"])}</h2>'
                        f'<p class="skip-note">Skipped — {esc(sec["skipped"])}</p></section>')
            continue
        if sec.get("query_error"):
            rows_html = f'<p class="err">Query error: {esc(sec["query_error"])}</p>'
        else:
            _labels = sec.get("column_labels", {})
            head = "".join(f"<th>{esc(_labels.get(c, c))}</th>" for c in sec["columns"])
            body_rows = "".join(
                "<tr>" + "".join(f"<td>{esc(r.get(c))}</td>" for c in sec["columns"]) + "</tr>"
                for r in sec["rows"][:20])
            rows_html = (f'<table class="data"><thead><tr>{head}</tr></thead>'
                        f'<tbody>{body_rows}</tbody></table>')
        ftab = "".join(
            f'<tr class="{"ok" if r["match"] else "bug"}">'
            f'<td>{esc(r["metric"])}</td><td><code>{esc(r["twb"])}</code></td>'
            f'<td><code>{esc(r["app_sql"])}</code></td>'
            f'<td>{"✅ Match" if r["match"] else "❌ BUG"}</td>'
            f'<td>{esc(r["impact"] or "None")}</td></tr>'
            for r in sec["formula_rows"])
        summary = esc(sec.get("cortex_summary") or f'unavailable — {sec.get("cortex_error")}')
        body.append(f"""
        <section class="sec">
          <h2>{i}. {esc(sec['title'])}</h2>
          <h4>Live combined query</h4>
          <pre class="sql">{esc(sec['sql'])}</pre>
          <h4>Result ({len(sec.get('rows', []))} row(s))</h4>
          {rows_html}
          <h4>{i}.1 TWB vs Streamlit — formula comparison</h4>
          <table class="ftab"><thead><tr><th>Metric</th><th>Tableau TWB Formula</th>
          <th>Streamlit App SQL</th><th>Match?</th><th>Impact</th></tr></thead>
          <tbody>{ftab}</tbody></table>
          <h4>{i}.2 Summary of Findings</h4>
          <p class="cortex">{summary}</p>
        </section>""")
    if bug_rollup:
        rt = "".join(
            f"<tr><td>{i}</td><td>{esc(b['section'])}</td><td>{esc(b['metric'])}</td>"
            f"<td>{esc(b['impact'])}</td></tr>"
            for i, b in enumerate(bug_rollup, 1))
        rollup_html = (f'<table class="ftab"><thead><tr><th>#</th><th>Section</th>'
                      f'<th>Metric</th><th>Impact</th></tr></thead><tbody>{rt}</tbody></table>')
    else:
        rollup_html = '<p class="ok-msg">✅ No bugs found across any validated section.</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(book_name)} — Dashboard Section Validation</title>
<style>
body{{background:#0a1428;color:#fff;font-family:'Segoe UI',sans-serif;max-width:1100px;
     margin:0 auto;padding:2rem 1.2rem;line-height:1.5}}
h1{{font-size:1.6rem}} h2{{font-size:1.2rem;color:{CY};margin-top:2rem}}
h4{{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:rgba(255,255,255,.55);
   margin:1rem 0 .4rem}}
section.sec{{background:#111d38;border:1px solid rgba(255,255,255,.08);border-radius:8px;
            padding:1rem 1.3rem;margin-bottom:1.2rem}}
section.skip{{opacity:.6}}
.skip-note{{color:rgba(255,255,255,.5);font-style:italic}}
pre.sql{{background:#0d1830;padding:.7rem;border-radius:6px;overflow-x:auto;font-size:.74rem;
        color:{CY};font-family:ui-monospace,Consolas,monospace}}
table{{width:100%;border-collapse:collapse;margin:.4rem 0 .8rem;font-size:.78rem}}
table.data th, table.data td, table.ftab th, table.ftab td{{
   border:1px solid rgba(255,255,255,.1);padding:.35rem .6rem;text-align:left}}
table th{{color:rgba(255,255,255,.55);font-size:.68rem;text-transform:uppercase}}
tr.bug{{background:rgba(224,90,78,.12)}}
code{{color:{CY};font-family:ui-monospace,Consolas,monospace;font-size:.76rem}}
.cortex{{background:rgba(139,124,248,.08);border:1px solid rgba(139,124,248,.3);
        border-radius:6px;padding:.6rem .9rem;margin:0}}
.ok-msg{{color:{GR};font-weight:700}}
.err{{color:{CO}}}
</style></head><body>
<h1>{esc(book_name)} — Dashboard Section Validation</h1>
<p style="color:rgba(255,255,255,.6)">Every query result and every Cortex summary below is a
REAL result from a live run — not a template.</p>
{"".join(body)}
<h2>Summary of All Bugs</h2>
{rollup_html}
</body></html>"""


def _judge_prompt_sql_expr(cap, formula, tv_lit):
    """The Cortex-judge prompt as a SQL expression. Everything known at
    notebook-GENERATION time (cap, formula, tv_lit) is embedded as a literal
    via _sql_lit; the app's computed value is only known at notebook-RUN
    time, so it's concatenated in with TO_VARCHAR(v.APP_VALUE) rather than
    baked in as text. Keeps the notebook fully self-contained SQL -- no
    dependency on this repo's Python modules being importable wherever the
    downloaded .ipynb actually runs (a Snowflake Notebook only guarantees
    `session` + stdlib, not this project's own modules)."""
    head = _sql_lit(
        "You are validating a Tableau-to-Snowflake migration. Compare these "
        "two ALREADY-COMPUTED values for the same metric and judge whether "
        "they agree within reasonable rounding/float tolerance. Do not "
        "recompute either value yourself -- both are given.\n\n"
        f"Metric: {cap}\n"
        f"Tableau's rendered value (from Tableau's own REST API -- ground "
        f"truth): {tv_lit}\n"
        "Streamlit app's computed value: ")
    tail = _sql_lit(
        f"\nOriginal Tableau formula (context only, do not recompute): "
        f"{formula}\n\n"
        "Respond with ONLY a JSON object, no markdown fences, no other "
        'text: {"verdict": "PASS" or "BUG", "explanation": "one sentence, '
        'naming the most likely cause if BUG"}.')
    return f"{head} || TO_VARCHAR(v.APP_VALUE) || {tail}"


def _judge_parse_cell(ident, cap, deterministic_verdict):
    """A plain %python cell (no project-module import) that parses the prior
    SQL cell's Cortex JSON response at notebook-RUN time and decides the
    displayed verdict from IT, not from a value baked in at generation time
    -- this is what makes Cortex's verdict the one actually shown, not just
    narrated alongside a pre-decided one."""
    cap_lit = json.dumps(cap)
    det_lit = json.dumps(deterministic_verdict)
    return (
        "import json, re\n"
        f"_raw = str({ident}_cortex[\"CORTEX_VERDICT\"][0])\n"
        "_m = re.search(r'\\{.*\\}', _raw, re.S)\n"
        "try:\n"
        "    _obj = json.loads(_m.group(0)) if _m else {}\n"
        "except Exception:\n"
        "    _obj = {}\n"
        "_v = str(_obj.get('verdict', '')).strip().upper()\n"
        "cortex_verdict = _v if _v in ('PASS', 'BUG') else 'UNKNOWN'\n"
        "cortex_explanation = _obj.get('explanation') or "
        "(_raw if cortex_verdict == 'UNKNOWN' else '')\n"
        f"deterministic_verdict = {det_lit}\n"
        "icon = {'PASS': '✅', 'BUG': '❌', 'UNKNOWN': '⚠️'}[cortex_verdict]\n"
        f"print(icon + ' Cortex verdict for ' + {cap_lit} + ': ' + cortex_verdict)\n"
        "print('Explanation: ' + str(cortex_explanation))\n"
        "if cortex_verdict != deterministic_verdict and cortex_verdict != 'UNKNOWN':\n"
        "    print('⚠️  Cortex disagrees with the deterministic cross-check "
        "(deterministic said ' + deterministic_verdict + ') -- flagged for human review')\n"
        f"_r2_results.append({{'metric': {cap_lit}, 'cortex_verdict': cortex_verdict, "
        "'cortex_explanation': cortex_explanation, "
        "'deterministic_verdict': deterministic_verdict})\n"
    )


def _no_reference_cell(cap, deterministic_verdict):
    """A plain %python cell recording that this metric had no independent
    Tableau reference to judge against at all -- so it was correctly never
    sent to Cortex, instead of being judged against a meaningless "unknown"
    placeholder. FOUND LIVE 2026-07-29: asking Cortex to compare a real app
    value against the literal text "unknown" reliably produced a BUG
    verdict, misrepresenting "nothing to compare against" as a real
    defect -- the same class of mistake as a deterministic check reporting
    BUG on a metric with no independent second computation path (which
    check_calc_metrics itself correctly avoids via its own EXECUTED
    verdict; this mirrors that same discipline for the Cortex-judged path)."""
    cap_lit = json.dumps(cap)
    det_lit = json.dumps(deterministic_verdict)
    return (
        "print('☑ No independent Tableau reference for ' + " + cap_lit +
        " + ' -- not sent to Cortex (nothing to compare against is not a defect)')\n"
        "_r2_results.append({'metric': " + cap_lit + ", "
        "'cortex_verdict': 'NO_REFERENCE', "
        "'cortex_explanation': 'no independent Tableau value available', "
        "'deterministic_verdict': " + det_lit + "})\n"
    )


def _bug_summary_cell():
    """Final rollup cell -- reduces _r2_results (built live, one entry per
    metric, by each _judge_parse_cell/_no_reference_cell above) rather than
    a bug list computed at generation time. The verdict source of truth is
    Cortex's own decision at RUN time, so the summary has to be computed at
    run time too."""
    return (
        'bugs = [r for r in _r2_results if r["cortex_verdict"] == "BUG"]\n'
        'unknowns = [r for r in _r2_results if r["cortex_verdict"] == "UNKNOWN"]\n'
        'no_reference = [r for r in _r2_results if r["cortex_verdict"] == "NO_REFERENCE"]\n'
        'disagreements = [r for r in _r2_results if r["cortex_verdict"] not in '
        '("UNKNOWN", "NO_REFERENCE") and r["cortex_verdict"] != r["deterministic_verdict"]]\n'
        'if bugs:\n'
        '    print("❌ " + str(len(bugs)) + " bug(s), Cortex-decided:")\n'
        '    for b in bugs:\n'
        '        print("  - " + b["metric"] + ": " + str(b["cortex_explanation"]))\n'
        'else:\n'
        '    print("✅ No bugs -- every migrated metric passes Cortex\'s judgment.")\n'
        'if unknowns:\n'
        '    print(str(len(unknowns)) + " metric(s) Cortex could not judge (parse/call '
        'failure) -- treat as unresolved, not as passing:")\n'
        '    for u in unknowns:\n'
        '        print("  - " + u["metric"])\n'
        'if no_reference:\n'
        '    print(str(len(no_reference)) + " metric(s) had no independent Tableau '
        'reference -- not sent to Cortex, not counted as bugs:")\n'
        '    for r in no_reference:\n'
        '        print("  - " + r["metric"])\n'
        'if disagreements:\n'
        '    print("⚠️  " + str(len(disagreements)) + " metric(s) where Cortex disagreed '
        'with the deterministic cross-check -- review these first:")\n'
        '    for d in disagreements:\n'
        '        print("  - " + d["metric"] + ": Cortex=" + d["cortex_verdict"] '
        '+ " vs deterministic=" + d["deterministic_verdict"])\n'
    )


def build_section_validation_notebook(ir, result, book_name, tableau_truth=None):
    """R2 — per-SECTION end-to-end validation notebook (dashboard-validation
    methodology). For each migrated calculated metric it lines up THREE things:
      1. the source TABLE the app reads,
      2. the migrated STREAMLIT app's SQL (how the app computes the metric),
      3. TABLEAU — the raw TWB formula (parsed from the .twb) + a known figure.
    That proves BOTH layers the user asked for: tables<->app AND app<->Tableau.

    tableau_truth (PLUGGABLE ground truth): {metric_internal_name: value or
    (lo,hi)}. Default = the known-figure bounds already in the result.
    tableau_server.query_view_data_csv() (R2, 2026-07-28) is the intended
    real source for this -- Tableau's ACTUAL rendered per-section values,
    pulled over REST, dropped in here for a literal rendered-dashboard
    comparison instead of the TWB formula's known-figure bound.

    ARCHITECTURE DECISION (2026-07-28, explicit user call, updates the prior
    'Cortex narrates only' rule FOR R2 SPECIFICALLY): Cortex now OWNS the
    PASS/BUG verdict, not just the narration. The boundary that keeps this
    safe: Cortex is handed ALREADY-COMPUTED, ALREADY-REAL numbers on both
    sides (the app's SQL actually executes; tableau_truth is Tableau's own
    REST-rendered value) -- it judges two known numbers, it never computes
    either one, so it can misjudge a comparison but cannot fabricate a
    figure. check_calc_metrics' deterministic verdict is still computed and
    shown per metric as a labeled CROSS-CHECK -- if Cortex's live verdict
    ever disagrees with it, that disagreement is flagged for human review
    rather than silently resolved either way. AI runs at notebook-RUN time
    inside Snowflake, same as the rest of this project's AI-gated paths --
    the verdict-parsing logic ships as plain %python cells (stdlib only, no
    import of this repo's modules) so the downloaded .ipynb stays
    self-contained wherever it's actually opened and run."""
    today = _dt.date.today().isoformat()
    raw = _raw_formulas(ir.get("source_file", ""))
    truth = dict(tableau_truth or {})
    metrics = result.get("calc_metrics") or []

    cells = [_md(
        f"# {book_name} — Section-by-Section Tableau Validation\n\n"
        "**Purpose:** for every migrated metric, compare the **source table**, the "
        "**Streamlit app's SQL**, and **Tableau** (TWB formula + known figure) side "
        "by side — proving both *tables ↔ app* and *app ↔ Tableau*, not just "
        "totals against the back-end tables.\n\n"
        "**How Tableau is represented here:** `tableau_truth` — pluggable, and "
        "intended to carry Tableau's own REST-rendered per-section values "
        "(`tableau_server.query_view_data_csv`) for a literal rendered-dashboard "
        "comparison; falls back to the TWB formula's known-figure bound when no "
        "real value is supplied.\n\n"
        "**Cortex's role: Cortex DECIDES the verdict.** Each metric's cell runs "
        "SNOWFLAKE.CORTEX.COMPLETE on the two already-computed real values (never "
        "asked to compute either one) and returns PASS/BUG + why. The prior "
        "deterministic check is still run and shown as a labeled cross-check — "
        "a disagreement is flagged for review, not silently overridden either way."
        f"\n\n**Date:** {today}")]

    cells.append(_md("## 0. Source audit — tables the app reads"))
    rc = "| Datasource | Table | App rows |\n|---|---|---|\n"
    for d in result["datasources"]:
        rc += f"| {d['datasource']} | `{d['table']}` | {d['app_rows']} |\n"
    cells.append(_md(rc))

    cells.append(_code(
        "_r2_results = []  # one entry per metric, appended live below; the "
        "Bug Summary at the end reduces THIS, not a value baked in at "
        "generation time -- Cortex's verdict is only known once the notebook runs"))

    n = 1
    for m in metrics:
        cap, ds = m["metric"], m["datasource"]
        table = next((d["table"] for d in result["datasources"]
                      if d["datasource"] == ds), None)
        formula = raw.get(cap, "— (formula not found in .twb)")
        app_sql = m["sql"]
        tv = truth.get(m["name"], m.get("tableau_bound"))
        if isinstance(tv, dict) and "value" in tv:
            # the live REST-pull shape ({"value","approx","rows"}) --
            # approximate matches (a multi-row view, column-summed) are
            # labeled as such in the text Cortex/the notebook reader sees,
            # never presented identically to a verified exact single-row
            # match (2026-07-29, user-directed after the exact-only rule
            # yielded nothing for a real multi-dimensional workbook).
            tv_lit = (f"{tv['value']} (approximate -- summed across "
                      f"{tv['rows']} rows, some deviation possible)"
                      if tv.get("approx") else f"{tv['value']} (Tableau REST, exact)")
        elif isinstance(tv, (list, tuple)):
            tv_lit = f"{tv[0]}–{tv[1]}"
        else:
            tv_lit = str(tv) if tv is not None else None
        ident = to_phys(cap)
        det_note = m["verdict"]
        if m.get("error"):
            det_note += f" — {m['error']}"
        cells.append(_md(
            f"## {n}. {cap} — Cortex-judged validation\n\n"
            f"**Datasource:** `{ds}`  ·  **Table:** `{table}`\n\n"
            f"**Tableau TWB formula:** `{formula}`\n\n"
            f"**Streamlit app SQL:** `{app_sql}`\n\n"
            f"**Tableau reference value:** {tv_lit if tv_lit is not None else '— none available'}\n\n"
            f"**Deterministic cross-check (not the shown verdict):** {det_note}"))
        if tv_lit is None:
            # No independent Tableau reference at all (no known-figure
            # bound, no live REST pull). FOUND LIVE 2026-07-29: sending
            # Cortex a real app value to compare against the literal text
            # "unknown" reliably produced a false BUG verdict -- skip the
            # Cortex call entirely rather than ask it to judge nothing.
            cells.append(_md("_No independent Tableau reference available "
                             "for this metric -- not sent to Cortex; "
                             "nothing to compare against is not a defect._"))
            cells.append(_code(_no_reference_cell(cap, m["verdict"])))
            n += 1
            continue
        # ONE SQL cell: compute the real app value, hand it + the real
        # Tableau value to Cortex, get back JSON. App value never leaves
        # SQL as guessed text -- it's concatenated in via TO_VARCHAR.
        prompt_expr = _judge_prompt_sql_expr(cap, formula, tv_lit)
        cells.append(_code(
            f"%%sql -r {ident}_cortex\n"
            f"WITH v AS (SELECT {app_sql} AS APP_VALUE FROM {table})\n"
            "SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-sonnet-4-5',\n"
            f"  {prompt_expr}\n"
            ") AS CORTEX_VERDICT FROM v;"))
        # Plain %python cell -- Cortex's own JSON decides what's PRINTED as
        # the verdict here, not a value this function already picked.
        cells.append(_code(_judge_parse_cell(ident, cap, m["verdict"])))
        n += 1

    cells.append(_md(f"## {n}. Bug summary — Cortex-decided"))
    cells.append(_code(_bug_summary_cell()))

    nb = {"cells": cells, "metadata": {"language_info": {"name": "python"},
          "kernelspec": {"name": "python3", "display_name": "Python 3"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1)


def _md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}


def _code(text):
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": text.splitlines(True)}


def build_notebook(ir, result, book_name):
    """Emit the validation as a Jupyter notebook (dashboard-validation
    methodology): header, per-datasource source-parity query + comparison
    table, calc-coverage, and a roll-up bug summary. Outputs cleared."""
    s = result["summary"]
    today = _dt.date.today().isoformat()
    cells = [_md(
        f"# {book_name} — Migration Validation\n\n"
        f"**Purpose:** Prove the converted Streamlit-in-Snowflake app reproduces "
        f"the Tableau workbook's numbers.\n\n"
        f"**Method:** each measure is computed two independent ways — through the "
        f"app's own SQL path and from a direct read of the source — and, where "
        f"published Tableau figures are known, cross-checked against them.\n\n"
        f"**Date:** {today}\n\n"
        f"**Result:** {s['measures_pass']}/{s['measures_checked']} measures PASS, "
        f"{s['measures_bug']} bug(s); {s['calcs_translated']} calcs translated, "
        f"{s['calcs_dropped']} routed to Cortex/review.")]

    cells.append(_md("## 0. Source File Audit\n\nWhich tables this validation "
                     "reads, and their row counts (complete-load / correct-"
                     "routing check)."))
    rc = "| Datasource | Table | App rows | Source rows | Match |\n" \
         "|---|---|---|---|---|\n"
    for d in result["datasources"]:
        match_icon = "✅" if d["match"] is True else "❌" if d["match"] is False else "—"
        src_rows = d["source_rows"] if d["source_rows"] is not None else "— (no local source file)"
        rc += (f"| {d['datasource']} | `{d['table']}` | {d['app_rows']} | "
               f"{src_rows} | {match_icon} |\n")
    cells.append(_md(rc))

    # one validation section per datasource
    n = 1
    for cap in dict.fromkeys(m["datasource"] for m in result["measures"]):
        table = next((d["table"] for d in result["datasources"]
                      if d["datasource"] == cap), None)
        cells.append(_md(f"## {n}. {cap} — Measure Parity\n\n"
                         f"**Data Source:** `{table}`"))
        ms = [m for m in result["measures"] if m["datasource"] == cap]
        cols = ", ".join(f"SUM({m['column']}) AS {m['column']}" for m in ms)
        cells.append(_code(f"%%sql -r {to_phys(cap)}_parity\n"
                           f"SELECT {cols}\nFROM {table};"))
        tbl = "| Measure | App | Source | Tableau | Verdict |\n|---|---|---|---|---|\n"
        _mv = {"PASS": "✅ PASS", "EXECUTED": "☑ EXECUTED", "BUG": "❌ BUG"}
        for m in ms:
            src = (f"{m['source']} (repull)" if m.get("source_kind") == "table-repull"
                   else (m["source"] if m["source"] is not None else "—"))
            tbl += (f"| **{m['measure']}** | {m['app']} | {src} | "
                    f"{m['tableau'] if m['tableau'] is not None else '—'} | "
                    f"{_mv[m['verdict']]} |\n")
        cells.append(_md(f"### {n}.1 App vs Source vs Tableau\n\n"
                         "_(A `(repull)` source value means no local extract "
                         "file was available in this environment — the "
                         "cross-check re-pulls the same table independently "
                         "rather than the original source file.)_\n\n" + tbl))
        n += 1

    if result.get("calc_metrics"):
        cells.append(_md(
            f"## {n}. Calculated-Field Metrics — Execution + Known-Value Proof\n\n"
            "These are Tableau CALCULATED FIELDS (LODs, ratios, etc.), not raw "
            "columns, so there is no independent second computation path here "
            "(that would mean re-implementing Tableau formula semantics in "
            "pandas). The proof is: the translated SQL EXECUTES against the real "
            "table, and where a Tableau-verified figure is known, the result is "
            "checked against it."))
        tbl2 = "| Datasource | Metric | Value | Tableau bound | Verdict |\n" \
               "|---|---|---|---|---|\n"
        for m in result["calc_metrics"]:
            icon = {"PASS": "✅ PASS", "EXECUTED": "☑ EXECUTED", "BUG": "❌ BUG"}[m["verdict"]]
            val = m["error"] or m["value"]
            bound = f"{m['tableau_bound'][0]}–{m['tableau_bound'][1]}" \
                if m["tableau_bound"] else "—"
            tbl2 += f"| {m['datasource']} | **{m['metric']}** | {val} | {bound} | {icon} |\n"
        cells.append(_md(tbl2))
        n += 1

    cov = ("| Metric | Count |\n|---|---|\n"
           f"| Calcs translated (deterministic) | {s['calcs_translated']} |\n"
           f"| Calcs routed to Cortex / review | {s['calcs_dropped']} |\n")
    drops = "\n".join(f"- `{d}`" for d in s["dropped_names"]) or "_none_"
    cells.append(_md(f"## {n}. Calc Translation Coverage\n\n{cov}\n"
                     f"**Routed to Cortex / review:**\n{drops}"))
    n += 1

    verdict = ("✅ **ALL MEASURES PASS**" if s["measures_bug"] == 0
               else f"❌ **{s['measures_bug']} MEASURE(S) FAILED — see table above**")
    cells.append(_md(f"## {n}. Validation Summary\n\n{verdict}\n\n"
                     f"- Datasources: {s['datasources']}\n"
                     f"- Measures checked: {s['measures_checked']} "
                     f"({s['measures_pass']} pass / {s['measures_bug']} bug)\n"
                     f"- Calcs: {s['calcs_translated']} translated, "
                     f"{s['calcs_dropped']} to Cortex/review"))

    nb = {"cells": cells, "metadata": {"language_info": {"name": "python"},
          "kernelspec": {"name": "python3", "display_name": "Python 3"}},
          "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb, indent=1)

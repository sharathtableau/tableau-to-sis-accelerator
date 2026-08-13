"""
cortex_semantic.py -- generate a Snowflake Cortex Analyst SEMANTIC MODEL
(YAML) from a workbook's IR, so every migrated dashboard ships with a
"talk to your data" layer powered by the SAME verified semantics the app
renders with.

Why this is nearly free: the Tableau workbook already IS a curated semantic
model -- business captions, measure formulas (translated to Snowflake SQL by
calc_translator), dimensions, date fields, aliases. The parser captured all
of it into the IR; this module just re-emits it in Cortex Analyst's format.

Deterministic, zero AI at generation time (same rule as the rest of the
pipeline). The AI happens at RUNTIME, inside Snowflake, when Cortex Analyst
answers questions against this model.

Per datasource in the IR:
  * base_table   <- datasources.deploy.json (Snowflake FQN; falls back to
                    datasources.json for local dev)
  * dimensions   <- fields used as dimensions on any sheet shelf
                    (caption kept as a synonym so NL questions using the
                    Tableau business name resolve)
  * time_dimensions <- date-part shelves / *Date captions
  * facts        <- base numeric columns used with an aggregation
  * metrics      <- Tableau calculated fields whose translated SQL
                    aggregates (params substituted at their workbook
                    defaults, engine.sub_params convention)

Never guesses: a datasource with no Snowflake mapping, or a calc whose SQL
still carries an unresolved __PARAM_/__TBL__ token, is emitted as a YAML
comment for a human decision -- not as a broken definition.

Usage:
    python cortex_semantic.py superstore_ir.json [-o sql/cortex/superstore_semantic_model.yaml]
                              [--mapping datasources.deploy.json]
"""

import argparse
import json
import os
import re

from calc_translator import to_phys

AGG_FACTS = {"sum": "sum", "avg": "avg", "min": "min", "max": "max",
             "cnt": "count", "count": "count", "countd": "count_distinct",
             "med": "median"}
DATE_PARTS = {"year", "qtr", "quarter", "month", "week", "day",
              "ymd", "my", "myr", "mdy", "yr"}
_AGG_RE = re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN)\s*\(", re.I)
_SKIP_KEYS = {"filters", "sort", "manual_sort", "reflines", "geom", "layout",
              "title", "name", "datasource", "kind", "mark", "orient",
              "labels", "device_layouts", "sheet_filters", "params"}
_SKIP_CAPTIONS = {"Multiple Values"}        # Tableau placeholder, not a column
_SQL_WORDS = {"SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN", "DISTINCT",
              "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT",
              "NULLIF", "OVER", "PARTITION", "BY", "IN", "IS", "NULL",
              "LIKE", "BETWEEN", "CAST", "AS", "EXTRACT", "DATE_TRUNC",
              "DATEDIFF", "ROW_NUMBER", "RANK", "DENSE_RANK", "TRUE", "FALSE"}


def _sql_columns(sql):
    """Bare column identifiers referenced by a metric's SQL (heuristic)."""
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)   # string literals are not columns
    toks = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql))
    return {t.upper() for t in toks if t.upper() not in _SQL_WORDS
            and not re.fullmatch(r"\d+", t)}


def _table_columns(entry):
    """Full physical column list from the table's local data file (the same
    file backend.py serves locally) -- shelf usage alone under-counts."""
    lf = (entry or {}).get("local_file")
    if not lf or not os.path.exists(lf):
        return None
    try:
        import pandas as pd
        if lf.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(lf, nrows=0)
        else:
            df = pd.read_csv(lf, nrows=0, encoding="utf-8-sig")
        return {to_phys(str(c)) for c in df.columns}
    except Exception:
        return None


def _param_token(caption):
    return "__PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", caption).strip("_").upper() + "__"


def sub_params(sql, params):
    """Substitute __PARAM_X__ tokens with workbook defaults (engine convention).

    Delegates the literal formatting to calc_translator.param_sql_literal --
    it round-trips numbers through float() so a value read off the XML as
    '0.064000000000000001' collapses to '0.064'. A naive str(val) literal with
    all 18 digits intact makes DuckDB infer a huge-precision DECIMAL, and
    SUM(sales) * that literal overflows DECIMAL(38) (field-found on
    Superstore's 'SUM([Sales])-SUM([Sales Forecast])' calc -- ~0.6/0.064
    params entered via a float() round-trip elsewhere in the XML)."""
    from calc_translator import param_sql_literal
    for cap, val in (params or {}).items():
        tok = _param_token(cap)
        if tok in sql:
            sql = sql.replace(tok, param_sql_literal(val))
    return sql


#  tableau_parser.py stores most shelf fields as {caption, ...} dicts, but a
#  handful of chart-kind-specific keys carry a BARE caption STRING instead --
#  `dim` (mbar's grouping dimension), `geo` (map's location field), `segment`
#  and `panel` (dtbar/strips/small-multiples). See tableau_parser.py's own
#  comment at its `for k in ("x", "y", "dim", ...)` scan: "geo/dim are
#  strings; x/y/color_measure/size/label are dicts". Missing this meant any
#  workbook using these chart kinds silently DROPPED that dimension from the
#  Cortex semantic layer -- found 2026-08-06 via a live Cortex Analyst
#  question ("total sales by region") failing on a workbook whose ONLY use of
#  Region was as an mbar `dim`, even though the engine renders it correctly
#  (the deterministic render path reads `dim` directly; only this collector
#  missed it).
_BARE_STRING_CAPTION_KEYS = {"text_fields", "dim", "geo", "segment", "panel"}


def _field_candidates(sheet):
    """Every {caption, ...} field dict used anywhere on this sheet's shelves."""
    out = []
    for k, v in sheet.items():
        if k in _SKIP_KEYS:
            continue
        items = v if isinstance(v, list) else [v]
        for it in items:
            if isinstance(it, dict) and it.get("caption"):
                out.append(it)
            elif isinstance(it, str) and k in _BARE_STRING_CAPTION_KEYS:
                out.append({"caption": it})
    return out


def _is_time(field):
    agg = (field.get("agg") or "").lower()
    cap = field.get("caption", "")
    return agg in DATE_PARTS or cap.lower().endswith("date")


def collect(ir):
    """Walk the IR -> per-datasource {dims, times, facts} keyed by caption."""
    ds_fields = {}
    calcs = ir.get("calcs", {})
    for d in ir.get("dashboards", []):
        for s in d.get("sheets", []):
            ds = s.get("datasource")
            if not ds:
                continue
            slot = ds_fields.setdefault(ds, {"dims": {}, "times": {}, "facts": {}})
            for f in _field_candidates(s):
                cap = f["caption"]
                if cap in calcs or cap in _SKIP_CAPTIONS:
                    continue                # calcs become metrics, not columns
                agg = (f.get("agg") or "").lower()
                if _is_time(f):
                    slot["times"][cap] = f
                elif agg in AGG_FACTS:
                    slot["facts"][cap] = f
                elif f.get("kind") == "dimension" or agg in ("", "usr", "none"):
                    slot["dims"][cap] = f
    return ds_fields


def _phys(ir, caption):
    return to_phys(ir.get("colmap", {}).get(caption, caption))


def _yq(s):
    """YAML-safe double-quoted scalar."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _table_block(ir, ds_cap, fields, table_fqn, metrics):
    """YAML lines for one logical table."""
    db, schema, table = (table_fqn.split(".") + ["", ""])[:3] if table_fqn.count(".") == 2 \
        else ("", "", table_fqn)
    L = []
    L.append(f"  - name: {to_phys(ds_cap)}")
    L.append(f"    description: {_yq('Migrated from Tableau datasource: ' + ds_cap)}")
    L.append("    base_table:")
    L.append(f"      database: {db}")
    L.append(f"      schema: {schema}")
    L.append(f"      table: {table}")

    def _cols(section, caps, data_type, extra=None):
        if not caps:
            return
        L.append(f"    {section}:")
        for cap in sorted(caps):
            p = _phys(ir, cap)
            L.append(f"      - name: {p}")
            L.append(f"        expr: {p}")
            L.append(f"        data_type: {data_type}")
            L.append(f"        synonyms: [{_yq(cap)}]")
            if extra:
                L.append(f"        {extra(caps[cap])}")

    _cols("dimensions", fields["dims"], "TEXT")
    _cols("time_dimensions", fields["times"], "DATE")
    _cols("facts", fields["facts"], "NUMBER")

    if metrics:
        L.append("    metrics:")
        for m in metrics:
            L.append(f"      - name: {m['name']}")
            L.append(f"        expr: {_yq(m['sql'])}")
            syns = ", ".join(_yq(s) for s in m["synonyms"])
            L.append(f"        synonyms: [{syns}]")
            L.append(f"        description: {_yq('Tableau calculated field: ' + m['synonyms'][0])}")
    return L


def build_metrics(ir):
    """Aggregating calcs -> deduped metric defs (Tableau '(copy)' and
    internal-name calcs share formula text; identical SQL merges into one
    metric, extra captions become synonyms)."""
    by_sql, skipped = {}, []
    for cap, c in ir.get("calcs", {}).items():
        sql = " ".join(sub_params(c.get("sql", ""), ir.get("params", {})).split())
        if not _AGG_RE.search(sql):
            continue                        # row-level calc, not a metric
        if "__PARAM_" in sql or "__TBL__" in sql:
            skipped.append((cap, "unresolved token"))
            continue
        if " OVER (" in sql.upper():
            skipped.append((cap, "window function -- not a scalar metric"))
            continue
        m = by_sql.setdefault(sql, {"name": None, "sql": sql, "synonyms": []})
        m["synonyms"].append(cap)
    out = []
    for m in by_sql.values():
        # prefer a human caption over Tableau internal names (Calculation_123...)
        caps = sorted(m["synonyms"],
                      key=lambda c: (bool(re.match(r"Calculation_\d+", c)),
                                     "(copy)" in c, len(c)))
        m["name"] = to_phys(caps[0])
        m["synonyms"] = caps
        m["cols"] = _sql_columns(m["sql"])
        out.append(m)
    return out, skipped


def generate(ir, mapping, model_name):
    ds_fields = collect(ir)
    all_metrics, skipped = build_metrics(ir)
    L = [f"# GENERATED by cortex_semantic.py -- Cortex Analyst semantic model",
         f"# from the Tableau workbook's own verified semantics. Review, then",
         f"# upload to a stage and register with Cortex Analyst.",
         f"name: {model_name}",
         f"description: {_yq('Semantic model migrated from Tableau: ' + ir.get('source_file', model_name))}",
         "tables:"]
    emitted, placed = 0, set()
    for ds_cap, fields in ds_fields.items():
        entry = mapping.get(ds_cap) or next(
            (v for k, v in mapping.items() if k.startswith(ds_cap)), None)
        if not entry:
            L.append(f"  # {ds_cap}: no Snowflake table mapping in datasources.deploy.json"
                     f" -- load it (load_snowflake.py) then regenerate.")
            continue
        # a metric belongs to the table whose columns cover every column its
        # SQL references (never attach a calc to a table that lacks its
        # columns); the local data file gives the full list, shelves are the
        # fallback
        table_cols = _table_columns(entry) or \
            {_phys(ir, c) for grp in fields.values() for c in grp}
        mine = [m for m in all_metrics
                if id(m) not in placed and m["cols"] <= table_cols]
        placed.update(id(m) for m in mine)
        L.extend(_table_block(ir, ds_cap, fields, entry["table"], mine))
        emitted += 1
    for m in all_metrics:
        if id(m) not in placed:
            L.append(f"  # metric UNPLACED (columns {sorted(m['cols'])} not all on"
                     f" one table's shelves): {m['synonyms'][0]}")
    for cap, why in skipped:
        L.append(f"  # metric SKIPPED ({why}): {cap}")
    return "\n".join(L) + "\n", emitted


def _sq(s):
    """SQL single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _ident(name):
    """SQL identifier: bare when already normal-form, else double-quoted."""
    if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def introspect_columns(mapping, connection):
    """DESCRIBE each mapped table on the REAL account (via snow CLI, from a
    LAPTOP) -> {table_fqn: [actual column names]}. The deployed schema is the
    source of truth for identifiers -- local CSV headers guess wrong when a
    table was loaded with original (quoted, mixed-case) names.

    LAPTOP-ONLY: shells out to the `snow` CLI. A Streamlit-in-Snowflake app
    has no shell access and no `snow` binary in its sandbox -- when a Snowpark
    session is already live (running INSIDE Snowflake), use
    introspect_columns_via_session instead, which needs no subprocess."""
    import subprocess
    out = {}
    for fqn in sorted({e["table"] for e in mapping.values()}):
        try:
            r = subprocess.run(
                ["snow", "sql", "-q", f"DESCRIBE TABLE {fqn}",
                 "--format", "json", "--connection", connection],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                continue                    # table absent on account -- skip
            body = r.stdout[r.stdout.index("["):]
            out[fqn] = [row["name"] for row in json.loads(body)]
        except Exception:
            continue
    return out


def introspect_columns_via_session(session, mapping):
    """Same result as introspect_columns, but through an ALREADY-ACTIVE
    Snowpark session (session.sql) -- the correct path when running INSIDE
    Snowflake (Streamlit-in-Snowflake), where there is no shell/CLI access.
    Reads INFORMATION_SCHEMA.COLUMNS so it works for any table the session's
    role can see, without a DESCRIBE per table."""
    out = {}
    fqns = sorted({e["table"] for e in mapping.values()})
    by_schema = {}
    for fqn in fqns:
        parts = fqn.split(".")
        if len(parts) != 3:
            continue
        db, schema, table = parts
        by_schema.setdefault((db, schema), []).append((fqn, table.upper()))
    for (db, schema), items in by_schema.items():
        try:
            rows = session.sql(
                f'SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION '
                f'FROM "{db}".INFORMATION_SCHEMA.COLUMNS '
                f"WHERE TABLE_SCHEMA = '{schema.upper()}' "
                f"ORDER BY TABLE_NAME, ORDINAL_POSITION").collect()
        except Exception:
            continue
        cols_by_table = {}
        for r in rows:
            cols_by_table.setdefault(r["TABLE_NAME"], []).append(r["COLUMN_NAME"])
        for fqn, table in items:
            if table in cols_by_table:
                out[fqn] = cols_by_table[table]
    return out


def generate_semantic_view(ir, mapping, model_name, db="WBR_DB", schema="PUBLIC",
                           real_cols=None):
    """Native `CREATE SEMANTIC VIEW` DDL (SQL-object form of the same model
    the YAML carries). Preferred on accounts that support it: Cortex Analyst
    queries it directly, and SYSTEM$EXPORT_TDS_FROM_SEMANTIC_VIEW can export
    it BACK to Tableau format.

    real_cols ({table_fqn: [actual column names]} from introspect_columns)
    makes the deployed schema the identifier source of truth: expressions use
    the table's REAL column names (quoted when needed), because tables loaded
    with original mixed-case names reject bare UPPER_SNAKE identifiers."""
    ds_fields = collect(ir)
    all_metrics, skipped = build_metrics(ir)
    tables, facts, dims, mets, notes = [], [], [], [], []
    placed = set()
    for ds_cap, fields in ds_fields.items():
        entry = mapping.get(ds_cap) or next(
            (v for k, v in mapping.items() if k.startswith(ds_cap)), None)
        if not entry:
            notes.append(f"-- {ds_cap}: no Snowflake mapping -- load first, regenerate.")
            continue
        alias = to_phys(ds_cap)
        tables.append(f"    {alias} AS {entry['table']}\n"
                      f"      WITH SYNONYMS = ({_sq(ds_cap)})\n"
                      f"      COMMENT = {_sq('Migrated from Tableau datasource: ' + ds_cap)}")
        # phys (UPPER_SNAKE guess) -> real deployed identifier
        actual = (real_cols or {}).get(entry["table"])
        phys2real = {to_phys(c): c for c in actual} if actual else None
        table_cols = set(phys2real) if phys2real else _table_columns(entry)

        def _ok(cap):
            """Column must physically exist in the base table (a Tableau calc
            posing as a shelf field has no physical column -- skip, note)."""
            if table_cols is not None and _phys(ir, cap) not in table_cols:
                notes.append(f"-- {ds_cap}: {cap!r} has no physical column "
                             f"({_phys(ir, cap)}) -- skipped.")
                return False
            return True

        def _expr(phys):
            return _ident(phys2real[phys]) if phys2real else phys

        fact_caps = sorted(c for c in fields["facts"] if _ok(c))
        for cap in fact_caps:
            p = _phys(ir, cap)
            facts.append(f"    {alias}.{p} AS {_expr(p)}"
                         f" WITH SYNONYMS = ({_sq(cap)})")
        fact_phys = {_phys(ir, c) for c in fact_caps}
        for cap in sorted({**fields["dims"], **fields["times"]}):
            p = _phys(ir, cap)
            if p in fact_phys or not _ok(cap):
                continue                    # facts win a name collision
            dims.append(f"    {alias}.{p} AS {_expr(p)}"
                        f" WITH SYNONYMS = ({_sq(cap)})")
        if table_cols is None:
            table_cols = {_phys(ir, c) for grp in fields.values() for c in grp}
        for m in all_metrics:
            if id(m) in placed or not m["cols"] <= table_cols:
                continue
            placed.add(id(m))
            msql = m["sql"]
            if phys2real:                   # rewrite col tokens to real names
                for p in sorted(m["cols"], key=len, reverse=True):
                    if p in phys2real and _ident(phys2real[p]) != p:
                        msql = re.sub(rf"\b{p}\b", _ident(phys2real[p]).replace("\\", "\\\\"), msql)
            syns = ", ".join(_sq(s) for s in m["synonyms"])
            mets.append(f"    {alias}.{m['name']} AS {msql}\n"
                        f"      WITH SYNONYMS = ({syns})\n"
                        f"      COMMENT = {_sq('Tableau calculated field: ' + m['synonyms'][0])}")
    vname = f"{db}.{schema}.{to_phys(model_name).upper()}_SEMANTIC"
    parts = [f"CREATE OR REPLACE SEMANTIC VIEW {vname}",
             "  TABLES (\n" + ",\n".join(tables) + "\n  )"]
    if facts:
        parts.append("  FACTS (\n" + ",\n".join(facts) + "\n  )")
    if dims:
        parts.append("  DIMENSIONS (\n" + ",\n".join(dims) + "\n  )")
    if mets:
        parts.append("  METRICS (\n" + ",\n".join(mets) + "\n  )")
    parts.append(f"  COMMENT = {_sq('Generated by cortex_semantic.py from ' + ir.get('source_file', model_name))};")
    sql = "\n".join(notes + parts) + "\n"
    for m in all_metrics:
        if id(m) not in placed:
            sql += f"-- metric UNPLACED: {m['synonyms'][0]}\n"
    for cap, why in skipped:
        sql += f"-- metric SKIPPED ({why}): {cap}\n"
    return sql


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ir_json")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--mapping", default="datasources.deploy.json")
    ap.add_argument("--db", default="WBR_DB")
    ap.add_argument("--schema", default="PUBLIC")
    ap.add_argument("--connection", default=None,
                    help="snow CLI connection name; when set, DESCRIBE the real "
                         "tables so identifiers match the deployed schema")
    a = ap.parse_args()
    ir = json.load(open(a.ir_json, encoding="utf-8"))
    mapping = json.load(open(a.mapping, encoding="utf-8"))
    real_cols = introspect_columns(mapping, a.connection) if a.connection else None
    if a.connection:
        print(f"-- introspected {len(real_cols or {})} table(s) via connection {a.connection}")
    stem = os.path.splitext(os.path.basename(a.ir_json))[0].replace("_ir", "")
    out = a.out or os.path.join("sql", "cortex", f"{stem}_semantic_model.yaml")
    yaml_text, n = generate(ir, mapping, stem)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    print(f"-> {out}  ({n} logical table(s), "
          f"{yaml_text.count('- name:')} named objects)")
    sql_out = os.path.join(os.path.dirname(out) or ".", f"{stem}_semantic_view.sql")
    sql_text = generate_semantic_view(ir, mapping, stem, a.db, a.schema, real_cols)
    with open(sql_out, "w", encoding="utf-8") as f:
        f.write(sql_text)
    print(f"-> {sql_out}  (CREATE SEMANTIC VIEW)")


if __name__ == "__main__":
    main()

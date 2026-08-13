"""
semantic_layer.py -- generate CREATE VIEW DDL from a workbook's OWN data
model (the relationship graph Tableau stores in the XML), so live multi-
table sources work without moving data: point the app at one semantic view
per datasource.

The view does two jobs at once:
  1. JOINS: fact LEFT JOIN dims exactly as the workbook's relationships
     declare (same graph that drives the extract flatten).
  2. NAMING: every column is aliased to the app's physical naming
     (to_phys, with Tableau's 'col (Table)' collision renames), so the
     GENERATED APP QUERIES THE VIEW AS-IS -- no config edits.

Source tables:
  * live Snowflake connection  -> qualified from the connection's OWN
    dbname/schema in the XML (no data movement).
  * anything else (extracts)   -> assumed loaded to --db/--schema under
    to_phys(object caption) (load_snowflake.py convention).

Never guesses: non-star graphs and legacy blend pairs are listed as
comments for a human decision, not emitted as speculative SQL.

Usage:
    python semantic_layer.py Book.twbx [-o sql/semantic_views.sql]
                             [--db SUPERSTORE --schema PUBLIC]
"""

import argparse
import re

import tableau_parser as TP
from calc_translator import to_phys
from init_workbook import parse_relationships


def _objects(ds):
    """Object-model tables of one datasource: id -> {caption, source}."""
    out = {}
    for el in ds.iter():
        if not (isinstance(el.tag, str) and el.tag.endswith("object")
                and "graph" not in el.tag):
            continue
        oid = el.get("id")
        if not oid:
            continue
        rel = next((r for r in el.iter()
                    if isinstance(r.tag, str) and r.tag.endswith("relation")
                    and r.get("type") == "table"), None)
        out[oid] = {"caption": el.get("caption") or oid,
                    "source": (rel.get("table") if rel is not None else None)}
    return out


def _columns(ds):
    """table caption -> [remote column names] from metadata-records."""
    cols = {}
    for mr in ds.iter("metadata-record"):
        if mr.get("class") != "column":
            continue
        parent = (mr.findtext("parent-name") or "").strip("[]")
        remote = mr.findtext("remote-name")
        if parent and remote and remote not in cols.setdefault(parent, []):
            cols[parent].append(remote)
    return cols


def _connection(ds):
    """The datasource's REAL upstream connection -- R10 ROOT-CAUSE FIX
    (2026-07-26).

    Was `ds.find(".//connection")`: the FIRST <connection> element in document
    order, which for every real federated datasource is the OUTER
    class='federated' WRAPPER, never the actual upstream connection nested in
    <named-connections>. Verified live on Regional Analysis: this returned
    {'class': 'federated', 'dbname': None, ...} for a datasource whose real
    upstream is Snowflake -- so _src_table()'s "keep the live/declared
    location" branch (gated on class == 'snowflake') could NEVER fire for an
    extract-based (or even a genuinely live, no-extract) multi-table
    datasource. Every multi-table model was therefore always assumed to need
    copying, even when its tables already exist separately in the account.

    Reuses tableau_parser._upstream_connections -- the SAME upstream-detection
    this project already built and gated for R3's source_tables() (excludes
    the federated wrapper, the extract's OWN hyper/dataengine engine, and
    file-backed connections). ONE canonical "what is the real source" answer
    for both the single-table (R3) and multi-table (this) cases -- two
    functions independently deciding "is this really Snowflake" is exactly
    the kind of divergence this project keeps getting bitten by.

    Returns {} (unchanged fallback) when there is no real remote upstream --
    e.g. a star built entirely from bundled CSV/Excel (Superstore's People/
    Returns, E-Commerce's flat sources): those have nothing better than the
    pipeline's own copy, so _src_table's existing "assumed copy" branch is
    correct and untouched for them."""
    c, _named = TP._upstream_connections(ds)
    if c is None:
        return {}
    return {"class": c.get("class"), "dbname": c.get("dbname"),
            "schema": c.get("schema"), "server": c.get("server")}


def _parse_relation_table(table_attr, default_db=None, default_schema=None):
    """A relation's `table=` attribute -> (db, schema, table). Handles the
    three real shapes seen in Tableau XML (confirmed against the actual
    corpus, not assumed): '[TABLE]' (db/schema live on the connection),
    '[SCHEMA].[TABLE]', and '[DB].[SCHEMA].[TABLE]' (fully qualified on the
    relation itself -- Regional Analysis' 'SAMPLE_SUPER_STORE_ORDERS' relation
    is exactly this shape: table='[SANDBOX].[DS].[SAMPLE_SUPER_STORE_ORDERS]').

    THE OTHER HALF OF THE ROOT-CAUSE FIX: even with _connection() finding the
    right connection, the old _src_table() did
    `obj["source"].strip("[]").replace("].[", ".")` then PREPENDED the
    connection's dbname/schema unconditionally -- for a 3-segment relation
    (which already carries its own db+schema) that DOUBLES them into
    db.schema.db.schema.table, the exact "double-schema" bug class
    live_connections()/source_tables() already had to guard against
    separately. This is the ONE place semantic_layer needs that same care;
    never dot-join every bracket segment onto a caller-supplied default.

    The table is always the LAST segment. The schema is the second-to-last
    when present, else the caller's default. The db is the THIRD-to-last
    (i.e. present only in a 3-segment relation) else the caller's default --
    the relation's OWN declared db/schema wins when the relation states it
    explicitly, since that is more specific than the connection-level default."""
    parts = [p for p in (table_attr or "").strip("[]").split("].[") if p]
    if not parts:
        return default_db, default_schema, None
    table = parts[-1]
    schema = parts[-2] if len(parts) >= 2 else default_schema
    db = parts[-3] if len(parts) >= 3 else default_db
    return db, schema, table


def data_model(root):
    """Per-datasource model: objects, relationships, columns, connection."""
    rels_all = parse_relationships(root)
    out = []
    for ds in root.findall(".//datasources/datasource"):
        if ds.get("name") == "Parameters":
            continue
        objs = _objects(ds)
        if not objs:
            continue
        rels = [r for r in rels_all if r["first"] in objs and r["second"] in objs]
        out.append({"caption": ds.get("caption") or ds.get("name"),
                    "objects": objs, "relationships": rels,
                    "columns": _columns(ds), "connection": _connection(ds)})
    return out


def join_plan(objs, rels):
    """Deterministic join order for a relationship graph -> plan dict, or a
    refusal. THE R7 GENERALIZATION.

    Before this, only a STAR (one fact, every other table hanging directly off
    it) was joinable; everything else was reported "model manually". But a star
    is just the depth-1 case of a TREE, and a SNOWFLAKE SCHEMA (Orders -> Product
    -> Category, i.e. a dim joined to another dim) is equally unambiguous -- there
    is exactly one path between any two tables, so the join order is forced, not
    chosen. Refusing those was a limitation of the check, not of the data.

    What still REFUSES, because these are genuinely ambiguous and this project
    does not guess at joins:
      * MULTI-FACT (>1 table that is never on the 'second' side): two fact
        tables sharing a dim can be joined several ways with different row
        counts. Which one is a modelling decision a human must make.
      * CYCLES / disconnected graphs (edge count != n-1): more than one path
        between tables, or no path at all -- fan-out or a cartesian product.

    Returns {'shape': 'single'|'star'|'snowflake', 'root': id, 'steps':
    [{'rel', 'alias', 'parent_alias', 'parent_id'}]} on success, else
    {'shape': 'multi_fact'|'non_star', 'root': None, 'steps': [], 'reason': str}.
    The steps are ordered so every step's PARENT is already joined -- which is
    what makes a depth->1 graph emit correct SQL (each ON clause references its
    own parent's alias, not blindly the fact's)."""
    n = len(objs)
    if n <= 1:
        return {"shape": "single", "root": next(iter(objs), None), "steps": [],
                "reason": None}
    rels = [r for r in rels if r["first"] in objs and r["second"] in objs]
    if len(rels) != n - 1:
        return {"shape": "non_star", "root": None, "steps": [],
                "reason": (f"{n} tables but {len(rels)} relationships (a tree needs "
                           f"{n - 1}) — the graph has a cycle or is disconnected, so "
                           "more than one join path exists; model manually")}
    seconds = {r["second"] for r in rels}
    roots = [o for o in objs if o not in seconds]
    if len(roots) != 1:
        return {"shape": "multi_fact", "root": None, "steps": [],
                "reason": (f"{len(roots)} fact tables (tables nothing joins TO) — a "
                           "multi-fact graph can be joined several ways with "
                           "different row counts; model manually")}
    root = roots[0]
    by_parent = {}
    for r in rels:
        by_parent.setdefault(r["first"], []).append(r)
    steps, seen, queue, depth = [], {root}, [root], {root: 0}
    while queue:                              # BFS -> parents always precede children
        cur = queue.pop(0)
        for r in by_parent.get(cur, []):
            if r["second"] in seen:
                continue                      # would be a cycle; edge count rules it out
            seen.add(r["second"])
            depth[r["second"]] = depth[cur] + 1
            steps.append({"rel": r, "alias": f"d{len(steps)}",
                          "parent_id": cur, "parent_alias": None})
            queue.append(r["second"])
    if len(seen) != n:
        return {"shape": "non_star", "root": None, "steps": [],
                "reason": (f"{n - len(seen)} table(s) unreachable from the fact — the "
                           "graph is disconnected; model manually")}
    alias_of = {root: "f"}
    for s in steps:
        alias_of[s["rel"]["second"]] = s["alias"]
    for s in steps:
        s["parent_alias"] = alias_of[s["parent_id"]]
    return {"shape": "star" if max(depth.values()) <= 1 else "snowflake",
            "root": root, "steps": steps, "reason": None}


def _qualify(db, schema, table):
    parts = [p for p in (db, schema) if p]
    return ".".join(parts + [table]) if parts else table


def _src_table(obj, conn, db, schema):
    """FROM target for one object. Returns (fqn, is_declared_source).

    is_declared_source=True means this fqn is the workbook's OWN declared
    upstream location (never copied by this pipeline) -- a CANDIDATE only,
    not yet verified. Callers that can reach a live session (pipeline.
    data_model_report) MUST existence+column-verify before trusting it (the
    same "a name is not evidence" rule R3 was built on); callers with no
    session (offline DDL preview, describe_model with session=None) show it
    as unverified. is_declared_source=False means the ASSUMED-COPY location
    this pipeline itself loads tables into -- trusted the same way it always
    was (this pipeline wrote it, normalized, no verification needed beyond
    "does it exist")."""
    if conn.get("class") == "snowflake" and obj.get("source"):
        d, s, t = _parse_relation_table(obj["source"], conn.get("dbname"),
                                        conn.get("schema"))
        if t:
            return _qualify(d, s, t), True
    return _qualify(db, schema, to_phys(obj["caption"])), False


def generate_views(model, db="SUPERSTORE", schema="PUBLIC", phys_source=False):
    """DDL text for every multi-table datasource (star graphs only).

    phys_source: how to read a table that is an ASSUMED COPY (this pipeline's
    own load) -- False keeps original column names quoted (f."event_id"), True
    reads them normalized via to_phys (f.EVENT_ID, required once loaded through
    _normalize_columns). A table that resolves to the workbook's OWN DECLARED
    source location (R10 -- _src_table's is_declared_source=True) ALWAYS reads
    quoted-original instead, regardless of phys_source: this pipeline never
    touched that table, so there is nothing to normalize -- Tableau's own
    metadata-record remote-name IS its real column name."""
    lines = ["-- GENERATED by semantic_layer.py -- views materialize the",
             "-- workbook's OWN relationship graph; the app queries these",
             "-- views directly (columns aliased to its physical naming).",
             ""]
    for ds in model:
        cap = ds["caption"]
        objs, rels = ds["objects"], ds["relationships"]
        conn = ds["connection"]
        plan = join_plan(objs, rels)
        if plan["shape"] == "single":
            only = next(iter(objs.values()), None)
            if only:
                fqn, _decl = _src_table(only, conn, db, schema)
                lines.append(f"-- {cap}: single table {fqn} -- no view needed.")
            continue
        if not plan["steps"]:
            lines.append(f"-- {cap}: {len(objs)} tables -- {plan['reason']}. "
                         f"DDL not guessed.")
            continue
        fact = objs[plan["root"]]
        vname = _qualify(db, schema, to_phys(cap) + "_MODEL")
        sel, used = [], set()
        alias_declared = {}       # alias -> is this table a declared source?

        def _src(col, declared):
            return f'"{col}"' if declared else (to_phys(col) if phys_source else f'"{col}"')

        def _add_cols(alias, table_caption, declared, dim_caption=None):
            for col in ds["columns"].get(table_caption, []):
                phys = to_phys(col)
                if phys.lower() in used and dim_caption:
                    phys = to_phys(f"{col} ({dim_caption})")
                if phys.lower() in used:
                    continue
                used.add(phys.lower())
                sel.append(f'  {alias}.{_src(col, declared)} AS {phys}')

        fact_fqn, fact_declared = _src_table(fact, conn, db, schema)
        alias_declared["f"] = fact_declared
        _add_cols("f", fact["caption"], fact_declared)
        joins = []
        for step in plan["steps"]:
            r, a = step["rel"], step["alias"]
            dim = objs[r["second"]]
            dim_fqn, dim_declared = _src_table(dim, conn, db, schema)
            alias_declared[a] = dim_declared
            _add_cols(a, dim["caption"], dim_declared, dim["caption"])
            # ON references THIS step's own parent, not blindly the fact. For a
            # star every parent IS the fact, so star DDL is byte-identical to
            # before; for a snowflake chain this is what makes it correct.
            joins.append(f'LEFT JOIN {dim_fqn} {a} '
                         f'ON {step["parent_alias"]}.'
                         f'{_src(r["lkey"], alias_declared[step["parent_alias"]])} '
                         f'= {a}.{_src(r["rkey"], dim_declared)}')
        if not sel:
            lines.append(f"-- {cap}: no column metadata in the workbook; "
                         f"write the view by hand (join keys: "
                         + "; ".join(f'{s["rel"]["lkey"]}={s["rel"]["rkey"]}'
                                     for s in plan["steps"]) + ").")
            continue
        lines.append(f"CREATE OR REPLACE VIEW {vname} AS")
        lines.append("SELECT")
        lines.append(",\n".join(sel))
        lines.append(f"FROM {fact_fqn} f")
        lines.extend(joins)
        lines.append(";")
        lines.append("")
    return "\n".join(lines) + "\n"


def describe_model(root, db="WBR_DB", schema="PUBLIC", phys_source=False):
    """Structured per-datasource data-model summary for the pipeline UI + the
    data-model-view deploy step. Returns, per datasource:
      caption, shape ('single' | 'star' | 'non_star'), n_tables,
      tables [{caption, fqn}], joins [{left, lkey, right, rkey}],
      view_ddl (the CREATE OR REPLACE VIEW replicating Tableau's relationships,
                for a star; None otherwise).
    No session -- pure XML -> structure, so it is offline-testable."""
    out = []
    for ds in data_model(root):
        objs, rels, conn = ds["objects"], ds["relationships"], ds["connection"]
        tables = []
        for o in objs.values():
            fqn, declared = _src_table(o, conn, db, schema)
            # R10: `declared` is a CANDIDATE, not a fact -- it is the workbook's
            # own claimed source location, unverified against the real account.
            # Callers with a session (pipeline.data_model_report) MUST confirm
            # existence + columns before trusting it; `columns` is exposed here
            # so that verification has something to check against (the same
            # source-columns-Tableau-recorded evidence R3's column guard uses).
            tables.append({"caption": o["caption"], "fqn": fqn,
                          "is_declared_source": declared,
                          "columns": ds["columns"].get(o["caption"], [])})
        # ONE classifier for the shape AND the DDL, so the two can never disagree
        # (the UI saying 'star' while generate_views refuses, or vice versa).
        plan = join_plan(objs, rels)
        shape = plan["shape"]
        joins = [{"left": objs[r["first"]]["caption"], "lkey": r["lkey"],
                  "right": objs[r["second"]]["caption"], "rkey": r["rkey"]}
                 for r in rels]
        joinable = bool(plan["steps"])
        out.append({"caption": ds["caption"], "shape": shape, "n_tables": len(objs),
                    "tables": tables, "joins": joins,
                    "joinable": joinable, "reason": plan["reason"],
                    "view_ddl": (generate_views([ds], db, schema, phys_source=phys_source)
                                 if joinable else None)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("twb")
    ap.add_argument("-o", "--out", default="sql/semantic_views.sql")
    ap.add_argument("--db", default="SUPERSTORE")
    ap.add_argument("--schema", default="PUBLIC")
    a = ap.parse_args()
    root = TP.load_twb_xml(a.twb)
    model = data_model(root)
    sql = generate_views(model, a.db, a.schema)
    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(sql)
    n_views = sql.count("CREATE OR REPLACE VIEW")
    print(f"-> {a.out}  ({n_views} view(s), "
          f"{sum(len(d['objects']) for d in model)} tables across "
          f"{len(model)} datasource(s))")


if __name__ == "__main__":
    main()

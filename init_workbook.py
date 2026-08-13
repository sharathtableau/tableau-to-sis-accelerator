"""
init_workbook.py  --  ONE-COMMAND onboarding for a new Tableau workbook.

Does the manual setup steps automatically:
  1. Reads the workbook's datasources and their source-file connections.
  2. .twbx: extracts every bundled data file into data/.
  3. Matches each datasource to a local file (prefers a same-stem .csv).
  4. Writes datasources.json  (config.py merges it over its built-ins).

Usage:
  python init_workbook.py YourBook.twbx [--db SUPERSTORE] [--schema PUBLIC]
                                        [--out datasources.json] [--force]

Then continue with the normal pipeline:
  python tableau_parser.py YourBook.twbx -o workbook_ir.json
  python codegen.py workbook_ir.json -o app.py
  python report.py YourBook.twbx
"""

import argparse
import json
import os
import re
import zipfile

import tableau_parser as TP
from calc_translator import to_phys

DATA_EXT = {".csv", ".xls", ".xlsx", ".txt", ".tsv", ".hyper", ".tde"}


def extract_twbx_data(path, outdir="data"):
    """Pull every bundled data file out of a .twbx into data/. Returns paths."""
    extracted = []
    if not path.lower().endswith(".twbx"):
        return extracted
    os.makedirs(outdir, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            ext = os.path.splitext(n)[1].lower()
            if ext not in DATA_EXT:
                continue
            dst = os.path.join(outdir, os.path.basename(n))
            with open(dst, "wb") as f:
                f.write(z.read(n))
            extracted.append(dst)
    return extracted


def parse_relationships(root):
    """Tableau relationship graph (object model) -> list of
    {first, second, lkey, rkey}. Keys come back caption-style:
    the dim-side key may carry Tableau's collision rename
    ('customer_id (Customers)') -- rkey is the PHYSICAL dim column."""
    rels = []
    for r in root.iter():
        if not (isinstance(r.tag, str) and r.tag.endswith("relationship")):
            continue
        exprs = [e.get("op") for e in r.iter()
                 if isinstance(e.tag, str) and e.tag.endswith("expression")
                 and e.get("op") and e.get("op") != "="]
        ends = [e.get("object-id") for e in r.iter()
                if isinstance(e.tag, str) and e.tag.endswith("end-point")]
        if len(exprs) == 2 and len(ends) == 2:
            lkey = exprs[0].strip("[]")
            rkey = re.sub(r"\s*\([^)]*\)$", "", exprs[1].strip("[]"))
            rels.append({"first": ends[0], "second": ends[1],
                         "lkey": lkey, "rkey": rkey})
    return rels


def _obj_caption(object_id):
    """'Customers_45D1687D8E0B46B7...' -> 'Customers'."""
    return re.sub(r"_[0-9A-F]{16,}$", "", object_id or "", flags=re.I)


def hyper_to_csv(hyper_path, outdir="data", relationships=None):
    """Convert a .hyper extract to ONE CSV. Single-table extracts dump as-is.
    Multi-table extracts (Tableau 2020.2+ relationship extracts store tables
    SEPARATELY -- joins are NOT materialized) flatten deterministically when
    the relationship graph is a star around one fact table: fact LEFT JOIN
    each dim, colliding dim columns renamed 'col (Table)' exactly the way
    Tableau exposes them. Anything non-star: largest table + loud warning
    (never guess). Returns the CSV path or None."""
    try:
        from tableauhyperapi import HyperProcess, Connection, Telemetry
    except ImportError:
        print(f"  ~ {hyper_path}: install 'tableauhyperapi' to auto-convert "
              f"hyper extracts to CSV")
        return None
    import pandas as pd
    tables = {}
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(hp.endpoint, hyper_path) as con:
            for schema in con.catalog.get_schema_names():
                for t in con.catalog.get_table_names(schema):
                    cols = [c.name.unescaped
                            for c in con.catalog.get_table_definition(t).columns]
                    rows = con.execute_list_query(f"SELECT * FROM {t}")
                    tables[t.name.unescaped] = pd.DataFrame(rows, columns=cols)
    if not tables:
        return None
    out = os.path.join(outdir,
                       os.path.splitext(os.path.basename(hyper_path))[0] + ".csv")

    df, note = flatten_tables(tables, relationships)
    if note:
        print(note)
    df.to_csv(out, index=False)
    print(f"  converted {hyper_path} -> {out}  ({len(df)} rows)")
    return out


def flatten_tables(tables, relationships):
    """{table_id: DataFrame} + the relationship graph -> (one flattened frame,
    log text). Extracted from hyper_to_csv so the JOIN LOGIC can be tested with
    plain DataFrames -- decoding a .hyper needs Tableau's engine, which meant the
    riskiest part of this path (the merge order and key tracking) previously had
    no direct test.

    Uses semantic_layer.join_plan -- the SAME planner that emits the view DDL.
    ONE classifier for both is deliberate: two data paths disagreeing about what
    is joinable is this project's most-repeated bug class (the converter/
    init_workbook decode divergence that silently dropped E-Commerce's dim
    columns). If the DDL says a graph is joinable, the flatten must agree."""
    from semantic_layer import join_plan
    log = []
    if len(tables) == 1:
        return next(iter(tables.values())), ""
    rels = [r for r in (relationships or [])
            if r["first"] in tables and r["second"] in tables]
    plan = join_plan({t: {"caption": _obj_caption(t)} for t in tables}, rels)
    if not plan["steps"]:
        df = max(tables.values(), key=len)
        return df, (f"  !! {len(tables)} tables -- {plan['reason']}. Dumped the "
                    f"largest table ONLY; fields from other tables will not "
                    f"resolve. Model the joins in Snowflake views instead.")
    df = tables[plan["root"]]
    # A column of the PARENT may have been renamed 'col (Parent)' when it collided
    # on an EARLIER merge; a snowflake chain then joins ON that renamed column.
    # Track where each table's columns ended up so a depth>1 join resolves against
    # the accumulated frame instead of a name that no longer exists.
    renamed = {}                              # (table_id, original col) -> current
    for step in plan["steps"]:
        r = step["rel"]
        dim = tables[r["second"]].copy()
        dcap = _obj_caption(r["second"])
        ren = {c: f"{c} ({dcap})" for c in dim.columns
               if c != r["rkey"] and c in df.columns}
        dim = dim.rename(columns=ren)
        for c, new in ren.items():
            renamed[(r["second"], c)] = new
        rkey = r["rkey"]
        if rkey in df.columns and rkey != r["lkey"]:
            dim = dim.rename(columns={rkey: f"{rkey} ({dcap})"})
            renamed[(r["second"], r["rkey"])] = f"{rkey} ({dcap})"
            rkey = f"{rkey} ({dcap})"
        lkey = renamed.get((step["parent_id"], r["lkey"]), r["lkey"])
        if lkey not in df.columns:
            log.append(f"  !! join key '{lkey}' not present after earlier merges "
                       f"-- stopping the flatten here rather than guessing "
                       f"(fields from {dcap} will not resolve).")
            break
        df = df.merge(dim, how="left", left_on=lkey, right_on=rkey)
        log.append(f"    joined {dcap} on "
                   f"{_obj_caption(step['parent_id'])}.{lkey} ({len(dim)} rows)")
    log.append(f"    flattened {len(tables)} tables around fact "
               f"'{_obj_caption(plan['root'])}' (relationship {plan['shape']})")
    return df, "\n".join(log)


def hyper_to_tables(hyper_path, outdir="data"):
    """Decode a .hyper extract to SEPARATE per-table CSVs (NOT flattened) --
    scope-B data-model replication. Each Tableau logical table lands as its own
    CSV keyed by object CAPTION (so a loader can create `to_phys(caption)` tables
    a semantic_layer relationship VIEW then joins), instead of collapsing the
    model into one merged table like hyper_to_csv. Returns {caption: csv_path}
    (empty if the extract can't be decoded here)."""
    try:
        from tableauhyperapi import HyperProcess, Connection, Telemetry
    except ImportError:
        return {}
    import pandas as pd
    out = {}
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(hp.endpoint, hyper_path) as con:
            for schema in con.catalog.get_schema_names():
                for t in con.catalog.get_table_names(schema):
                    cols = [c.name.unescaped
                            for c in con.catalog.get_table_definition(t).columns]
                    rows = con.execute_list_query(f"SELECT * FROM {t}")
                    cap = _obj_caption(t.name.unescaped)
                    path = os.path.join(
                        outdir, re.sub(r"[^0-9A-Za-z]+", "_", cap).strip("_") + ".csv")
                    pd.DataFrame(rows, columns=cols).to_csv(path, index=False)
                    out[cap] = path
    return out


def materialize_union(member_paths, out_path):
    """Combine same-schema UNION member files row-wise (UNION ALL) into one CSV
    at out_path. Column alignment is by NAME (Tableau union semantics -- a
    column missing from one member becomes empty for those rows). Returns
    out_path, or None if no member file could be read. Adds a 'Table Name'
    column naming each row's source file (Tableau adds this to a union too)."""
    import pandas as pd
    from backend import _read_source_file
    frames = []
    for p in member_paths:
        if p and os.path.exists(p):
            df = _read_source_file(p)
            df = df.copy()
            df["Table Name"] = os.path.splitext(os.path.basename(p))[0]
            frames.append(df)
    if not frames:
        return None
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def datasource_files(root):
    """Datasource caption -> source file basename(s) from its connections."""
    out = {}
    for ds in root.findall("./datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters":
            continue
        files = []
        for c in ds.findall(".//connection"):
            # file path may be in filename (csv/excel) OR dbname (hyper extract)
            for attr in ("filename", "dbname"):
                fn = c.get(attr) or ""
                if fn and ("/" in fn or "\\" in fn or "." in os.path.basename(fn)):
                    files.append(os.path.basename(fn.replace("\\", "/")))
        out[cap] = files
    return out


def pick_local_file(basenames, datadir="data"):
    """Best local file for a datasource: exact basename match in data/,
    preferring a same-stem .csv over .xls/.xlsx (no extra readers needed)."""
    if not os.path.isdir(datadir):
        return None
    have = {f.lower(): f for f in os.listdir(datadir)}
    for b in basenames:
        stem = os.path.splitext(b)[0].lower()
        if stem + ".csv" in have:                    # prefer csv sibling
            return os.path.join(datadir, have[stem + ".csv"])
        if b.lower() in have:
            return os.path.join(datadir, have[b.lower()])
    return None


def extract_for_caption(converted, caption):
    """The extract THIS workbook ships for `caption`, if any.

    Tableau names a datasource's extract after the datasource caption
    ('Sample - Superstore (2).hyper' for caption 'Sample - Superstore (2)').

    This must WIN over pick_local_file: the connection element names the
    ORIGINAL source ('Sample - Superstore.xls'), and a previous workbook may
    have left a same-stemmed file in data/ -- Fil Test mapped to the old dev
    'Sample - Superstore.csv' that way and every number in the app came from
    the wrong dataset, silently. A .twbx that ships an extract IS the data
    Tableau reads; nothing in data/ may outrank it."""
    want = str(caption).strip().lower()
    for c in converted:
        if os.path.splitext(os.path.basename(c))[0].strip().lower() == want:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("twb")
    ap.add_argument("--db", default="SUPERSTORE")
    ap.add_argument("--schema", default="PUBLIC")
    ap.add_argument("--out", default="datasources.json")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--connection", default=None,
                    help="snow CLI connection name. Enables R3 auto-binding: "
                         "a datasource whose source table already exists in the "
                         "account is pointed straight at it (no extract copy) "
                         "instead of being decoded and loaded.")
    a = ap.parse_args()

    # R3 auto-bind (same resolver pipeline.onboard uses -- this project's two
    # onboarding paths must never diverge). Needs a session to probe
    # INFORMATION_SCHEMA; without --connection this stays empty and every
    # decision below is exactly what it was before the feature.
    auto_bound, auto_reports = {}, []
    if a.connection:
        import pipeline
        try:
            _sess = pipeline.snow_session(a.connection)
            auto_bound, auto_reports = pipeline.auto_bind_sources(
                _sess, TP.load_twb_xml(a.twb), db=a.db, schema=a.schema)
        except Exception as e:
            print(f"  ! auto-bind skipped (could not connect to "
                  f"'{a.connection}'): {e}")

    # MERGE with any existing mapping so multiple onboarded workbooks coexist
    # (each app keeps its tables; nothing is clobbered between conversions).
    existing = {}
    if os.path.exists(a.out):
        try:
            existing = json.load(open(a.out, encoding="utf-8"))
        except Exception:
            existing = {}

    extracted = extract_twbx_data(a.twb)
    for p in extracted:
        print("extracted", p)

    root = TP.load_twb_xml(a.twb)
    rels = parse_relationships(root)

    # .hyper extracts -> CSV (backend reads CSV/Excel, not hyper binaries);
    # multi-table relationship extracts flatten via the relationship graph
    converted = []
    for p in extracted:
        if p.lower().endswith((".hyper", ".tde")):
            c = hyper_to_csv(p, relationships=rels)
            if c:
                converted.append(c)
    # extract filenames encode their datasource: federated_<id>.hyper belongs
    # to datasource name federated.<id> -- match converted CSVs that way
    dsmap = TP.datasource_map(root)          # federated.<id> -> caption
    by_ds_caption = {}
    for c in converted:
        stem = os.path.splitext(os.path.basename(c))[0]
        if stem.startswith("federated_"):
            cap = dsmap.get("federated." + stem[len("federated_"):])
            if cap:
                by_ds_caption[cap] = c
    live = TP.live_connections(root)
    csql = TP.custom_sql_sources(root)
    mapping = {}
    problems = []
    unclaimed = [c for c in converted if c not in by_ds_caption.values()]
    for cap, files in datasource_files(root).items():
        if cap in auto_bound:
            # R3: the source table already exists in Snowflake -- point at the
            # governed original rather than copying this workbook's extract.
            # Outranks the local file deliberately (that is the whole feature).
            mapping[cap] = {"table": auto_bound[cap], "local_file": None,
                            "live": True, "auto_bound": True}
            continue
        # THIS workbook's own extract outranks anything already in data/
        own = by_ds_caption.get(cap) or extract_for_caption(converted, cap)
        local = own or pick_local_file(files)
        if local is None and len(unclaimed) == 1 and len(datasource_files(root)) == 1:
            local = unclaimed.pop(0)     # single extract, single datasource
        info = live.get(cap)
        csinfo = csql.get(cap)
        if local is None and csinfo and csinfo["queryable"]:
            # LIVE custom-SQL datasource on Snowflake -- the SQL text is
            # already valid Snowflake SQL, run it verbatim as a derived table.
            mapping[cap] = {"table": f"({csinfo['sql']}) AS {to_phys(cap)}_CSQL",
                            "local_file": None, "live": True, "custom_sql": True}
            continue
        if local is None and info and info["queryable"]:
            # LIVE connection straight to Snowflake, single named table, no
            # join/custom-SQL -- point at the source's OWN db.schema.table
            # (genuinely live, no copy) instead of the usual dev DB target.
            mapping[cap] = {"table": f"{info['dbname']}.{info['schema']}.{info['table']}",
                            "local_file": None, "live": True}
            continue
        table = f"{a.db}.{a.schema}.{to_phys(cap)}"
        mapping[cap] = {"table": table, "local_file": local}
        if local is None:
            if csinfo and not csinfo["queryable"]:
                problems.append(f"  ! '{cap}': custom-SQL datasource -- {csinfo['reason']}")
            elif info and not info["queryable"]:
                problems.append(f"  ! '{cap}': live connection ({info['class']}) -- "
                                f"{info['reason']}")
            else:
                problems.append(f"  ! '{cap}': no local file found (connection says "
                                f"{files or 'n/a'}) -- put it in data/ and edit {a.out}")
        elif local.lower().endswith((".xls",)):
            problems.append(f"  ~ '{cap}': matched {local}; .xls needs the xlrd "
                            f"package (or export it to .csv)")

    # UNION datasources: combine ALL member files row-wise into one CSV so the
    # app queries the whole union, not just the single file pick_local_file
    # happened to grab (which silently dropped the other members' rows).
    for cap, members in TP.union_members(root).items():
        if cap in auto_bound:
            continue                 # already bound to a real table -- nothing to union
        paths = [os.path.join("data", m) for m in members
                 if os.path.exists(os.path.join("data", m))]
        if len(paths) >= 2:
            out = os.path.join("data", to_phys(cap) + "__union.csv")
            if materialize_union(paths, out):
                mapping[cap] = {"table": f"{a.db}.{a.schema}.{to_phys(cap)}",
                                "local_file": out, "union": True}
                print(f"unioned {len(paths)} files -> {out}")

    existing.update(mapping)           # this workbook's entries win for itself
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)

    print(f"\nWrote {a.out} (merged; {len(existing)} datasources total):")
    for cap, m in mapping.items():
        tag = "  [auto-bound, no copy]" if m.get("auto_bound") else ""
        print(f"  {cap:<24} -> {m['table']:<40} local={m['local_file']}{tag}")
    # Report what was NOT auto-bound too (ambiguous / column mismatch): the
    # honesty half of R3 -- a match we refused must be visible, not silent.
    if auto_reports:
        print("\nSource-table binding:")
        for cap, _fq, status, note in auto_reports:
            print(f"  {'ok ' if status == 'bound' else '!  '}{cap}: {note}")
    if problems:
        print("\nNeeds attention:")
        for p in problems:
            print(p)
    print("\nNext:")
    print(f"  python tableau_parser.py {a.twb} -o workbook_ir.json")
    print("  python codegen.py workbook_ir.json -o app.py")
    print(f"  python report.py {a.twb}")


if __name__ == "__main__":
    main()

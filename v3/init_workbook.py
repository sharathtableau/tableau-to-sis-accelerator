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


def _orient_multi_fact(tables, rels):
    """DM4: a multi-fact relationship tree (two or more tables nothing joins TO, e.g. Orders -> Product <- Targets).
    The largest fact (ties: name) is flattened with every table it reaches; each other fact becomes a related table of
    role 'fact' linked to the ONE table of the flattened part it relates to (the shared dimension) -- never joined,
    which would multiply the flattened rows; a table reached only through another fact is left out. A fact linked to
    the flattened part by more than one relationship is refused (ambiguous)."""
    from semantic_layer import join_plan
    seconds = {r["second"] for r in rels}
    roots = sorted((t for t in tables if t not in seconds), key=lambda t: (-len(tables[t]), t))
    keep, frontier = {roots[0]}, [roots[0]]
    while frontier:
        cur = frontier.pop()
        for r in rels:
            if r["first"] == cur and r["second"] not in keep:
                keep.add(r["second"])
                frontier.append(r["second"])
    links = []
    for other in roots[1:]:
        into = [r for r in rels if r["first"] == other and r["second"] in keep]
        if len(into) != 1:
            raise ValueError(f"{_obj_caption(other)}: a fact table must relate to the other fact's tables exactly once")
        r = into[0]
        links.append({"table": other, "rows": len(tables[other]), "role": "fact", "parent": r["second"],
                      "parent_key": r["rkey"], "key": r["lkey"],
                      "reason": f"another fact table (relates to {_obj_caption(r['second'])})"})
    for t in tables:
        if t not in keep and t not in roots:
            links.append({"table": t, "rows": len(tables[t]), "reason": "reached only through another fact table",
                          "parent": None})
    kept_rels = [r for r in rels if r["first"] in keep and r["second"] in keep]
    kept = {t: v for t, v in tables.items() if t in keep}
    plan = join_plan({t: {"caption": _obj_caption(t)} for t in kept}, kept_rels)
    return dict(kept, **{e["table"]: tables[e["table"]] for e in links}), kept_rels, plan, links


def _composite_pairs(r):
    """E04: a relationship on several fields (<expression op='AND'> of equalities) -> [(left, right)], else None."""
    expression = r.find('expression')
    endpoints = [r.find('first-end-point'), r.find('second-end-point')]
    if (expression is None or expression.attrib != {'op': 'AND'} or len(expression) < 2 or len(r) != 3
            or any(e is None or not e.get('object-id') for e in endpoints)):
        return None
    pairs = []
    for eq in expression:
        if (eq.tag != 'expression' or eq.attrib != {'op': '='} or len(eq) != 2
                or any(e.tag != 'expression' or len(e) or set(e.attrib) != {'op'}
                       or not re.fullmatch(r'\[[^\[\]]+\]', e.get('op', '')) for e in eq)):
            return None
        pairs.append((eq[0].get('op').strip('[]'), re.sub(r"\s*\([^)]*\)$", "", eq[1].get('op').strip('[]'))))
    if len({p[0] for p in pairs}) != len(pairs) or len({p[1] for p in pairs}) != len(pairs):
        return None
    return pairs


def parse_relationships(root, strict=False, composite=False):
    """Tableau relationship graph (object model) -> list of
    {first, second, lkey, rkey} (+ lkeys, rkeys for a composite key). Keys come back caption-style:
    the dim-side key may carry Tableau's collision rename
    ('customer_id (Customers)') -- rkey is the PHYSICAL dim column.
    E04: with composite=True a relationship on several fields (AND of equalities) is returned with every pair in
    lkeys / rkeys (lkey / rkey = the first pair); consumers that join on one key never ask for it, so for them a
    composite relationship still refuses in strict mode (no partial model)."""
    rels = []
    for r in root.iter():
        # Object-model relationships only ('relationship', or a Tableau feature-flag prefixed
        # '...relationship'). '<datasource-relationship>' is a data-blending link between two
        # datasources, not a join inside one; it is handled (or refused) by the blending path.
        if not (isinstance(r.tag, str) and r.tag.rsplit(".", 1)[-1] == "relationship"):
            continue
        pairs = _composite_pairs(r) if composite else None
        if pairs:
            ends = [r.find('first-end-point').get('object-id'), r.find('second-end-point').get('object-id')]
            rels.append({"first": ends[0], "second": ends[1], "lkey": pairs[0][0], "rkey": pairs[0][1],
                         "lkeys": [p[0] for p in pairs], "rkeys": [p[1] for p in pairs]})
            continue
        if strict:
            expression = r.find('expression')
            endpoints = [r.find('first-end-point'), r.find('second-end-point')]
            if (expression is None or expression.attrib != {'op': '='}
                    or len(expression) != 2
                    or any(e.tag != 'expression' or len(e) or set(e.attrib) != {'op'}
                           or not re.fullmatch(r'\[[^\[\]]+\]', e.get('op', '')) for e in expression)
                    or any(e is None or not e.get('object-id') for e in endpoints)
                    or len(r) != 3):
                raise ValueError('Relationship extract requires a simple equality between two declared fields')
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


def metadata_object_id(record):
    """R47: the logical table a <metadata-record> belongs to. Tableau 2024+ writes the element as
    <_.fcp.ObjectModelEncapsulateLegacy.true...object-id> (a feature-flag prefix); older files as <object-id>.
    Both name the same thing; reading only the plain tag lost every field's owner (Superstore 2024.3 could not be
    bound or loaded as a relationship model)."""
    for child in record:
        tag = child.tag if isinstance(child.tag, str) else ''
        if tag == 'object-id' or tag.endswith('...object-id'):
            return (child.text or '').strip().strip('[]')
    return ''


def _obj_caption(object_id):
    """'Customers_45D1687D8E0B46B7...' -> 'Customers'."""
    return re.sub(r"_[0-9A-F]{16,}$", "", object_id or "", flags=re.I)


HYPER_CACHE_ENV = "TABLEAU_V3_HYPER_CACHE"


def _hyper_cache_key(hyper_path):
    """The extract's own bytes, the Hyper API version and this reader's code: any of them changes -> a new key."""
    import hashlib
    import inspect
    h = hashlib.sha256()
    with open(hyper_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    try:
        import tableauhyperapi
        h.update(str(getattr(tableauhyperapi, "__version__", "")).encode())
    except ImportError:
        pass
    h.update(inspect.getsource(_decode_hyper_tables).encode())
    return h.hexdigest()


def _decode_hyper_tables(hyper_path, HyperProcess, Connection, Telemetry):
    """Every table of a .hyper extract as [(table name, DataFrame)], in catalog order (Tableau's engine)."""
    import pandas as pd
    out = []
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
        with Connection(hp.endpoint, hyper_path) as con:
            for schema in con.catalog.get_schema_names():
                for t in con.catalog.get_table_names(schema):
                    cols = [c.name.unescaped
                            for c in con.catalog.get_table_definition(t).columns]
                    rows = con.execute_list_query(f"SELECT * FROM {t}")
                    out.append((t.name.unescaped, pd.DataFrame(rows, columns=cols)))
    return out


def _read_hyper_tables(hyper_path, HyperProcess, Connection, Telemetry):
    """R47: decoding an extract is most of a test's time and every gate decodes the same corpus extracts again.
    Only when TABLEAU_V3_HYPER_CACHE names a folder (the release gates set it; the product never does), the decoded
    tables are kept there under a key of the extract's bytes + Hyper API version + the decoder's code, written
    atomically (parallel gate workers). Everything after the decode -- flattening, keys, CSV -- runs every time."""
    import pickle
    folder = os.environ.get(HYPER_CACHE_ENV)
    if not folder:
        return _decode_hyper_tables(hyper_path, HyperProcess, Connection, Telemetry)
    try:
        path = os.path.join(folder, _hyper_cache_key(hyper_path) + ".pkl")
        if os.path.isfile(path):
            with open(path, "rb") as f:
                out = pickle.load(f)
            os.utime(path)                      # recently used: kept by the gates' pruning
            return out
    except Exception:
        path = None
    out = _decode_hyper_tables(hyper_path, HyperProcess, Connection, Telemetry)
    if path:
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except Exception:
            pass
    return out


def hyper_to_csv(hyper_path, outdir="data", relationships=None, strict=False, evidence=None):
    """See _hyper_to_csv. R47: under TABLEAU_V3_HYPER_CACHE (release gates only) the whole result -- the CSV, the
    left-out tables' CSVs and the evidence -- is also kept, keyed by the extract, the Hyper API, this module's code
    and the arguments; any change to the conversion code converts again."""
    folder = os.environ.get(HYPER_CACHE_ENV)
    try:
        import tableauhyperapi  # noqa: F401  -- without Tableau's engine nothing converts, cached or not
    except ImportError:
        folder = None
    if not folder:
        return _hyper_to_csv(hyper_path, outdir, relationships, strict, evidence)
    import hashlib
    import inspect
    import json as _json
    import pickle
    import sys as _sys
    stem = os.path.splitext(os.path.basename(hyper_path))[0]
    try:
        key = hashlib.sha256((_hyper_cache_key(hyper_path) + inspect.getsource(_sys.modules[__name__])
                              + _json.dumps(relationships, sort_keys=True, default=str) + repr(strict)
                              + _json.dumps(evidence, sort_keys=True, default=str)).encode()).hexdigest()
        path = os.path.join(folder, "csv_" + key + ".pkl")
    except Exception:
        return _hyper_to_csv(hyper_path, outdir, relationships, strict, evidence)
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                hit = pickle.load(f)
            os.utime(path)
            os.makedirs(outdir, exist_ok=True)
            for name, data in hit["files"].items():
                with open(os.path.join(outdir, stem + name), "wb") as f:
                    f.write(data)
            if evidence is not None:
                ev = hit["evidence"]
                for e in ev.get("left_out") or []:
                    if e.get("file"):
                        e["file"] = os.path.join(outdir, stem + e["file"])
                evidence.clear()
                evidence.update(ev)
            if hit["log"]:
                print(hit["log"], end="")
            return os.path.join(outdir, stem + ".csv") if hit["out"] else None
        except Exception:
            pass
    import contextlib
    import copy
    import io
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        out = _hyper_to_csv(hyper_path, outdir, relationships, strict, evidence)
    print(log.getvalue(), end="")
    try:
        files = {}
        if out:
            for f in os.listdir(outdir):
                full = os.path.join(outdir, f)
                if f == stem + ".csv" or (f.startswith(stem + "__") and f.endswith(".csv")):
                    with open(full, "rb") as fh:
                        files[f[len(stem):]] = fh.read()
        ev = copy.deepcopy(evidence) if evidence is not None else None
        for e in (ev or {}).get("left_out") or []:
            if e.get("file"):
                e["file"] = os.path.basename(e["file"])[len(stem):]
        os.makedirs(folder, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"files": files, "evidence": ev, "log": log.getvalue(), "out": bool(out)}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        pass
    return out


def _hyper_to_csv(hyper_path, outdir="data", relationships=None, strict=False, evidence=None):
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
    tables = {}
    for name, frame in _read_hyper_tables(hyper_path, HyperProcess, Connection, Telemetry):
        if strict and name in tables:
            raise ValueError('Extract table names collide across schemas')
        tables[name] = frame
    if not tables:
        return None
    out = os.path.join(outdir,
                       os.path.splitext(os.path.basename(hyper_path))[0] + ".csv")

    df, note = flatten_tables(tables, relationships, strict=strict, evidence=evidence)
    if note:
        print(note)
    df.to_csv(out, index=False)
    for e in (evidence or {}).get("left_out") or []:
        # DM4: a related table left out of the flatten keeps its own rows (queried per table, never joined flat)
        side = os.path.join(outdir, os.path.splitext(os.path.basename(hyper_path))[0] + "__"
                            + re.sub(r"[^0-9A-Za-z]+", "_", _obj_caption(e["table"])).strip("_") + ".csv")
        tables[e["table"]].to_csv(side, index=False)
        e["file"] = side
    print(f"  converted {hyper_path} -> {out}  ({len(df)} rows)")
    return out


def _merge_composite(df, tables, r, step, renamed, strict, checks, log):
    """E04: LEFT JOIN a lookup on every pair of a composite relationship key (fact grain kept, keys unique,
    types equal per pair); the dimension's own key columns stay available as 'key (Table)' like a single key."""
    dim = tables[r["second"]].copy()
    dcap = _obj_caption(r["second"])
    rkeys = list(r["rkeys"])
    lkeys = [renamed.get((step["parent_id"], k), k) for k in r["lkeys"]]
    if strict and (any(k not in dim.columns for k in rkeys) or any(k not in df.columns for k in lkeys)):
        raise ValueError('Relationship key is missing')
    if strict:
        keyframe = dim[rkeys]
        if keyframe.isna().any().any() or keyframe.duplicated().any():
            raise ValueError(f"Composite relationship key of {dcap} is null or repeats; joining would multiply "
                             "the fact rows")
        for a, b in zip(lkeys, rkeys):
            if df[a].dtype != dim[b].dtype:
                raise ValueError('Relationship key types differ')
    ren = {c: f"{c} ({dcap})" for c in dim.columns if c in df.columns}
    dim = dim.rename(columns=ren)
    for c, new in ren.items():
        renamed[(r["second"], c)] = new
    right_on = [ren.get(k, k) for k in rkeys]
    if strict and dim.columns.duplicated().any():
        raise ValueError('Relationship column renaming is ambiguous')
    before_rows = len(df)
    probe = df[lkeys].merge(dim[right_on].assign(__hit=1), how="left", left_on=lkeys, right_on=right_on)
    out = df.merge(dim, how="left", left_on=lkeys, right_on=right_on, **({'validate': 'many_to_one'} if strict else {}))
    if strict and (len(out) != before_rows or out.columns.duplicated().any()):
        raise ValueError('Relationship merge changed fact grain or produced ambiguous columns')
    checks.append({'parent': step['parent_id'], 'dimension': r['second'], 'parent_key': lkeys,
                   'dimension_key': right_on, 'unmatched_rows': int(probe['__hit'].isna().sum()),
                   'dimension_rows': len(dim)})
    log.append(f"    joined {dcap} on {_obj_caption(step['parent_id'])}.({', '.join(lkeys)}) ({len(dim)} rows)")
    return out


def flatten_tables(tables, relationships, strict=False, evidence=None):
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
    checks = []
    if strict:
        if not tables or any(frame.columns.duplicated().any() for frame in tables.values()):
            raise ValueError('Extract requires nonempty table inventory and unique column names')
        for rel in relationships or []:
            if (rel['first'] in tables) != (rel['second'] in tables):
                raise ValueError('Relationship endpoint is absent from this extract')
    if len(tables) == 1:
        if strict and any(rel['first'] in tables or rel['second'] in tables for rel in relationships or []):
            raise ValueError('Single table extract cannot realize a declared relationship')
        if evidence is not None:
            evidence.update(shape='single', table_rows={k: len(v) for k, v in tables.items()},
                            output_rows=len(next(iter(tables.values()))), edges=checks)
        return next(iter(tables.values())), ""
    rels = [r for r in (relationships or [])
            if r["first"] in tables and r["second"] in tables]
    plan = join_plan({t: {"caption": _obj_caption(t)} for t in tables}, rels)
    fact_links = []                            # DM4: other fact tables of a multi-fact model (never joined)
    if strict and plan.get("shape") == "multi_fact" and len(rels) == len(tables) - 1:
        if any(len(r.get("lkeys") or [1]) > 1 for r in rels):
            raise ValueError('A multi-fact model with a composite relationship key is not supported yet')
        tables, rels, plan, fact_links = _orient_multi_fact(tables, rels)
    if not plan["steps"]:
        if strict:
            raise ValueError('Incomplete relationship plan: ' + str(plan.get('reason')))
        df = max(tables.values(), key=len)
        return df, (f"  !! {len(tables)} tables -- {plan['reason']}. Dumped the "
                    f"largest table ONLY; fields from other tables will not "
                    f"resolve. Model the joins in Snowflake views instead.")
    df = tables[plan["root"]]
    if strict and len(plan['steps']) != len(tables) - 1 - len(fact_links):
        raise ValueError('Relationship plan does not cover every table')
    # A column of the PARENT may have been renamed 'col (Parent)' when it collided
    # on an EARLIER merge; a snowflake chain then joins ON that renamed column.
    # Track where each table's columns ended up so a depth>1 join resolves against
    # the accumulated frame instead of a name that no longer exists.
    renamed = {}                              # (table_id, original col) -> current
    left_out = []                             # E04: lookup tables NOT joined (and why)
    skipped = {e["table"] for e in fact_links}
    for step in plan["steps"]:
        r = step["rel"]
        if step["parent_id"] in skipped:
            # reached only through a table that was left out: no exact path to the fact
            skipped.add(r["second"])
            left_out.append({"table": r["second"], "rows": len(tables[r["second"]]),
                             "reason": f"reached only through {_obj_caption(step['parent_id'])}"})
            log.append(f"    left out {_obj_caption(r['second'])} (reached only through "
                       f"{_obj_caption(step['parent_id'])})")
            continue
        if r.get("lkeys") and len(r["lkeys"]) > 1:
            # E04: a relationship on several fields -- joined on all of them, under the same exactness rules
            df = _merge_composite(df, tables, r, step, renamed, strict, checks, log)
            continue
        if strict and r["rkey"] in tables[r["second"]].columns:
            keys = tables[r["second"]][r["rkey"]]
            if keys.notna().all() and keys.duplicated().any():
                # E04: a lookup whose keys repeat would multiply the fact's rows (inflated measures). Tableau
                # queries a related table only when a sheet uses it, so leave it OUT: sheets that do not use its
                # fields keep Tableau's numbers; a sheet that does refuses (its fields do not resolve).
                skipped.add(r["second"])
                left_out.append({"table": r["second"], "rows": len(keys), "distinct_keys": int(keys.nunique()),
                                 "reason": f"relationship key '{r['rkey']}' repeats",
                                 # DM4: how it links to the flattened table (the parent's key column as it is
                                 # named after earlier merges), so a relationship query can use it
                                 "parent": step["parent_id"], "parent_key": renamed.get((step["parent_id"], r["lkey"]),
                                                                                       r["lkey"]),
                                 "key": r["rkey"]})
                log.append(f"    left out {_obj_caption(r['second'])}: key '{r['rkey']}' repeats "
                           f"({len(keys)} rows, {keys.nunique()} keys) -- joining would multiply the fact rows")
                continue
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
        # Same-named keys merge into the parent's column, which loses the dimension's own key.
        # Tableau exposes that key as 'key (Table)'; keep a copy under exactly that name so a
        # sheet using the dimension-side key resolves (null where the fact row has no match).
        if rkey == lkey and f"{rkey} ({dcap})" not in df.columns and f"{rkey} ({dcap})" not in dim.columns:
            dim[f"{rkey} ({dcap})"] = dim[rkey]
        if strict:
            if lkey not in df.columns or rkey not in dim.columns:
                raise ValueError('Relationship key is missing')
            if dim.columns.duplicated().any():
                raise ValueError('Relationship column renaming is ambiguous')
            if dim[rkey].isna().any() or dim[rkey].duplicated().any():
                raise ValueError('Relationship dimension keys must be unique and nonnull')
            if df[lkey].dtype != dim[rkey].dtype:
                raise ValueError('Relationship key types differ')
            unexpected = (set(df.columns) & set(dim.columns)) - ({lkey} if lkey == rkey else set())
            if unexpected:
                raise ValueError('Relationship columns collide after renaming')
            checks.append({'parent': step['parent_id'], 'dimension': r['second'],
                           'parent_key': lkey, 'dimension_key': rkey,
                           'unmatched_rows': int((~df[lkey].isin(dim[rkey])).sum()),
                           'dimension_rows': len(dim)})
        if lkey not in df.columns:
            log.append(f"  !! join key '{lkey}' not present after earlier merges "
                       f"-- stopping the flatten here rather than guessing "
                       f"(fields from {dcap} will not resolve).")
            break
        before_rows = len(df)
        df = df.merge(dim, how="left", left_on=lkey, right_on=rkey,
                      **({'validate': 'many_to_one'} if strict else {}))
        if strict and (len(df) != before_rows or df.columns.duplicated().any()):
            raise ValueError('Relationship merge changed fact grain or produced ambiguous columns')
        log.append(f"    joined {dcap} on "
                   f"{_obj_caption(step['parent_id'])}.{lkey} ({len(dim)} rows)")
    for e in fact_links:                       # resolve each other fact's link to the flattened column name
        if e.get("role") == "fact":
            e["parent_key"] = renamed.get((e["parent"], e["parent_key"]), e["parent_key"])
            log.append(f"    left out {_obj_caption(e['table'])}: another fact table (queried per table)")
    left_out = fact_links + left_out
    log.append(f"    flattened {len(tables)} tables around fact "
               f"'{_obj_caption(plan['root'])}' (relationship {plan['shape']})")
    if evidence is not None:
        evidence.update(shape=plan['shape'], root=plan['root'],
                        table_rows={k: len(v) for k, v in tables.items()},
                        output_rows=len(df), edges=checks, left_out=left_out,
                        scope='Local extract row preservation; worksheet and Tableau parity unverified')
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


def datasource_paths(root):
    """Datasource caption -> the relative file paths its connections name (directory + filename, or a filename /
    dbname that carries a path), forward slashes. DM1: lets a bundled file be matched by its archive path when two
    bundled files share a basename."""
    out = {}
    for ds in root.findall("./datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters":
            continue
        paths = []
        for c in ds.findall(".//connection"):
            for attr in ("filename", "dbname"):
                fn = (c.get(attr) or "").replace("\\", "/")
                if not fn or "." not in os.path.basename(fn):
                    continue
                d = (c.get("directory") or "").replace("\\", "/").strip("/")
                paths.append(f"{d}/{fn}" if d and "/" not in fn else fn)
        out[cap] = paths
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

"""
pipeline.py  --  the accelerator's stage logic, UI-free.

Single source of truth for the onboard/decode/match/load steps so the
command-line path (convert.py), the classic converter (converter_app.py) and
the staged demo UI (pipeline_app.py) all run IDENTICAL logic. This project has
been bitten before by two decode paths diverging (the converter dumped the
largest hyper table only while init_workbook star-flattened) -- keeping the
logic here, imported everywhere, prevents that class of bug.

No `streamlit` import: these functions are pure and testable. UI lives in the
*_app.py files that call them.
"""

import io
import os
import re
import zipfile

import config
import engine
import tableau_parser as TP
from backend import _read_source_file, _normalize_columns
from calc_translator import to_phys

# Target database/schema for tables the converter auto-creates in Snowflake.
# WBR_DB already exists and this account's app-owner role can use it; a
# DEDICATED schema keeps demo uploads isolated from WBR_DB.PUBLIC, which holds
# the real corpus tables the deployed E-Commerce app and the Cortex semantic
# views (SUPERSTORE_SEMANTIC etc.) depend on -- write_pandas(overwrite=True)
# on a re-uploaded Superstore.twbx into PUBLIC would silently replace them.
LOAD_DB = "WBR_DB"
LOAD_SCHEMA = "PIPELINE_DEMO"
DATA_EXT = {".csv", ".xls", ".xlsx", ".txt", ".tsv"}

# Internal stage the human-gated Deploy button stages a generated app into
# before CREATE STREAMLIT. Fully-qualified as "{LOAD_DB}"."{LOAD_SCHEMA}".<this>.
DEPLOY_STAGE = "STREAMLIT_STAGE"

# Files a generated STANDALONE app (`app_<stem>.py`, which does
# `from engine import run`) needs at runtime in Snowflake. This is exactly the
# artifact set snowflake.yml's superstore_app / tableau_migration_app entities
# ship (engine -> backend, config, calc_translator, findings; config ->
# profile_superstore, profile_default) -- kept in sync with that manifest.
# datasources.json is written per-deploy (points at the loaded tables), so it
# is NOT listed here.
APP_RUNTIME_MODULES = ["engine.py", "backend.py", "config.py",
                       "calc_translator.py", "findings.py",
                       "profile_superstore.py", "profile_default.py",
                       "environment.yml"]


def get_session():
    """The active Snowpark session when running in Snowflake, else None."""
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        return None


def _cli_config_paths():
    """Where the `snow` CLI may store connections (config.toml). Ordered."""
    paths = []
    home = os.environ.get("SNOWFLAKE_HOME")
    if home:
        paths.append(os.path.join(home, "config.toml"))
    la = os.environ.get("LOCALAPPDATA")           # Windows: %LOCALAPPDATA%\snowflake
    if la:
        paths.append(os.path.join(la, "snowflake", "config.toml"))
    paths.append(os.path.expanduser("~/.snowflake/config.toml"))
    paths.append(os.path.expanduser("~/.config/snowflake/config.toml"))
    return paths


def read_cli_connection(name):
    """Read a named connection's params from the `snow` CLI config.toml. The CLI
    stores connections in config.toml under [connections.<name>]; the bare
    Snowpark/connector `connection_name` resolver reads connections.toml, which
    may not exist -- so we read config.toml ourselves to be robust. Returns a
    params dict (account/user/authenticator/...) or None. Lazy tomllib import
    keeps this module importable on pre-3.11 runtimes (e.g. a SiS sandbox)."""
    try:
        import tomllib
    except Exception:
        return None
    for p in _cli_config_paths():
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                conns = tomllib.load(f).get("connections", {})
        except Exception:
            continue
        if name in conns:
            return dict(conns[name])
    return None


def snow_session(connection_name="wbr"):
    """Build a Snowpark session from a named `snow` CLI connection, for running
    this migrator LOCALLY but connected to Snowflake -- so a hyper-only workbook
    can decode HERE (laptop has the Hyper engine) and load UP THERE in a single
    upload. Tries the connector's native connection_name resolution first, then
    falls back to the CLI's config.toml params. May open a browser for SSO."""
    from snowflake.snowpark import Session
    try:
        return Session.builder.config("connection_name", connection_name).create()
    except Exception:
        params = read_cli_connection(connection_name)
        if not params:
            raise RuntimeError(
                f"Snowflake connection '{connection_name}' not found "
                "(checked connections.toml and the snow CLI config.toml). "
                "Run `snow connection list` to see configured names.")
        return Session.builder.configs(params).create()


def fqn(caption, db=LOAD_DB, schema=LOAD_SCHEMA):
    return f"{db}.{schema}.{to_phys(caption)}"


def table_exists(session, db, schema, table):
    """Fully-qualified existence probe (NO `USE` -- see ensure_target for why
    session-context statements are banned inside a SiS app). Used to REUSE a
    pre-loaded table for a datasource whose extract cannot be decoded inside
    Snowflake (.hyper has no in-Snowflake decoder), instead of pointing the
    sheets at a table that was never created."""
    try:
        n = session.sql(
            f'SELECT COUNT(*) AS N FROM "{db}".INFORMATION_SCHEMA.TABLES '
            f"WHERE TABLE_SCHEMA = '{schema.upper()}' "
            f"AND TABLE_NAME = '{table.upper()}'").collect()[0]["N"]
        return bool(n)
    except Exception:
        return False


def resolve_existing_table(session, db, schema, table):
    """Find where <table> ACTUALLY exists in <db>, to reuse a pre-loaded table
    for a datasource whose extract can't be decoded inside Snowflake (.hyper).

    The old probe only checked LOAD_SCHEMA (PIPELINE_DEMO). But a table can
    already live elsewhere in the account -- the corpus `load_snowflake.py`
    loads every demo table into WBR_DB.PUBLIC, and the E-Commerce
    'Customers (DataDNA ...)' relationship extract is exactly that case: present
    in PUBLIC, absent from PIPELINE_DEMO, so Stage 1 wrongly reported MISSING.

    Resolution order + honesty boundary (the same confident-match-only rule as
    R3 / the per-workbook routing fix):
      1. prefer LOAD_SCHEMA (a copy this app itself loaded wins);
      2. else search EVERY schema in <db> for the exact table name; a SINGLE hit
         is reused; a name in >1 schema is AMBIGUOUS and surfaced (not bound),
         so we never silently point at a same-named foreign table (the
         Superstore-gravity wrong-table class).
    Returns (fqn, note) on a confident match, else (None, reason_or_None)."""
    if table_exists(session, db, schema, table):
        return f"{db}.{schema}.{table}", "existing (pre-loaded)"
    try:
        rows = session.sql(
            f'SELECT TABLE_SCHEMA AS S FROM "{db}".INFORMATION_SCHEMA.TABLES '
            f"WHERE TABLE_NAME = '{table.upper()}'").collect()
    except Exception:
        rows = []
    schemas = []
    for r in rows:                       # tolerate odd row shapes -- never crash
        try:                             # Stage 1 on a malformed metadata row
            schemas.append(r["S"])
        except (KeyError, TypeError, IndexError):
            pass
    if len(schemas) == 1:
        return f"{db}.{schemas[0]}.{table}", f"existing (reused from {schemas[0]})"
    if len(schemas) > 1:
        return None, (f"ambiguous — table {table} exists in {len(schemas)} schemas "
                      f"({', '.join(schemas)}); not auto-bound to avoid the wrong "
                      "table. Load it into "
                      f"{db}.{schema} (preload_demo.py) or qualify the target.")
    return None, None


def fqn_exists(session, fqn):
    """Existence of a fully-qualified DB.SCHEMA.OBJECT (table OR view --
    INFORMATION_SCHEMA.TABLES lists both). Fully-qualified, never a `USE`."""
    parts = [p for p in fqn.replace('"', '').split(".") if p]
    if len(parts) != 3:
        return False
    d, s, o = parts
    try:
        n = session.sql(
            f'SELECT COUNT(*) AS N FROM "{d}".INFORMATION_SCHEMA.TABLES '
            f"WHERE TABLE_SCHEMA = '{s.upper()}' AND TABLE_NAME = '{o.upper()}'"
        ).collect()[0]["N"]
        return bool(n)
    except Exception:
        return False


def table_columns_in_snowflake(session, fqn):
    """Actual column names of a fully-qualified table/view, UPPERCASED.
    Fully-qualified INFORMATION_SCHEMA read -- never a `USE`. Empty set on any
    failure, which the caller treats as 'cannot verify' (never as 'verified')."""
    parts = [p for p in (fqn or "").replace('"', '').split(".") if p]
    if len(parts) != 3:
        return set()
    d, s, o = parts
    try:
        rows = session.sql(
            f'SELECT COLUMN_NAME AS C FROM "{d}".INFORMATION_SCHEMA.COLUMNS '
            f"WHERE TABLE_SCHEMA = '{s.upper()}' AND TABLE_NAME = '{o.upper()}'"
        ).collect()
    except Exception:
        return set()
    cols = set()
    for r in rows:                       # tolerate odd row shapes -- never crash
        try:
            cols.add(str(r["C"]).upper())
        except (KeyError, TypeError, IndexError):
            pass
    return cols


def _columns_cover(session, fqn, declared):
    """Does <fqn> actually look like the table the workbook was built on?

    THE WRONG-TABLE GUARD. Matching on NAME ALONE is precisely the failure this
    project has been burned by twice (the Superstore-gravity bug, where every
    workbook silently queried the dev Superstore table; and the Cortex
    foreign-table bug, where a same-columned table from another schema was
    picked). A name is not evidence. So before any INFERRED bind, the candidate's
    real columns must COVER what the workbook's own metadata-records say the
    source had.

    Returns (ok, missing_sample). Cannot-verify (no columns readable, or the
    workbook declared none) returns ok=False -- absence of evidence is never
    treated as evidence, so an unverifiable candidate falls through to the normal
    decode+load path rather than being bound on a hunch."""
    want = {to_phys(c) for c in (declared or [])}
    if not want:
        return False, ["<workbook declared no source columns to verify against>"]
    have = table_columns_in_snowflake(session, fqn)
    if not have:
        return False, ["<could not read the candidate table's columns>"]
    missing = sorted(want - have)
    return (not missing), missing[:6]


def resolve_source_binding(session, cap, info, db=LOAD_DB, schema=LOAD_SCHEMA,
                           source_map=None):
    """ROADMAP R3: can this datasource point at a table that ALREADY exists in
    Snowflake, instead of decoding its extract and write_pandas-ing a copy?

    Generalizes two things already built: the live-Snowflake routing
    (live_connections -> point at the source's own db.schema.table) and
    load_into_snowflake's reuse probe (resolve_existing_table -> reuse a
    pre-loaded table). The NEW case is an EXTRACT-based workbook whose upstream
    table nonetheless already lives in the account -- previously always decoded
    and copied, because nothing ever looked at where the extract came FROM.

    CONFIDENCE TIERS, strongest first. Only a confident match binds:
      0. sources.json explicitly maps this caption -> a human said so. Existence
         is still probed (a stale map must fail loudly, not bind to nothing) and
         columns are still checked, but a mismatch WARNS rather than refuses --
         overriding the inference is the whole point of the file.
      1. The workbook's OWN declared location (dbname.schema.table from a
         Snowflake-class connection) exists. Strongest inference available: the
         workbook literally names this table as its source.
      2. The declared table NAME exists in exactly ONE schema of <db>. Weaker --
         a name, not a location -- so it must clear the column guard.
    AMBIGUOUS (the name in >1 schema) NEVER binds: it is surfaced as a choice,
    because silently picking one of several same-named tables is exactly the
    wrong-table class this project keeps guarding against.

    Returns (fqn|None, note, status) where status is one of:
      'bound'     -- fqn is safe to use, no copy needed
      'ambiguous' -- a human must choose; note explains
      'skipped'   -- not a single-table binding target (multi-table / custom SQL)
      'mismatch'  -- a candidate existed but is not this workbook's table
      'no-match'  -- nothing found; caller proceeds with the normal decode+load
    """
    source_map = source_map or {}
    declared_cols = (info or {}).get("columns") or []

    override = source_map.get(cap)
    if override:
        if not fqn_exists(session, override):
            return None, (f"sources.json maps '{cap}' to {override}, which does "
                          "not exist in the account — fix the map or remove the "
                          "entry"), "mismatch"
        ok, missing = _columns_cover(session, override, declared_cols)
        note = f"bound to {override} (sources.json)"
        if not ok:
            note += (" — WARNING: its columns do not cover the workbook's source "
                     f"({', '.join(missing)}); honoring the explicit map anyway")
        return override, note, "bound"

    if not info or not info.get("bindable"):
        return None, (info or {}).get("reason"), "skipped"

    t = info["tables"][0]

    # Tier 1 -- the workbook's own declared location.
    if info.get("class") == "snowflake" and info.get("dbname") and t.get("schema"):
        declared_fqn = f"{info['dbname']}.{t['schema']}.{t['name']}"
        if fqn_exists(session, declared_fqn):
            ok, missing = _columns_cover(session, declared_fqn, declared_cols)
            if ok:
                return declared_fqn, (f"auto-bound to the workbook's declared "
                                      f"source {declared_fqn} (no copy)"), "bound"
            return None, (f"{declared_fqn} exists but its columns do not match the "
                          f"workbook's source (missing {', '.join(missing)}) — not "
                          "bound; loading the extract instead"), "mismatch"

    # Tier 2 -- the declared NAME, searched across <db>. Name-only evidence, so
    # the column guard is mandatory here, and >1 hit is never resolved for you.
    resolved, note = resolve_existing_table(session, db, schema, t["name"])
    if resolved is None:
        if note:                                     # resolve_existing_table only
            return None, note, "ambiguous"           # sets a note when ambiguous
        return None, None, "no-match"
    ok, missing = _columns_cover(session, resolved, declared_cols)
    if not ok:
        return None, (f"{resolved} matches the source table name '{t['name']}' but "
                      f"its columns do not match the workbook (missing "
                      f"{', '.join(missing)}) — not bound; loading the extract "
                      "instead"), "mismatch"
    return resolved, (f"auto-bound to existing {resolved} — matched the workbook's "
                      f"source table name '{t['name']}' and verified its columns "
                      "(no copy)"), "bound"


def auto_bind_sources(session, root, db=LOAD_DB, schema=LOAD_SCHEMA):
    """Run resolve_source_binding over every datasource in the workbook.

    Returns (bound, reports) where `bound` is {caption: fqn} for confident
    matches only, and `reports` is [(caption, fqn_or_None, status, note)] for
    every datasource that had anything to say -- so the UI can show BOTH what was
    auto-bound and what was deliberately not (ambiguous / mismatched), which is
    the honesty half of this feature."""
    bound, reports = {}, []
    if session is None:
        return bound, reports
    source_map = getattr(config, "SOURCE_MAP", {}) or {}
    # A datasource the LIVE path already handles is left alone. live_connections
    # points it at its own db.schema.table with no copy -- the same outcome R3
    # would reach -- and that path is proven end-to-end against a real account
    # (Superstore_KPI_Parameter_Dashboard_Live). Re-routing it through a second
    # mechanism would change a working workbook's behaviour for no gain.
    already_live = {c for c, i in TP.live_connections(root).items() if i.get("queryable")}
    for cap, info in TP.source_tables(root).items():
        if cap in already_live:
            continue
        try:
            fq, note, status = resolve_source_binding(
                session, cap, info, db, schema, source_map)
        except Exception as e:                 # never let a metadata read break
            fq, note, status = None, f"could not check: {e}", "no-match"
        if status == "bound" and fq:
            bound[cap] = fq
        if note:
            reports.append((cap, fq, status, note))
    return bound, reports


def _extract_create_view(ddl):
    """Pull the single `CREATE OR REPLACE VIEW ... ;` statement out of the DDL
    text semantic_layer.generate_views produces (it also carries header + skip
    comments, and session.sql runs ONE statement)."""
    i = (ddl or "").find("CREATE OR REPLACE VIEW")
    if i < 0:
        return None
    j = ddl.find(";", i)
    return ddl[i:j + 1] if j > 0 else ddl[i:]


def verify_table_candidate(session, table_entry):
    """Is ONE table dict from semantic_layer.describe_model's `tables` list safe
    to point the view at, as-is, with no copy? (fqn, ok, note).

    R10 ROOT-CAUSE FIX (2026-07-26), the verification half. `_src_table` now
    correctly resolves a table to its workbook-DECLARED location when the real
    upstream connection is Snowflake (fixed: it used to always assume a copy,
    because semantic_layer._connection() picked up the federated wrapper
    instead of the real upstream class -- see semantic_layer._connection's
    docstring). That resolution is still only a CANDIDATE: the workbook SAYING
    a table lives at db.schema.table is not proof it does, or that it's the
    SAME table the workbook was built against (the Superstore-gravity / Cortex
    foreign-table wrong-table class this project keeps guarding against).

    So a `is_declared_source=True` candidate must pass BOTH:
      1. existence (fqn_exists)
      2. its REAL INFORMATION_SCHEMA columns COVER the columns Tableau's own
         metadata-records recorded for it (_columns_cover, same guard R3 uses)
    An `is_declared_source=False` table is the pipeline's OWN assumed-copy
    location -- existence is the only question (this pipeline wrote it, with
    normalized columns, by construction)."""
    fqn = table_entry["fqn"]
    if not table_entry.get("is_declared_source"):
        return fqn, fqn_exists(session, fqn), None
    if not fqn_exists(session, fqn):
        return fqn, False, f"declared source {fqn} does not exist"
    ok, missing = _columns_cover(session, fqn, table_entry.get("columns") or [])
    if not ok:
        return fqn, False, (f"{fqn} exists but its columns do not match the "
                            f"workbook's source (missing {', '.join(missing)})")
    return fqn, True, None


def data_model_report(session, root, db=LOAD_DB, schema=LOAD_SCHEMA):
    """Per-datasource data-model summary + deployability of the join view.

    'deployable' means EVERY constituent table verifies -- whether that's an
    already-loaded scope-B copy (existence only) or, since R10, the workbook's
    OWN declared source table (existence + column-verified, never a bare name
    match). Only when ALL tables in the graph verify does the view resolve; a
    single unverified table refuses the WHOLE model rather than deploying a
    view with a dangling join -- no partial silent binds, same honesty rule as
    R3's per-caption resolver. session=None -> existence unknown (local dev)."""
    import semantic_layer as SL
    # tables the pipeline loads are ALWAYS normalized to UPPER_SNAKE
    # (_normalize_columns), so the view must reference to_phys columns.
    rep = SL.describe_model(root, db, schema, phys_source=True)
    for ds in rep:
        ds["tables_exist"] = None
        ds["deployable"] = False
        ds["table_notes"] = []
        if not (ds.get("joinable") and session is not None):
            continue
        results = [verify_table_candidate(session, t) for t in ds["tables"]]
        ds["tables_exist"] = all(ok for _fq, ok, _note in results)
        ds["deployable"] = ds["tables_exist"]
        ds["table_notes"] = [note for _fq, _ok, note in results if note]
    return rep


def deploy_model_view(session, ds_entry):
    """Deploy ONE star datasource's relationship view (CREATE OR REPLACE VIEW),
    then verify it exists. Returns the view FQN. Raises (clear message) on
    failure -- never a silent 'ran but nothing there'."""
    stmt = _extract_create_view(ds_entry.get("view_ddl"))
    if not stmt:
        raise RuntimeError(f"no CREATE VIEW DDL for datasource "
                           f"{ds_entry.get('caption')!r}")
    session.sql(stmt).collect()
    vname = stmt.split("VIEW", 1)[1].split(" AS", 1)[0].strip()
    if not fqn_exists(session, vname):
        raise RuntimeError(f"view {vname} ran but does not exist afterwards")
    return vname


def build_data_model_tables(session, root, hyper_paths, db=LOAD_DB, schema=LOAD_SCHEMA):
    """Scope B: replicate a Tableau data model as REAL Snowflake objects. For
    each STAR/snowflake-chain datasource, either (a) R10 -- every constituent
    table already verifies against the workbook's OWN declared source (see
    pipeline.verify_table_candidate): deploy the view straight at the
    originals, NO decode, NO copy; or (b) the extract can be decoded here:
    load tables SEPARATELY (not flattened) + CREATE the relationship VIEW,
    then repoint config at the view so the app AND the existing Stage-5
    validation query the real model (the view's columns are identical to the
    flatten by construction, so numbers are proven the same way, not on trust).

    (a) needs only a session (no hyper decoder -- there is nothing to decode).
    (b) requires a Snowflake session + a laptop hyper decoder (a .hyper can't
    decode in a SiS sandbox -- same constraint as the flatten path). Additive:
    callers opt in; the default onboard/flatten path is untouched. Returns
    [(ds_caption, view_fqn_or_None, note)]."""
    import init_workbook as IW
    import semantic_layer as SL

    ensure_target(session, db, schema)
    out = []
    decoded = None                            # lazy -- only decode if actually needed
    for ds in SL.describe_model(root, db, schema, phys_source=True):
        if not ds.get("joinable"):
            if ds["n_tables"] > 1:
                out.append((ds["caption"], None,
                            f"skipped -- {ds['shape']} graph: {ds.get('reason')}"))
            continue

        # R10 -- every table already verified (existence + column-checked
        # against the workbook's own declared source)? Deploy directly, no
        # decode, no copy. `verify_table_candidate` is re-run here (not just
        # read from a prior data_model_report call) so this function is
        # correct standalone, without depending on caller ordering.
        results = [verify_table_candidate(session, t) for t in ds["tables"]]
        if results and all(ok for _fq, ok, _note in results):
            view = deploy_model_view(session, ds)
            if ds["caption"] in config.DATASOURCES:
                config.DATASOURCES[ds["caption"]]["table"] = view
                if ds["caption"] == config.DEFAULT_DATASOURCE:
                    config.ORDERS = view
                    engine.ORDERS = view
            for fn in (engine.table_columns, engine._q_exec):
                try:
                    fn.clear()
                except Exception:
                    pass
            out.append((ds["caption"], view,
                        "data model bound to existing Snowflake tables -- "
                        "no decode, no copy (R10)"))
            continue

        if decoded is None:
            decoded = {}
            for hp in hyper_paths or []:
                decoded.update(IW.hyper_to_tables(hp, os.path.dirname(hp) or "."))
        if not decoded:
            out.append((ds["caption"], None,
                        "skipped -- no decodable extract and its declared "
                        "source did not verify (" +
                        "; ".join(n for _f, _o, n in results if n) + ")"))
            continue
        need = [t["caption"] for t in ds["tables"]]
        have = {c: decoded[c] for c in need if c in decoded}
        if len(have) != len(need):
            out.append((ds["caption"], None, "skipped -- could not decode every "
                        f"table ({sorted(set(need) - set(have))} missing)"))
            continue
        for cap, path in have.items():
            df = _normalize_columns(_read_source_file(path))
            table = to_phys(cap)
            session.write_pandas(df, table, database=db, schema=schema,
                                 auto_create_table=True, overwrite=True,
                                 quote_identifiers=False)
            date_cols = [c for c in df.columns
                         if str(df[c].dtype).startswith("datetime")]
            try:
                _fix_date_columns_session(session, db, schema, table, date_cols)
            except Exception:
                pass
        view = deploy_model_view(session, ds)
        if ds["caption"] in config.DATASOURCES:
            config.DATASOURCES[ds["caption"]]["table"] = view
            if ds["caption"] == config.DEFAULT_DATASOURCE:
                config.ORDERS = view
                engine.ORDERS = view
        for fn in (engine.table_columns, engine._q_exec):
            try:
                fn.clear()
            except Exception:
                pass
        out.append((ds["caption"], view,
                    f"data model replicated: {len(have)} separate tables + view"))
    return out


def semantic_view_exists(session, name):
    """Skip-if-exists probe for a Cortex SEMANTIC VIEW. Semantic views are NOT in
    INFORMATION_SCHEMA.TABLES, so use SHOW SEMANTIC VIEWS and match the FULLY
    QUALIFIED name (database + schema + object), case-insensitively.

    Matching on bare object name only (pre-2026-08 behavior) is a real bug:
    SHOW SEMANTIC VIEWS with no scope can surface a same-named view in a
    DIFFERENT database/schema (a prior run's stale object, or an unrelated
    workbook that stemmed to the same phys name). A bare-name match then
    reports "exists" and skips the CREATE here, while the fully-qualified
    name this function was asked about was never actually created --
    Cortex Analyst then 404s on it ("does not exist or not authorized"),
    exactly the failure this was supposed to prevent."""
    parts = name.replace('"', '').split(".")
    obj = parts[-1].upper()
    db = parts[-3].upper() if len(parts) >= 3 else None
    schema = parts[-2].upper() if len(parts) >= 2 else None
    try:
        # Scope the SHOW to the target schema when known -- narrower, faster,
        # and avoids the account-wide bare-name collision entirely, rather
        # than only avoiding it via the row-level check below.
        stmt = (f'SHOW SEMANTIC VIEWS IN SCHEMA "{db}"."{schema}"'
                if db and schema else "SHOW SEMANTIC VIEWS")
        rows = session.sql(stmt).collect()
    except Exception:
        return False
    for r in rows:
        try:
            if str(r["name"]).upper() != obj:
                continue
            if db and str(r["database_name"]).upper() != db:
                continue
            if schema and str(r["schema_name"]).upper() != schema:
                continue
            return True
        except (KeyError, TypeError, IndexError):
            pass
    return False


def bundled_data_files(twbx_bytes, outdir):
    """Extract bundled data files from a .twbx into outdir. Returns
    (basename -> path for CSV/Excel, list of extracted .hyper paths)."""
    out, hyper_paths = {}, []
    with zipfile.ZipFile(io.BytesIO(twbx_bytes)) as z:
        for n in z.namelist():
            ext = os.path.splitext(n)[1].lower()
            base = os.path.basename(n)
            if ext in DATA_EXT:
                dst = os.path.join(outdir, base)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
                out[base] = dst
            elif ext in (".hyper", ".tde"):
                dst = os.path.join(outdir, base)
                with open(dst, "wb") as f:
                    f.write(z.read(n))
                hyper_paths.append(dst)
    return out, hyper_paths


def decode_hypers_locally(hyper_paths, files, outdir, relationships=None):
    """OUTSIDE Snowflake, decode each .hyper to CSV and add to the files map.
    Returns basenames that could NOT be decoded. `relationships` MUST be
    threaded through so a 2020.2+ multi-table relationship extract flattens
    (fact LEFT JOIN dims) instead of dumping the largest table only."""
    import init_workbook as IW
    blocked = []
    for hp in hyper_paths:
        try:
            csv = IW.hyper_to_csv(hp, outdir, relationships=relationships)
        except Exception:
            csv = None
        if csv:
            files[os.path.basename(csv)] = csv
        else:
            blocked.append(os.path.basename(hp))
    return blocked


def match_files_to_datasources(root, files):
    """Datasource caption -> local file path via connection filename/dbname
    basenames (same matching init_workbook does)."""
    import init_workbook as IW
    have = {k.lower(): v for k, v in files.items()}
    mapping = {}
    for cap, basenames in IW.datasource_files(root).items():
        local = None
        for b in basenames:
            stem = os.path.splitext(b)[0].lower()
            if stem + ".csv" in have:
                local = have[stem + ".csv"]; break
            if b.lower() in have:
                local = have[b.lower()]; break
        mapping[cap] = local
    return mapping


def configure_datasources(caption_to_file, db=LOAD_DB, schema=LOAD_SCHEMA, live=None,
                          custom_sql=None, auto_bound=None):
    """Point config.DATASOURCES at this workbook's sources and refresh the
    engine's captured bindings (engine imports ORDERS by value; caches must
    be cleared so a previous upload's tables aren't reused).

    `live` (from tableau_parser.live_connections) routes a queryable live
    Snowflake datasource straight at its OWN db.schema.table -- genuinely
    live, never copied into LOAD_DB/LOAD_SCHEMA like every other datasource
    here. `custom_sql` (from tableau_parser.custom_sql_sources) routes a
    queryable live custom-SQL datasource to a derived-table subquery running
    its own SQL verbatim -- the table_for()/engine FROM-clause plumbing never
    validates the string it's given, so a parenthesized subquery works
    identically to a real table name everywhere it's interpolated.

    `auto_bound` (roadmap R3, from auto_bind_sources) points an EXTRACT-based
    datasource at a table that already exists in the account. It OUTRANKS the
    local file deliberately: the whole point is to query the governed original
    instead of copying a second copy of it, so a caption that auto-bound keeps
    local_file=None and is never write_pandas-ed."""
    live = live or {}
    custom_sql = custom_sql or {}
    auto_bound = auto_bound or {}
    ds = {}
    for cap, path in caption_to_file.items():
        csinfo = custom_sql.get(cap)
        info = live.get(cap)
        if auto_bound.get(cap):
            ds[cap] = {"table": auto_bound[cap], "local_file": None,
                       "live": True, "auto_bound": True}
            continue
        if not path and csinfo and csinfo["queryable"]:
            ds[cap] = {"table": f"({csinfo['sql']}) AS {to_phys(cap)}_CSQL",
                      "local_file": None, "live": True, "custom_sql": True}
        elif not path and info and info["queryable"]:
            ds[cap] = {"table": f"{info['dbname']}.{info['schema']}.{info['table']}",
                      "local_file": None, "live": True}
        else:
            ds[cap] = {"table": fqn(cap, db, schema), "local_file": path}
    config.DATASOURCES.clear()
    config.DATASOURCES.update(ds)
    config.DEFAULT_DATASOURCE = next(
        (c for c, m in ds.items() if m.get("local_file")
         and os.path.exists(m["local_file"])), next(iter(ds), None))
    if config.DEFAULT_DATASOURCE:
        config.ORDERS = ds[config.DEFAULT_DATASOURCE]["table"]
        engine.ORDERS = config.ORDERS
    for fn in (engine.table_columns, engine._q_exec):
        try:
            fn.clear()
        except Exception:
            pass
    import backend
    backend._LOCAL_CON = None
    return ds


def ensure_target(session, db, schema):
    """Make db.schema usable WITHOUT ever issuing `USE ...` and WITHOUT
    assuming the app's role can CREATE DATABASE.

    TWO field-found bugs fixed here, both from the SAME wrong assumption --
    that a Streamlit-in-Snowflake app's Snowpark session behaves like an
    interactive worksheet session:
      1. `CREATE DATABASE IF NOT EXISTS` re-runs its privilege check even when
         the database already exists, so a locked-down OWNER role (a SiS app
         runs with its OWNER role's rights, commonly well below ACCOUNTADMIN)
         hard-crashes on it regardless of outcome.
      2. `USE SCHEMA` is a SESSION-CONTEXT statement. It works fine from `snow
         sql` (an interactive worksheet session) even as the IDENTICAL role,
         but fails inside the app's owner's-rights execution sandbox (proven
         2026-07-20: reproduced the exact role via CLI, USE SCHEMA succeeded
         there, then failed inside the deployed app and silently fell through
         to the CREATE DATABASE crash). The fix is to never rely on session
         context anywhere -- exactly how the rest of this codebase already
         always fully-qualifies every table reference instead of relying on a
         current database/schema.
    So: a pure read-only existence check (no context change), CREATE SCHEMA
    only as a fallback for a genuinely fresh target (never CREATE DATABASE --
    that's a deliberate scope limit, not an oversight), and ONE clear,
    actionable message -- not a bare Snowpark traceback -- if neither works."""
    try:
        n = session.sql(
            f'SELECT COUNT(*) AS N FROM "{db}".INFORMATION_SCHEMA.SCHEMATA '
            f"WHERE SCHEMA_NAME = '{schema.upper()}'").collect()[0]["N"]
        if n:
            return
    except Exception:
        pass                                # db not visible -- fall through
    try:
        session.sql(f'CREATE SCHEMA IF NOT EXISTS "{db}"."{schema}"').collect()
    except Exception as e:
        raise RuntimeError(
            f"Cannot find or create {db}.{schema} — the app's owner role "
            f"lacks the needed grant. Ask a Snowflake admin to run ONE of:\n"
            f'  CREATE SCHEMA IF NOT EXISTS "{db}"."{schema}"; '
            f'GRANT USAGE, CREATE TABLE ON SCHEMA "{db}"."{schema}" '
            f"TO ROLE <app owner role>;\n"
            f"  -- (if {db} itself does not exist yet) --\n"
            f'  GRANT USAGE, CREATE SCHEMA ON DATABASE "{db}" TO ROLE '
            f"<app owner role>;  -- then re-run this app\n"
            f"Original error: {e}") from e


def _fix_date_columns_session(session, db, schema, table, date_cols):
    """Snowpark-session twin of load_snowflake._fix_date_columns -- KEEP THE
    TWO IN SYNC (this is the same bug class the project keeps hitting: two data
    paths diverging). write_pandas lands a pandas datetime64[ns] column as
    NUMBER(38,0) = epoch NANOSECONDS, which Snowflake then rejects for
    DATE_TRUNC / EXTRACT / DATEDIFF and for `col BETWEEN 'YYYY-MM-DD'` -- the
    exact wall the deployed Superstore demo hit (EVERY date-using sheet failed
    with 'DATE_TRUNC/EXTRACT does not support NUMBER'). Convert each such
    column to TIMESTAMP_NTZ in place; idempotent (skips already-temporal)."""
    fq = f'"{db}"."{schema}"."{table}"'
    types = {str(r["name"]).upper(): str(r["type"] or "").upper()
             for r in session.sql(f"DESCRIBE TABLE {fq}").collect()}
    fixed = []
    for c in date_cols:
        t = types.get(c.upper(), "")
        if not t or t.startswith(("TIMESTAMP", "DATE", "TIME")):
            continue                        # already temporal, or absent
        tmp = f"{c}__TS"
        session.sql(f'ALTER TABLE {fq} ADD COLUMN "{tmp}" TIMESTAMP_NTZ').collect()
        session.sql(f'UPDATE {fq} SET "{tmp}" = TO_TIMESTAMP("{c}", 9)').collect()
        session.sql(f'ALTER TABLE {fq} DROP COLUMN "{c}"').collect()
        session.sql(f'ALTER TABLE {fq} RENAME COLUMN "{tmp}" TO "{c}"').collect()
        fixed.append(c)
    return fixed


def load_into_snowflake(session, caption_to_file, db=LOAD_DB, schema=LOAD_SCHEMA,
                        live=None, custom_sql=None, auto_bound=None):
    """Create + load a Snowflake table per datasource file (write_pandas).
    Returns [(caption, table, rows, note)] for the load report."""
    import pandas as pd
    live = live or {}
    custom_sql = custom_sql or {}
    auto_bound = auto_bound or {}
    rows = []
    ensure_target(session, db, schema)
    for cap, path in caption_to_file.items():
        table = to_phys(cap)
        csinfo = custom_sql.get(cap)
        info = live.get(cap)
        if auto_bound.get(cap):
            # R3: the source table already exists in the account -- nothing to
            # load. PROBE it for real anyway (same execution-gated rule as the
            # live and custom-SQL paths): the report must prove the table is
            # genuinely reachable, not merely that config points at a name.
            fq = auto_bound[cap]
            try:
                n = session.sql(f"SELECT COUNT(*) FROM {fq}").collect()[0][0]
                rows.append((cap, fq, n, "existing table (auto-bound, no copy)"))
            except Exception as e:
                rows.append((cap, fq, 0,
                             f"MISSING — auto-bound table unreachable: {e}"))
            continue
        if not path and csinfo and csinfo["queryable"]:
            # Live custom-SQL datasource -- EXECUTE the workbook's own SQL for
            # real (not just detect it) so the report proves it actually
            # compiles + runs on the account, matching this project's
            # execution-gated trust model (Cortex calc-fallback uses the same
            # "proposal must execute, not just look plausible" rule).
            derived = f"({csinfo['sql']}) AS {to_phys(cap)}_CSQL"
            try:
                n = session.sql(f"SELECT COUNT(*) FROM {derived}").collect()[0][0]
                rows.append((cap, derived, n, "custom SQL executed live, no copy"))
            except Exception as e:
                # execution-gated, like every other "trust but verify" check in
                # this project: a custom SQL statement that fails to compile/run
                # must stop the pipeline here (MISSING prefix -- same signal
                # onboard() already uses to halt Stage 1 cleanly), never leave
                # sheets pointed at a derived table that doesn't actually work.
                rows.append((cap, derived, 0, f"MISSING -- custom SQL failed to execute: {e}"))
            continue
        if not path and info and info["queryable"]:
            # Genuinely LIVE Snowflake datasource -- nothing to load, config
            # already points at the source's own table (configure_datasources).
            # Probe it for real so the report proves it's actually reachable,
            # not just configured.
            live_fqn = f"{info['dbname']}.{info['schema']}.{info['table']}"
            try:
                n = session.sql(f"SELECT COUNT(*) FROM {live_fqn}").collect()[0][0]
                rows.append((cap, live_fqn, n, "live (queried directly, no copy)"))
            except Exception as e:
                rows.append((cap, live_fqn, 0, f"live connection unreachable: {e}"))
            continue
        if not path or not os.path.exists(path):
            # No decodable local file: either a live source we can't query
            # directly (see datasource_notes for the reason) or an extract
            # (.hyper) that cannot be decoded inside Snowflake. REUSE a
            # pre-loaded table WHEREVER it already lives in the DB (LOAD_SCHEMA
            # first, else any schema -- e.g. WBR_DB.PUBLIC from the corpus load)
            # if a confident single match exists; otherwise flag it MISSING
            # (never leave the sheets pointed at a table that was never created
            # -- the 'does not exist or not authorized' cascade hyper-only
            # workbooks hit in Snowsight).
            resolved, note = resolve_existing_table(session, db, schema, table)
            if resolved:
                n = session.sql(f"SELECT COUNT(*) FROM {resolved}").collect()[0][0]
                # Point the app at wherever the table ACTUALLY lives (may be a
                # different schema than LOAD_SCHEMA) so every chart query + the
                # semantic view hit the real table, not the phantom
                # LOAD_SCHEMA one configure_datasources assumed.
                if cap in config.DATASOURCES:
                    config.DATASOURCES[cap]["table"] = resolved
                    if cap == config.DEFAULT_DATASOURCE:
                        config.ORDERS = resolved
                        engine.ORDERS = resolved
                rows.append((cap, resolved, n, note))
            else:
                rows.append((cap, fqn(cap, db, schema), 0,
                             ("MISSING — " + note) if note else
                             "MISSING — run one-time local pre-load "
                             "(preload_demo.py)"))
            continue
        df = _normalize_columns(_read_source_file(path))
        session.write_pandas(df, table, database=db, schema=schema,
                             auto_create_table=True, overwrite=True,
                             quote_identifiers=False)
        # write_pandas stored datetime64 columns as NUMBER (epoch nanos) --
        # repair them to TIMESTAMP so date functions work (see the twin in
        # load_snowflake.py). Detect from the DataFrame's own dtypes.
        date_cols = [c for c in df.columns
                     if str(df[c].dtype).startswith("datetime")]
        try:
            _fix_date_columns_session(session, db, schema, table, date_cols)
        except Exception as e:
            findings_note = f"date-fix failed: {e}"     # surfaced in the note
        else:
            findings_note = None
        n = session.sql(f"SELECT COUNT(*) FROM {fqn(cap, db, schema)}").collect()[0][0]
        note = "OK" if n == len(df) else "row mismatch"
        if findings_note:
            note += f"; {findings_note}"
        rows.append((cap, fqn(cap, db, schema), n, note))
    return rows


def onboard(wb_path, raw, in_snowflake=False, session=None):
    """STAGE 1 (Discovery) as pure logic: parse the root, extract + decode
    bundled data, match to datasources, configure the engine, and (whenever a
    Snowflake session is present) load tables. Returns a dict describing what
    was discovered.

    `in_snowflake` is retained for caller compatibility but no longer gates the
    decode: the decode is ALWAYS attempted (see below). What actually decides
    behaviour is (a) whether the environment can decode a `.hyper` and (b)
    whether a Snowflake `session` was handed in -- which is exactly what lets
    the SAME migrator run locally-connected and do everything in one upload."""
    import tempfile
    import init_workbook as IW
    workdir = os.path.dirname(wb_path) or tempfile.mkdtemp(prefix="twbconv_")
    root = TP.load_twb_xml(wb_path)
    rels = IW.parse_relationships(root)
    files, hyper_paths = ({}, [])
    if wb_path.lower().endswith(".twbx"):
        files, hyper_paths = bundled_data_files(raw, workdir)
    # R3: BEFORE decoding anything, ask whether each datasource's upstream table
    # already exists in the account. Resolving first is what makes this a real
    # saving -- an auto-bound datasource skips BOTH the extract decode (the
    # slowest local step) and the write_pandas copy, and the app queries the
    # governed original instead of a duplicate. Needs a session to probe
    # INFORMATION_SCHEMA, so with no session this is a no-op and every existing
    # code path below behaves exactly as before.
    auto_bound, auto_reports = auto_bind_sources(session, root, db=LOAD_DB,
                                                 schema=LOAD_SCHEMA)
    if auto_bound:
        bound_files = set()
        for cap, basenames in IW.datasource_files(root).items():
            if cap in auto_bound:
                bound_files.update(b.lower() for b in basenames)
        hyper_paths = [p for p in hyper_paths
                       if os.path.basename(p).lower() not in bound_files]
    blocked = []
    if hyper_paths:
        # ALWAYS attempt the decode. On a laptop (which has Tableau's Hyper
        # engine) it succeeds; inside a Streamlit-in-Snowflake sandbox
        # tableauhyperapi is absent so every .hyper comes back blocked and is
        # handled downstream (reuse a pre-loaded table, else flag MISSING).
        # A .hyper CANNOT be decoded inside Snowflake -- so the way to migrate
        # a hyper-only workbook in a single upload is to run this migrator
        # locally but connected to Snowflake: decode here, load up there.
        blocked = decode_hypers_locally(hyper_paths, files, workdir,
                                        relationships=rels)
    caption_to_file = match_files_to_datasources(root, files)
    # UNION datasources: materialize all member files (UNION ALL) into one CSV
    # so every member's rows are queried, not just the one match_files picked.
    for cap, members in TP.union_members(root).items():
        paths = [files.get(m) for m in members if files.get(m)]
        if len(paths) >= 2:
            out = os.path.join(workdir, to_phys(cap) + "__union.csv")
            if IW.materialize_union(paths, out):
                caption_to_file[cap] = out
    live = TP.live_connections(root)
    custom_sql = TP.custom_sql_sources(root)
    # An auto-bound caption must not also carry a local file, or configure/load
    # would still see something to copy. Its data lives in Snowflake already.
    for cap in auto_bound:
        caption_to_file[cap] = None
    configure_datasources(caption_to_file, live=live, custom_sql=custom_sql,
                          auto_bound=auto_bound)
    load_report = None
    missing = []
    if session is not None:                 # ANY Snowflake session -- the app
        load_report = load_into_snowflake(session, caption_to_file, live=live,
                                          custom_sql=custom_sql,
                                          auto_bound=auto_bound)  # hosted in
        # SiS OR run locally against a named connection. Datasources with no
        # decodable file AND no pre-loaded table would 404; surface them so the
        # UI can stop cleanly with the remediation instead of cascading errors.
        missing = [r[0] for r in load_report
                   if str(r[3]).startswith("MISSING")]
        if missing:
            # R10 GAP FIX (found live, 2026-07-26): load_into_snowflake's
            # missing-check only ever probes for ONE table named
            # to_phys(caption) -- it has no idea a MULTI-TABLE datasource's
            # constituent tables might independently verify as an existing
            # separate model (R10) or be decodable as separate tables (scope
            # B). Neither of those possibilities is single-table-shaped, so
            # the naive probe always calls a multi-table datasource MISSING,
            # and Stage 1 (pipeline_app.py) hard st.stop()s on any MISSING
            # caption -- BEFORE Stage 3's build_data_model_tables (which DOES
            # know how to resolve it) ever gets a chance to run. Resolve it
            # HERE, at Discovery time, instead of waiting for a later stage
            # that would never be reached. A no-op for every single-table
            # datasource (build_data_model_tables only touches n_tables>1
            # models) and for a multi-table one that can't resolve either way
            # (falls through to the identical MISSING outcome, just one call
            # earlier -- no behavior change, only an earlier resolution).
            import semantic_layer as SL
            multi_caps = {m["caption"] for m in
                         SL.describe_model(root, LOAD_DB, LOAD_SCHEMA)
                         if m["n_tables"] > 1}
            if multi_caps & set(missing):
                resolved = build_data_model_tables(session, root, hyper_paths,
                                                   db=LOAD_DB, schema=LOAD_SCHEMA)
                by_cap = {r[0]: r for r in load_report}
                for cap, view, note in resolved:
                    if not view or cap not in missing:
                        continue
                    try:
                        n = session.sql(f"SELECT COUNT(*) FROM {view}").collect()[0][0]
                    except Exception:
                        n = 0
                    by_cap[cap] = (cap, view, n, note)
                load_report = list(by_cap.values())
                missing = [r[0] for r in load_report
                          if str(r[3]).startswith("MISSING")]
    return {"root": root, "caption_to_file": caption_to_file,
            "blocked": blocked, "hyper_paths": hyper_paths,
            "load_report": load_report, "missing": missing,
            "live_connections": live, "custom_sql_sources": custom_sql,
            "auto_bound": auto_bound, "auto_bind_reports": auto_reports,
            "n_datasources": len(caption_to_file)}


# --------------------------------------------------------------------------- #
# STAGE 6 (opt-in, HUMAN-GATED): deploy the generated app to Snowflake.
#
# Today the pipeline GENERATES app_<stem>.py and only offers a download; this
# is the "click Deploy after reviewing all 5 stages" step. It never runs
# automatically -- the UI wires it to an explicit button, matching the
# project's nothing-ships-without-a-human-gate philosophy.
#
# NO `snow` CLI: a Streamlit-in-Snowflake sandbox has no shell (same limit that
# blocks hyper decode). Deploy is done PURELY through the Snowpark session --
# stage the files with session.file.put, then CREATE STREAMLIT -- so the exact
# same call works whether the app runs locally-connected (backend.set_session)
# or hosted in SiS. Everything is fully-qualified; a session-context `USE` is
# never issued (it fails in an owner's-rights sandbox -- see ensure_target).
# --------------------------------------------------------------------------- #

def _streamlit_identifier(stem, prefix="TABLEAU_TO_SIS_"):
    """A valid, unquoted Snowflake identifier for the deployed Streamlit object,
    derived from the workbook stem. Uppercased, non-alnum -> '_', never
    leading-digit."""
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(stem)).strip("_").upper() or "APP"
    if s[0].isdigit():
        s = "A_" + s
    return (prefix + s)[:255]


def _create_streamlit_ddl(db, schema, identifier, stage, stem, main_file,
                          warehouse, title=None):
    """Pure CREATE STREAMLIT DDL builder (no session) so it is offline-testable.
    Fully-qualifies the object AND its ROOT_LOCATION stage, sets a query
    warehouse, and NEVER issues a session-context `USE` -- exactly how the rest
    of this codebase always addresses objects, because `USE` fails inside an
    owner's-rights SiS sandbox."""
    t = (title or f"Tableau -> SiS ({stem})").replace("'", "''")
    return (f'CREATE OR REPLACE STREAMLIT "{db}"."{schema}"."{identifier}"\n'
            f"  ROOT_LOCATION = '@{db}.{schema}.{stage}/{stem}'\n"
            f"  MAIN_FILE = '{main_file}'\n"
            f'  QUERY_WAREHOUSE = "{warehouse}"\n'
            f"  TITLE = '{t}'")


def _snowsight_url(session, db, schema, identifier):
    """Best-effort Snowsight deep link to the deployed app. Returns None if the
    org/account can't be resolved (the UI then shows the nav path instead of a
    URL that might not resolve -- honesty over a fabricated link)."""
    try:
        r = session.sql("SELECT CURRENT_ORGANIZATION_NAME() AS O, "
                        "CURRENT_ACCOUNT_NAME() AS A").collect()[0]
        org, acct = r["O"], r["A"]
        if org and acct:
            return (f"https://app.snowflake.com/{org.lower()}/{acct.lower()}"
                    f"/#/streamlit-apps/{db}.{schema}.{identifier}")
    except Exception:
        pass
    return None


def deploy_streamlit_app(session, stem, app_file, *, db=LOAD_DB, schema=LOAD_SCHEMA,
                         stage=DEPLOY_STAGE, identifier=None, warehouse=None,
                         datasources=None, title=None, runtime_modules=None,
                         root_dir=None):
    """Deploy a generated standalone app_<stem>.py to Streamlit-in-Snowflake
    through the Snowpark `session` (no CLI). Stages the app + its runtime
    modules (APP_RUNTIME_MODULES) + a per-deploy datasources.json pointing at
    the loaded tables, then runs CREATE OR REPLACE STREAMLIT. Re-deploy replaces
    it in place. Returns a dict: identifier / url / root_location / warehouse /
    files / ddl.

    Raises RuntimeError (never a bare traceback) with an actionable message for
    the recoverable cases: no session, no query warehouse, a missing artifact.
    """
    import json as _json
    import shutil
    import tempfile

    if session is None:
        raise RuntimeError(
            "Deploy needs a live Snowflake session, but none is connected. Tick "
            "'Push to Snowflake on upload' in the sidebar (or run hosted in "
            "Snowsight) and re-upload, then click Deploy.")

    root_dir = root_dir or os.getcwd()
    identifier = identifier or _streamlit_identifier(stem)
    main_file = os.path.basename(app_file)
    runtime_modules = list(runtime_modules if runtime_modules is not None
                           else APP_RUNTIME_MODULES)

    # A Streamlit object needs a query warehouse. Read the session's current one
    # (strip the quotes get_current_warehouse returns); never `USE` it.
    if not warehouse:
        try:
            warehouse = (session.get_current_warehouse() or "").strip('"')
        except Exception:
            warehouse = ""
    if not warehouse:
        raise RuntimeError(
            "No query warehouse on the session -- a Streamlit app requires one. "
            "Set a default warehouse on the `snow` connection (or the session) "
            "and retry.")

    # Resolve every artifact to an absolute path; a missing runtime module would
    # make the deployed app fail to import, so fail LOUD here with the name.
    app_path = app_file if os.path.isabs(app_file) else os.path.join(root_dir, app_file)
    if not os.path.exists(app_path):
        raise RuntimeError(f"Generated app not found on disk: {app_path}")
    srcs = [app_path]
    for m in runtime_modules:
        p = os.path.join(root_dir, m)
        if not os.path.exists(p):
            raise RuntimeError(
                f"Deploy artifact missing on disk: {m} (expected in {root_dir}). "
                "Cannot deploy an app whose runtime module is absent.")
        srcs.append(p)

    # Copy artifacts into a SPACE-FREE temp dir before PUT: Snowpark builds a
    # `PUT 'file://<path>' ...` statement, and this project's own working dir
    # contains spaces ("Tableau to SiS_Cowork - Cortex"), which breaks that
    # file:// argument. Also write the per-deploy datasources.json here so the
    # deployed app (running in Snowflake, where the staged mapping WINS -- see
    # config.py) points at the tables Stage 1 loaded.
    staging = tempfile.mkdtemp(prefix="sisdeploy_")
    staged = []
    for p in srcs:
        dst = os.path.join(staging, os.path.basename(p))
        shutil.copyfile(p, dst)
        staged.append(dst)
    ds_map = (datasources if datasources is not None
              else dict(getattr(config, "DATASOURCES", {})))
    ds_path = os.path.join(staging, "datasources.json")
    with open(ds_path, "w", encoding="utf-8") as f:
        _json.dump(ds_map, f, indent=2)
    staged.append(ds_path)

    # Target: create the schema (if needed) + the stage, fully-qualified, no USE.
    ensure_target(session, db, schema)
    session.sql(f'CREATE STAGE IF NOT EXISTS "{db}"."{schema}"."{stage}"').collect()
    root_location = f"@{db}.{schema}.{stage}/{stem}"
    for p in staged:
        session.file.put(p.replace("\\", "/"), root_location + "/",
                         auto_compress=False, overwrite=True)

    ddl = _create_streamlit_ddl(db, schema, identifier, stage, stem, main_file,
                                warehouse, title)
    session.sql(ddl).collect()

    return {"identifier": f"{db}.{schema}.{identifier}",
            "url": _snowsight_url(session, db, schema, identifier),
            "root_location": root_location, "warehouse": warehouse,
            "files": [os.path.basename(p) for p in staged], "ddl": ddl}

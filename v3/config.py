"""
config.py  --  single source of truth for table/column mapping + client profile.
Shared by the app, the data backend, and the code generator.
Edit ONLY this file (and the client profile it points to) to repoint the
accelerator at different physical Snowflake tables / local files.
"""

import profile_superstore as PROFILE   # DEFAULT client profile (back-compat;
                                        # overridden per-workbook by set_profile() below)
import profile_default as _PROFILE_DEFAULT   # neutral fallback for unrecognized workbooks

# ---- Datasource -> physical table mapping (MULTI-datasource) ---------------
# Keyed by the Tableau datasource CAPTION as it appears in the workbook.
# "table" is the fully-qualified Snowflake name; "local_file" is used by the
# DuckDB backend when testing outside Snowflake.
DB = "SUPERSTORE"
SCHEMA = "PUBLIC"

DATASOURCES = {
    "Sample - Superstore": {"table": f"{DB}.{SCHEMA}.ORDERS",
                            "local_file": "data/Sample - Superstore.csv"},
    "Sales Commission":    {"table": f"{DB}.{SCHEMA}.SALES_COMMISSION",
                            "local_file": "data/Sales Commission.csv"},
    "Sales Target":        {"table": f"{DB}.{SCHEMA}.SALES_TARGET",
                            "local_file": "data/Sales Target.xlsx"},
}

# datasources.json (written/merged by `python init_workbook.py YourBook.twbx`,
# or staged from datasources.deploy.json for a Snowflake deploy).
#
# PRECEDENCE depends on WHERE we run:
#  * LOCAL (no Snowpark session): built-in captions WIN -- the canonical
#    SUPERSTORE.PUBLIC.* mapping the numeric harness / validate_numbers depend
#    on stays authoritative; datasources.json only ADDS new captions.
#  * IN SNOWFLAKE: the staged datasources.json (= datasources.deploy.json,
#    WBR_DB.* targets) WINS for every caption -- the built-in SUPERSTORE.*
#    tables point at the LOCAL dev database, which does not exist in the
#    account, so a deployed app whose datasource caption collides with a
#    built-in (e.g. Superstore's 'Sample - Superstore') must use the deployed
#    table, not the phantom local one. This is the fix for the long-standing
#    "built-in captions beat the deploy mapping" routing landmine.
import json as _json, os as _os


def _in_snowflake():
    try:
        from snowflake.snowpark.context import get_active_session
        get_active_session()
        return True
    except Exception:
        return False


def _apply_routing_file(path, deploy_wins):
    """Merge one datasource mapping file into DATASOURCES under the precedence
    rule documented above. Returns {caption: (old_table, new_table)} for every
    caption this call actually repointed."""
    moved = {}
    with open(path, encoding="utf-8") as fh:
        for cap, mapping in _json.load(fh).items():
            if deploy_wins or cap not in DATASOURCES:
                before = (DATASOURCES.get(cap) or {}).get("table")
                DATASOURCES[cap] = mapping
                if before != mapping.get("table"):
                    moved[cap] = (before, mapping.get("table"))
    return moved


if _os.path.exists("datasources.json"):
    _apply_routing_file("datasources.json", _in_snowflake())

# ---- Explicit source-table map (roadmap R3) --------------------------------
# sources.json: {"<Tableau datasource caption>": "DB.SCHEMA.TABLE"} -- tells the
# accelerator that a datasource's data ALREADY lives at this exact table, so it
# should point there instead of decoding the workbook's extract and copying it.
#
# This is the human override for pipeline.resolve_source_binding's inference.
# The inference only auto-binds on strong evidence (the workbook's own declared
# db.schema.table, or a single name match whose COLUMNS check out); anything
# ambiguous is surfaced as a choice rather than guessed. This file is how you
# answer that choice, and how you bind a table the inference can't see (renamed,
# a different database, a view over the source). The target's existence is still
# probed at onboard time -- a stale entry fails loudly instead of binding to
# nothing. Absent file = no overrides, pure inference.
SOURCE_MAP = {}
_SOURCES_JSON = "sources.json"
if _os.path.exists(_SOURCES_JSON):
    try:
        with open(_SOURCES_JSON, encoding="utf-8") as _f:
            for _cap, _tbl in _json.load(_f).items():
                if isinstance(_tbl, str) and _tbl.strip():
                    SOURCE_MAP[_cap] = _tbl.strip()
    except Exception:
        SOURCE_MAP = {}      # malformed map must never break app startup

# default = first datasource whose local file actually exists (a fresh
# workbook onboarded via datasources.json must not default to a stale entry)
DEFAULT_DATASOURCE = next((c for c, m in DATASOURCES.items()
                           if m.get("local_file") and _os.path.exists(m["local_file"])),
                          next(iter(DATASOURCES)))

# Back-compat: the primary table's FQN (legacy single-table call sites).
TABLE = "ORDERS"
ORDERS = DATASOURCES[DEFAULT_DATASOURCE]["table"]

# Local data file for the DEFAULT datasource (legacy call sites).
LOCAL_DATA_FILE = DATASOURCES[DEFAULT_DATASOURCE]["local_file"]


def table_for(datasource):
    """Datasource caption -> fully-qualified table name (default table if unknown)."""
    d = DATASOURCES.get(datasource)
    return d["table"] if d else ORDERS


def use_snowflake_routing():
    """Re-apply routing precedence AS IF this process were running inside
    Snowflake, and return {caption: (old_table, new_table)} for what moved.

    WHY THIS EXISTS (2026-08-27). The precedence block above runs ONCE, at
    import time, and asks `_in_snowflake()` then. That is correct for both
    normal callers: a deployed SiS app already has its session when config
    imports, and a purely local run never gets one. It is WRONG for a process
    that opens a Snowpark session LATER -- make_validation_pack.py does
    exactly that, connecting well after config was imported.

    The result was silent and expensive: routing stayed latched on the
    built-in SUPERSTORE.* dev tables (the "phantom local database" the
    comment above already warns about), while backend.run_sql decides
    Snowflake-vs-DuckDB per CALL and therefore started shipping that same SQL
    to the account. Every query died with `Database 'SUPERSTORE' does not
    exist or not authorized`, engine drew nothing, and the validation pack
    reported all 21 charts as "sheet drew no Altair chart" -- a capture-layer
    symptom for a routing-layer cause.

    Deliberately NOT a second routing path: it reuses _apply_routing_file with
    deploy_wins=True, the exact rule the deployed app runs under. The source
    is datasources.deploy.json when it exists (local dev, where plain
    datasources.json still holds the SUPERSTORE dev mapping) and otherwise
    datasources.json -- which IS the deploy mapping in Snowflake, because
    snowflake.yml stages datasources.deploy.json AS datasources.json.
    """
    global ORDERS, LOCAL_DATA_FILE
    path = next((p for p in ("datasources.deploy.json", "datasources.json")
                 if _os.path.exists(p)), None)
    if path is None:
        return {}
    moved = _apply_routing_file(path, deploy_wins=True)
    # Derived values are computed once at import from the pre-move mapping;
    # refresh them or the legacy single-table call sites keep the old target.
    default = DATASOURCES.get(DEFAULT_DATASOURCE) or {}
    ORDERS = default.get("table", ORDERS)
    LOCAL_DATA_FILE = default.get("local_file", LOCAL_DATA_FILE)
    return moved


# ---- Per-workbook client profile ------------------------------------------
# ROUTING LANDMINE (2026-07-21 MVP fix): config.py used to hardcode
# `import profile_superstore as PROFILE` for EVERY workbook, unconditionally.
# profile_superstore.MEASURE_LIBRARY/DIM_VALUE_COLORS use generic captions
# ("Sales", "Profit", "Discount", "Region") -- a genuinely different client's
# workbook could coincidentally use the same caption for an unrelated raw
# field and silently inherit Superstore's curated SQL/format/colors instead
# of its own. Fix: resolve the profile PER WORKBOOK from its source filename.
#
# _PROFILE_REGISTRY is an explicit allow-list of the workbooks this profile
# was actually authored/tuned against (the whole current corpus is Superstore-
# schema-family, or -- World Indicators -- has its Region colors hand-tuned
# INTO profile_superstore.py). Listing them explicitly keeps every existing
# workbook's behavior byte-identical (regression-safe) while making the
# selection an explicit, auditable mapping instead of a silent global default.
# Anything NOT in this list (a genuinely new/foreign workbook) now gets
# profile_default (neutral, empty) instead of quietly inheriting Superstore's
# vocabulary -- that is the actual landmine fix.
_PROFILE_REGISTRY = {
    "superstore.twb": PROFILE,
    "superstore.twbx": PROFILE,
    "superstore_tableau2024_3.twbx": PROFILE,
    "superstore_topn_measureswap.twbx": PROFILE,
    "superstore_kpi_parameter_dashboard.twbx": PROFILE,
    "_ss2024.twb": PROFILE,
    "world indicators.twbx": PROFILE,
    "world_indicators_tableau2024_3.twbx": PROFILE,
    "_wi2024.twb": PROFILE,
}

# Optional external override (same JSON-merge pattern as datasources.json):
# {"<source .twb/.twbx filename>": "<profile module name, e.g. profile_acme>"}
# Lets a new client's workbook be pointed at its own profile_<client>.py
# without editing config.py.
_PROFILES_JSON = "profiles.json"
if _os.path.exists(_PROFILES_JSON):
    import importlib as _importlib
    with open(_PROFILES_JSON, encoding="utf-8") as _f:
        for _fname, _modname in _json.load(_f).items():
            try:
                _PROFILE_REGISTRY[_fname.strip().lower()] = _importlib.import_module(_modname)
            except ImportError:
                pass   # bad entry in profiles.json -- fall through to the default profile


def profile_for(source_file):
    """Source .twb/.twbx path (as recorded in ir['source_file']) -> the client
    profile module for that workbook. Unrecognized workbook -> profile_default
    (neutral), never a silent Superstore fallback."""
    name = _os.path.basename(source_file or "").strip().lower()
    return _PROFILE_REGISTRY.get(name, _PROFILE_DEFAULT)


def set_profile(source_file):
    """Make PROFILE (module-level, read live via `config.PROFILE`) the profile
    for THIS workbook. Call once per workbook via engine.configure(ir)."""
    global PROFILE
    PROFILE = profile_for(source_file)
    return PROFILE


# Logical Tableau field  ->  physical column name (used in generated SQL).
COL = {
    "Order Date":    "ORDER_DATE",
    "Ship Date":     "SHIP_DATE",
    "Sales":         "SALES",
    "Profit":        "PROFIT",
    "Quantity":      "QUANTITY",
    "Discount":      "DISCOUNT",
    "Segment":       "SEGMENT",
    "Category":      "CATEGORY",
    "Sub-Category":  "SUB_CATEGORY",
    "State":         "STATE",
    "Region":        "REGION",
    "Order ID":      "ORDER_ID",
    "Customer Name": "CUSTOMER_NAME",
}

# Source-file header  ->  physical column. Handles common Tableau-sample variants.
SOURCE_COLUMN_ALIASES = {
    "ORDER_DATE":    ["Order Date", "order_date"],
    "SHIP_DATE":     ["Ship Date", "ship_date"],
    "SALES":         ["Sales", "sales"],
    "PROFIT":        ["Profit", "profit"],
    "QUANTITY":      ["Quantity", "quantity"],
    "DISCOUNT":      ["Discount", "discount"],
    "SEGMENT":       ["Segment", "segment"],
    "CATEGORY":      ["Category", "category"],
    "SUB_CATEGORY":  ["Sub-Category", "Sub Category", "sub_category"],
    "STATE":         ["State", "State/Province", "state"],
    "REGION":        ["Region", "region"],
    "ORDER_ID":      ["Order ID", "Order Id", "order_id"],
    "CUSTOMER_NAME": ["Customer Name", "customer_name"],
}

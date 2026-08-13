"""
load_snowflake.py  --  one-time data load for EXTRACT-based workbooks.

For workbooks whose data lives only in the .twbx (hyper extracts / CSVs),
this loads every local file from datasources.json into the Snowflake table
it is mapped to, with the SAME UPPER_SNAKE columns the app queries -- so the
deployed app works identically to local.

Workbooks whose datasources are ALREADY Snowflake tables don't need this:
just point datasources.json "table" at the real FQNs and deploy.

Usage:
  pip install snowflake-connector-python[pandas]
  python load_snowflake.py --account <acct> --user <user> --warehouse <wh> \
                           [--role <role>] [--authenticator externalbrowser]

Reads datasources.json (written by convert.py / init_workbook.py).
Verify afterwards:  every table prints its row count; compare with local.
"""

import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend import _read_source_file, _normalize_columns   # noqa: E402


def _fix_date_columns(cur, db, schema, table, date_cols):
    """write_pandas stores datetime64[ns] as NUMBER(38,0) = epoch NANOSECONDS
    (Snowflake then rejects DATE_TRUNC/DATEDIFF on it -- the app fails).
    Convert each such column to TIMESTAMP_NTZ in place. Skips columns that
    are already a temporal type (idempotent). Order changes (fine: the app
    queries by name). Returns the list of columns actually converted."""
    fqn = f'"{db}"."{schema}"."{table}"'
    types = {r[0].upper(): (r[1] or "").upper()
             for r in cur.execute(f"DESCRIBE TABLE {fqn}").fetchall()}
    fixed = []
    for c in date_cols:
        cu = c.upper()
        t = types.get(cu, "")
        if not t or t.startswith(("TIMESTAMP", "DATE", "TIME")):
            continue                       # already temporal, or absent
        tmp = f"{c}__TS"
        cur.execute(f'ALTER TABLE {fqn} ADD COLUMN "{tmp}" TIMESTAMP_NTZ')
        # NUMBER holds nanosecond epoch -> scale 9; if it were a string date
        # TO_TIMESTAMP still parses it, so this is safe either way
        cur.execute(f'UPDATE {fqn} SET "{tmp}" = TO_TIMESTAMP("{c}", 9)')
        cur.execute(f'ALTER TABLE {fqn} DROP COLUMN "{c}"')
        cur.execute(f'ALTER TABLE {fqn} RENAME COLUMN "{tmp}" TO "{c}"')
        fixed.append(c)
    return fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--role", default=None)
    ap.add_argument("--authenticator", default=None,
                    help="e.g. externalbrowser for SSO")
    ap.add_argument("--mapping", default="datasources.json")
    ap.add_argument("--database", default=None,
                    help="override target DATABASE for every table (also "
                         "rewrites the mapping file so the app + semantic "
                         "views query the same place)")
    ap.add_argument("--schema", default=None,
                    help="override target SCHEMA (with --database)")
    a = ap.parse_args()

    import snowflake.connector
    from snowflake.connector.pandas_tools import write_pandas

    kw = dict(account=a.account, user=a.user, warehouse=a.warehouse)
    if a.role:
        kw["role"] = a.role
    if a.authenticator:
        kw["authenticator"] = a.authenticator
    else:
        kw["password"] = getpass.getpass("Snowflake password: ")
    con = snowflake.connector.connect(**kw)

    mapping = json.load(open(a.mapping, encoding="utf-8"))
    if a.database:
        # retarget every FQN to the requested db/schema, keep table names.
        # Written to a SEPARATE deploy mapping -- the dev mapping stays
        # untouched (mutating it broke local DuckDB verification; swap the
        # deploy file in only when deploying to Snowflake).
        for m in mapping.values():
            if m.get("table"):
                parts = m["table"].split(".")
                m["table"] = ".".join([a.database, a.schema or parts[1],
                                       parts[2]])
        deploy_out = a.mapping.replace(".json", ".deploy.json")
        with open(deploy_out, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2)
        print(f"retargeted {len(mapping)} datasource(s) -> "
              f"{a.database}.{a.schema or '<schema>'}  "
              f"(deploy mapping: {deploy_out}; dev mapping untouched)\n")
    for caption, m in mapping.items():
        path, fqn = m.get("local_file"), m.get("table")
        if not path or not os.path.exists(path):
            print(f"SKIP {caption}: no local file ({path}) -- if this datasource "
                  f"is already a Snowflake table, just set its FQN in {a.mapping}")
            continue
        db, schema, table = fqn.split(".")
        df = _normalize_columns(_read_source_file(path))   # same cols the app queries
        cur = con.cursor()
        # only CREATE when actually missing -- an unconditional CREATE
        # DATABASE needs account-level privilege even when the DB exists
        if not cur.execute(f"SHOW DATABASES LIKE '{db}'").fetchall():
            cur.execute(f'CREATE DATABASE "{db}"')
        if not cur.execute(f"SHOW SCHEMAS LIKE '{schema}' IN DATABASE \"{db}\"").fetchall():
            cur.execute(f'CREATE SCHEMA "{db}"."{schema}"')
        cur.execute(f'USE SCHEMA "{db}"."{schema}"')
        ok, _, nrows, _ = write_pandas(con, df, table, database=db, schema=schema,
                                       auto_create_table=True, overwrite=True)
        # PERSISTENCE GUARD: some connector versions create the table
        # SESSION-TEMPORARY here -- it counts fine in this session and
        # vanishes on close (in-session verification passes, the deployed
        # app then sees 'does not exist'). Detect the kind and, if temp,
        # copy to a real table (temp SHADOWS the permanent name in-session,
        # so CTAS to a side name, drop the temp, rename).
        row = cur.execute(f"SHOW TABLES LIKE '{table}' IN SCHEMA "
                          f'"{db}"."{schema}"').fetchall()
        kind = row[0][4] if row else "?"
        if kind and "TEMP" in str(kind).upper():
            cur.execute(f'CREATE OR REPLACE TABLE "{db}"."{schema}"."{table}__PERM" '
                        f'AS SELECT * FROM {fqn}')
            cur.execute(f'DROP TABLE "{db}"."{schema}"."{table}"')  # the temp
            cur.execute(f'ALTER TABLE "{db}"."{schema}"."{table}__PERM" '
                        f'RENAME TO "{db}"."{schema}"."{table}"')
            kind = "TABLE (persisted from temp)"
        # write_pandas lands datetime64 as NUMBER (epoch nanos) -> convert
        # to TIMESTAMP so DATE_TRUNC/DATEDIFF work in Snowflake
        date_cols = [c for c in df.columns
                     if str(df[c].dtype).startswith("datetime")]
        fixed = _fix_date_columns(cur, db, schema, table, date_cols)
        check = con.cursor().execute(f'SELECT COUNT(*) FROM {fqn}').fetchone()[0]
        dt = f"  +{len(fixed)} date col(s) -> TIMESTAMP" if fixed else ""
        print(f"{'OK  ' if ok and check == len(df) else 'FAIL'} {caption:<28} "
              f"-> {fqn}  local={len(df)} rows, snowflake={check} rows  [{kind}]{dt}")
    con.close()
    print("\nNext: snow streamlit deploy   (see DEPLOY.md)")


if __name__ == "__main__":
    main()

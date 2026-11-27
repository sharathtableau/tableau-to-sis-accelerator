"""
verify_deploy.py  --  live proof of the human-gated Deploy button (R5).

The in-app Deploy button ships a generated app to Streamlit-in-Snowflake through
the Snowpark session (session.file.put -> CREATE STREAMLIT), NOT the `snow` CLI
-- because a hosted SiS sandbox has no shell. That path is proven OFFLINE by
tests/test_regression.py::test_deploy_streamlit_app (stub session). This script
proves it LIVE on a real account, from a laptop, using the SAME `wbr` connection
(and the SAME browser SSO) every other feature was deployed with.

It runs the EXACT code the button runs -- pipeline.onboard (load the tables) ->
codegen.build (generate app_<stem>.py) -> pipeline.deploy_streamlit_app -- so a
green run here is a green run of the button. Then it independently SHOWs the
created object so the proof is a check, not a claim.

    python verify_deploy.py "Workbooks/Superstore.twbx" [--connection wbr]
                            [--cleanup]   # DROP the deployed app + leave the account clean

Needs the `wbr` named Snowflake connection (same one convert.py --connection /
preload_demo.py use). SSO may open a browser to authenticate. A hyper-only
workbook must be pre-loaded first (preload_demo.py) -- same rule as the app.
"""

import argparse
import os
import re
import sys

import codegen
import pipeline


def _session(connection_name):
    try:
        return pipeline.snow_session(connection_name)
    except Exception as e:
        sys.exit(f"Could not open Snowflake session from connection "
                 f"'{connection_name}': {e}\nCheck `snow connection list`.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook", help=".twb/.twbx to convert + deploy live")
    ap.add_argument("--connection", default="wbr",
                    help="named Snowflake connection (default: wbr)")
    ap.add_argument("--cleanup", action="store_true",
                    help="DROP the deployed Streamlit app afterwards (leave the "
                         "account as it was) -- use for a pure smoke test")
    a = ap.parse_args()
    if not os.path.exists(a.workbook):
        sys.exit(f"No such workbook: {a.workbook}")

    stem = re.sub(r"[^0-9A-Za-z]+", "_",
                  os.path.splitext(os.path.basename(a.workbook))[0]).strip("_").lower() or "workbook"
    raw = open(a.workbook, "rb").read()

    print(f"Connecting to '{a.connection}' (SSO may open a browser) ...")
    session = _session(a.connection)
    try:
        # 1) Load the tables the app will query -- the EXACT Stage-1 path.
        disc = pipeline.onboard(a.workbook, raw, in_snowflake=False, session=session)
        if disc.get("missing"):
            sys.exit(f"Cannot deploy: datasource(s) {disc['missing']} have no data "
                     f"in Snowflake. Pre-load first:\n  python preload_demo.py "
                     f'"{a.workbook}"')
        print(f"  loaded {disc['n_datasources']} datasource(s) into "
              f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}")

        # 2) Generate the standalone app -- the EXACT Stage-4 path.
        import tableau_parser as TP
        ir = TP.build_ir(a.workbook)
        app_name = f"app_{stem}.py"
        with open(app_name, "w", encoding="utf-8") as f:
            f.write(codegen.build(ir))
        print(f"  generated {app_name}")

        # 3) Deploy -- the EXACT button path (session.file.put -> CREATE STREAMLIT).
        res = pipeline.deploy_streamlit_app(session, stem, app_name)
        print(f"  deployed {res['identifier']}  (warehouse {res['warehouse']})")
        print(f"  staged: {', '.join(res['files'])}")

        # 4) PROVE it -- independently SHOW the object exists (a check, not a claim).
        db, schema, ident = res["identifier"].split(".")
        found = session.sql(
            f"SHOW STREAMLITS LIKE '{ident}' IN SCHEMA \"{db}\".\"{schema}\"").collect()
        assert found, (f"CREATE ran but SHOW STREAMLITS found no '{ident}' -- "
                       "deploy did not actually land the object.")
        print(f"\nPROVEN LIVE: {res['identifier']} exists in the account.")
        if res.get("url"):
            print(f"  Open in Snowsight: {res['url']}")
        else:
            print(f"  Snowsight -> Projects -> Streamlit -> {res['identifier']}")

        if a.cleanup:
            session.sql(f'DROP STREAMLIT IF EXISTS "{db}"."{schema}"."{ident}"').collect()
            print(f"  cleaned up (dropped {res['identifier']}).")
    finally:
        session.close()


if __name__ == "__main__":
    main()

"""
preload_demo.py  --  one-time local pre-load for hyper-only workbooks.

Some workbooks (Regional Analysis, Global Sales) carry their data ONLY as
Tableau `.hyper` extracts. A Streamlit-in-Snowflake sandbox has no hyper
decoder, so the staged demo app cannot create those tables itself. This script
runs from a laptop -- where the extract DOES decode -- and loads the tables
into the SAME place, with the SAME names, the deployed demo app looks for
(`WBR_DB.PIPELINE_DEMO.<to_phys(caption)>`). After running it once, upload the
same .twbx in Snowsight and the app reuses these tables automatically.

It reuses pipeline.onboard (local decode) + pipeline.load_into_snowflake (the
exact naming + epoch-nanosecond date repair the app expects) so there is ONE
load code path -- no chance of the pre-load and the app disagreeing on a name.

    python preload_demo.py "Workbooks/Regional Analysis.twbx" [--connection wbr]

Needs a Snowflake connection (default: the `wbr` named connection in
~/.snowflake/connections.toml, same one convert.py --connection uses). SSO may
open a browser to authenticate.
"""

import argparse
import os
import sys

import pipeline


def _session(connection_name):
    try:
        return pipeline.snow_session(connection_name)          # shared, robust
    except Exception as e:
        sys.exit(f"Could not open Snowflake session from connection "
                 f"'{connection_name}': {e}\nCheck `snow connection list`.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook", help=".twb/.twbx whose extract-only "
                    "datasources need loading")
    ap.add_argument("--connection", default="wbr",
                    help="named Snowflake connection (default: wbr)")
    a = ap.parse_args()
    if not os.path.exists(a.workbook):
        sys.exit(f"No such workbook: {a.workbook}")

    raw = open(a.workbook, "rb").read()
    # LOCAL onboard: decode the .hyper here (this is the whole point) and build
    # the caption -> local-CSV map. in_snowflake=False so hyper decode runs.
    disc = pipeline.onboard(a.workbook, raw, in_snowflake=False, session=None)
    have = {c: f for c, f in disc["caption_to_file"].items() if f}
    if not have:
        sys.exit("Nothing to load: no datasource decoded to a local file. "
                 "(A purely live-connection workbook needs its real tables "
                 "referenced directly, not pre-loaded.)")

    print(f"Decoded {len(have)} datasource file(s). Connecting to "
          f"'{a.connection}' ...")
    session = _session(a.connection)
    try:
        report = pipeline.load_into_snowflake(session, have)
    finally:
        session.close()

    print(f"\nLoaded into {pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}:")
    for cap, table, n, note in report:
        print(f"  {'OK  ' if note.startswith('OK') else '    '}"
              f"{cap:<45} -> {table}  {n} rows  [{note}]")
    print("\nDone. Re-upload the same workbook in the Snowsight demo app — it "
          "will reuse these tables.")


if __name__ == "__main__":
    main()

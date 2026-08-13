"""
preload_model.py  --  scope B: replicate a Tableau relationship extract's data
model as REAL Snowflake objects. Loads each underlying table SEPARATELY (not
flattened) into WBR_DB.PIPELINE_DEMO + CREATEs the relationship VIEW the
workbook's joins imply, so the model is visible/queryable in the backend.

Laptop-only: a .hyper cannot be decoded inside Snowsight (no Hyper engine in the
SiS sandbox), so the separate-table decode + load must run from a machine that
has the Tableau Hyper engine -- exactly like preload_demo.py for the flattened
table. After running this, the deployed demo app's Stage 3a shows the deployed
view for that workbook.

    python preload_model.py "Workbooks/E-Commerce (Software) Sales Dashboard VOTD.twbx" [--connection wbr]

Reuses pipeline.build_data_model_tables (same code the in-app "Replicate data
model" toggle runs), so there is ONE scope-B code path. SSO may open a browser.
"""

import argparse
import os
import sys
import tempfile
import zipfile

import pipeline
import tableau_parser as TP


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook", help=".twb/.twbx with a relationship extract")
    ap.add_argument("--connection", default="wbr",
                    help="named Snowflake connection (default: wbr)")
    a = ap.parse_args()
    if not os.path.exists(a.workbook):
        sys.exit(f"No such workbook: {a.workbook}")

    workdir = tempfile.mkdtemp(prefix="modelb_")
    hyper_paths = []
    if a.workbook.lower().endswith(".twbx"):
        with zipfile.ZipFile(a.workbook) as z:
            for n in z.namelist():
                if n.lower().endswith(".hyper"):
                    z.extract(n, workdir)
                    hyper_paths.append(os.path.join(workdir, n))
    if not hyper_paths:
        sys.exit("No .hyper extract found in this workbook -- scope B replicates "
                 "relationship EXTRACTS (nothing to decode separately here).")

    raw = open(a.workbook, "rb").read()
    # configure config.DATASOURCES for this workbook (no load: session=None) so
    # build_data_model_tables repoints the right datasource caption at the view.
    pipeline.onboard(a.workbook, raw, in_snowflake=False, session=None)

    print(f"Connecting to '{a.connection}' (SSO may open a browser) ...")
    try:
        session = pipeline.snow_session(a.connection)
    except Exception as e:
        sys.exit(f"Could not open Snowflake session '{a.connection}': {e}")
    try:
        report = pipeline.build_data_model_tables(session, TP.load_twb_xml(a.workbook),
                                                  hyper_paths)
    finally:
        session.close()

    if not report:
        print("Nothing replicated -- no star datasource decoded (a non-star graph "
              "or a single-table source has no relationship view).")
        return
    print(f"\nData model replicated into {pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}:")
    for cap, view, note in report:
        print(f"  {cap}\n    -> {view or '(skipped)'}  [{note}]")
    print("\nDone. Query the view in Snowsight, or re-upload the workbook in the "
          "demo app -- Stage 3a now shows the deployed relationship view.")


if __name__ == "__main__":
    main()

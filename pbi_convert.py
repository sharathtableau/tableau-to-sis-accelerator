"""
pbi_convert.py  --  convert a Power BI project (PBIP) into a Streamlit-in-Snowflake app.

    python pbi_convert.py path/to/Report.pbip -o out/
        [--database DB] [--schema SCHEMA] [--bind Table=DB.SCHEMA.OBJECT ...]
        [--local DB.SCHEMA.OBJECT=data/file.csv ...]   # local preview only

Writes to the output folder:
    report_ir.json            the shared report IR (report-ir/1)
    app_<stem>.py             the generated Streamlit app (embeds the IR)
    conversion_report.md      what converted, what is partial, held or unsupported
    conversion_report.json    the same, machine-readable

Deploy (Snowpark session, no CLI) with deploy(session, out_dir, ...): stages the
app plus PBI_RUNTIME_MODULES and runs CREATE STREAMLIT through the same
pipeline.deploy_streamlit_app the Tableau path uses (runtime pinned to the
warehouse runtime). Database, schema, warehouse and stage are arguments;
nothing account-specific is hard-coded and no credential is written anywhere.
"""

import argparse
import json
import os
import re
import sys

import codegen
import conversion_report
import pbi_parser

PBI_RUNTIME_MODULES = ["report_runtime.py", "report_sql.py", "report_data.py",
                       "report_ir.py", "dax_rules.py", "conversion_report.py",
                       "query_cache.py", "backend.py", "config.py",
                       "calc_translator.py", "findings.py", "profile_superstore.py",
                       "profile_default.py", "environment.yml"]


def stem_of(name):
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(name)).strip("_").lower()
    return s or "report"


def convert(pbip, out_dir, bindings=None):
    ir = pbi_parser.parse_pbip(pbip, bindings=bindings)
    os.makedirs(out_dir, exist_ok=True)
    stem = stem_of(ir["source"]["name"])
    paths = {
        "ir": os.path.join(out_dir, "report_ir.json"),
        "app": os.path.join(out_dir, f"app_{stem}.py"),
        "report_md": os.path.join(out_dir, "conversion_report.md"),
        "report_json": os.path.join(out_dir, "conversion_report.json"),
    }
    with open(paths["ir"], "w", encoding="utf-8") as f:
        json.dump(ir, f, indent=2)
    with open(paths["app"], "w", encoding="utf-8") as f:
        f.write(codegen.build(ir, runtime="report_runtime"))
    with open(paths["report_md"], "w", encoding="utf-8") as f:
        f.write(conversion_report.to_markdown(ir))
    with open(paths["report_json"], "w", encoding="utf-8") as f:
        f.write(conversion_report.to_json(ir))
    return ir, paths


def deploy(session, app_file, *, db, schema, warehouse=None, stage="STREAMLIT_STAGE",
           root_dir=None, title=None):
    """Deploy a generated Power BI app. Needs an explicit target db/schema."""
    import pipeline
    stem = os.path.splitext(os.path.basename(app_file))[0].replace("app_", "", 1)
    return pipeline.deploy_streamlit_app(
        session, stem, app_file, db=db, schema=schema, stage=stage,
        identifier=pipeline._streamlit_identifier(stem, prefix="POWERBI_TO_SIS_"),
        warehouse=warehouse, datasources={}, title=title or f"Power BI -> SiS ({stem})",
        runtime_modules=PBI_RUNTIME_MODULES,
        root_dir=root_dir or os.path.dirname(os.path.abspath(__file__)))


def _pairs(items, what):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--{what} expects NAME=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pbip", help=".pbip file, project folder or .zip of one")
    ap.add_argument("-o", "--out", default="pbi_out")
    ap.add_argument("--database", help="retarget every bound table to this database")
    ap.add_argument("--schema", help="retarget every bound table to this schema")
    ap.add_argument("--bind", action="append", help="Table=DB.SCHEMA.OBJECT override")
    a = ap.parse_args(argv)
    bindings = {"database": a.database, "schema": a.schema,
                "tables": _pairs(a.bind, "bind")}
    try:
        ir, paths = convert(a.pbip, a.out, bindings)
    except pbi_parser.PbipInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    s = conversion_report.summary(ir)
    nv = sum(len(p["visuals"]) for p in ir["pages"])
    print(f"Converted {ir['source']['name']}: {len(ir['pages'])} pages, {nv} visuals, "
          f"{len(ir['model']['measures'])} measures")
    print("  " + ", ".join(f"{conversion_report.LABELS[k]}: {s.get(k, 0)}"
                           for k in ("auto", "partial", "manual", "unsupported")))
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

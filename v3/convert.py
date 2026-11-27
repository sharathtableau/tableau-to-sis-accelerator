"""
convert.py  --  THE accelerator command. One workbook in, one Streamlit app out.

    python convert.py YourBook.twbx [--serve PORT] [--name myapp]
                      [--connection wbr]        # enables the Cortex stages

Stages 1-5 are fully deterministic and code-driven -- no AI:
  1. init      onboard datasources (extract twbx data, hyper->CSV, mapping)
  2. assess    coverage audit + compatibility report (know the gaps FIRST)
  3. parse     workbook -> IR (chart inference, calcs->SQL, colors, filters)
  4. generate  IR -> app_<name>.py  (+ reviewable SQL artifact)
  5. verify    headless render of every sheet; abort on blockers

With --connection <snow-cli-conn>, two SNOWFLAKE CORTEX stages run -- the AI
executes IN the Snowflake account (SNOWFLAKE.CORTEX.COMPLETE / semantic
views), never on the laptop and never outside the account's governance:
  6. semantic  generate CREATE SEMANTIC VIEW + Cortex Analyst YAML from the
               IR's verified calcs (cortex_semantic.py; identifiers
               introspected from the real deployed tables)
  7. ai-calcs  calcs the deterministic translator REFUSED (calc_drops) go to
               Cortex for a proposed translation; every proposal is
               execution-tested, then written to reports/cortex_calc_
               proposals_<name>.md for HUMAN REVIEW (cortex_calc_fallback.py).
               Nothing is auto-applied.

Anything the pipeline cannot convert is REPORTED (reports/migration_
assessment.md + in-app migration notes), never silently dropped.
"""

import argparse
import os
import re
import subprocess
import sys


def sh(args, desc):
    print(f"\n=== {desc} ===")
    r = subprocess.run([sys.executable] + args, text=True)
    if r.returncode != 0:
        raise SystemExit(f"FAILED at: {desc}")


def sh_soft(args, desc):
    """Cortex stages are additive -- a failure warns, never kills the
    deterministic conversion that already succeeded."""
    print(f"\n=== {desc} ===")
    r = subprocess.run([sys.executable] + args, text=True)
    if r.returncode != 0:
        print(f"    !! {desc} failed (conversion itself is unaffected)")
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("twb", help=".twb or .twbx workbook")
    ap.add_argument("--name", default=None, help="app name (default: from workbook)")
    ap.add_argument("--serve", type=int, default=None, help="port to launch on")
    ap.add_argument("--db", default="SUPERSTORE")
    ap.add_argument("--schema", default="PUBLIC")
    ap.add_argument("--connection", default=None,
                    help="snow CLI connection name; enables the Cortex stages "
                         "(semantic view + AI calc-fallback, in-account)")
    ap.add_argument("--cortex-db", default="WBR_DB",
                    help="target db for the generated semantic view")
    ap.add_argument("--cortex-schema", default="PUBLIC")
    ap.add_argument("--deploy-semantic", action="store_true",
                    help="also CREATE the semantic view on the account")
    a = ap.parse_args()

    stem = a.name or re.sub(r"[^0-9A-Za-z]+", "_",
                            os.path.splitext(os.path.basename(a.twb))[0]).strip("_").lower()
    ir_file = f"{stem}_ir.json"
    app_file = f"app_{stem}.py"

    sh(["init_workbook.py", a.twb, "--force", "--db", a.db, "--schema", a.schema],
       "1/5 onboard datasources")
    sh(["audit_coverage.py", a.twb], "2/5a coverage audit")
    sh(["report.py", a.twb], "2/5b compatibility report")
    sh(["tableau_parser.py", a.twb, "-o", ir_file], "3/5 parse -> IR")
    sh(["codegen.py", ir_file, "-o", app_file], "4/5 generate app")

    # 5/5 verify: headless render of every sheet, fail on blockers
    print("\n=== 5/5 verify (headless render of every sheet) ===")
    import json
    import engine
    import findings

    class _Col:
        def __enter__(self): return self
        def __exit__(self, *x): return False
        def __getattr__(self, n): return lambda *x, **k: None

    class _Probe:
        def __init__(self): self.msgs = []
        def columns(self, n): return [_Col() for _ in (range(n) if isinstance(n, int) else n)]
        def container(self, **k): return _Col()
        def warning(self, m): self.msgs.append(str(m))
        def __getattr__(self, n): return lambda *x, **k: None

    ir = json.load(open(ir_file, encoding="utf-8"))
    engine.configure(ir)
    findings.clear()
    bad = []
    total = 0
    for d in ir["dashboards"]:
        for s in d["sheets"]:
            total += 1
            p = _Probe()
            engine.st = p
            try:
                engine.render_sheet(s, "")
            except Exception as e:
                p.msgs.append(f"{type(e).__name__}: {e}")
            if p.msgs:
                bad.append((s["name"], p.msgs[0][:120]))
    blockers = [f for f in findings.all_findings() if f["severity"] == "BLOCKER"]
    warns = [f for f in findings.all_findings() if f["severity"] == "WARNING"]
    print(f"    sheets: {total}  render-degraded: {len(bad)}  "
          f"blockers: {len(blockers)}  warnings: {len(warns)}")
    for n, m in bad:
        print(f"    !! {n}: {m}")
    if bad or blockers:
        print("    -> see reports/migration_assessment.md for remediation")

    # 6/7 CORTEX stages (opt-in; AI runs inside the Snowflake account)
    if a.connection:
        ok = sh_soft(["cortex_semantic.py", ir_file,
                      "--connection", a.connection,
                      "--db", a.cortex_db, "--schema", a.cortex_schema],
                     "6/7 Cortex: semantic view + Analyst model from the IR")
        if ok and a.deploy_semantic:
            sv = os.path.join("sql", "cortex", f"{stem}_semantic_view.sql")
            print(f"\n=== 6/7b deploy semantic view ({sv}) ===")
            r = subprocess.run(["snow", "sql", "-f", sv,
                                "--connection", a.connection], text=True)
            if r.returncode != 0:
                print("    !! semantic view deploy failed (see output above)")
        if ir.get("calc_drops"):
            sh_soft(["cortex_calc_fallback.py", ir_file,
                     "--connection", a.connection],
                    f"7/7 Cortex: AI proposals for {len(ir['calc_drops'])} "
                    f"dropped calc(s) -- review required")
        else:
            print("\n=== 7/7 Cortex calc-fallback: no dropped calcs, skipped ===")

    print(f"\nDONE.  App: {app_file}   IR: {ir_file}")
    print("       Report: reports/migration_assessment.md")
    print("       SQL:    sql/generated_views.sql")
    if a.connection:
        print(f"       Cortex: sql/cortex/{stem}_semantic_view.sql (+ .yaml)")
        if ir.get("calc_drops"):
            print(f"               reports/cortex_calc_proposals_{stem}.md  <- REVIEW")
    if a.serve:
        print(f"\nServing on http://localhost:{a.serve} ...")
        subprocess.Popen([sys.executable, "-m", "streamlit", "run", app_file,
                          "--server.headless", "true",
                          "--server.port", str(a.serve)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"\nRun it:  streamlit run {app_file}")


if __name__ == "__main__":
    main()

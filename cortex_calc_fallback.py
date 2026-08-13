"""
cortex_calc_fallback.py -- Snowflake Cortex AI fallback for the calcs the
deterministic translator refuses (ir['calc_drops']).

WHERE THE AI RUNS: inside Snowflake, via SNOWFLAKE.CORTEX.COMPLETE (Claude on
Cortex). No external API, no data leaves the account -- the AI is a Snowflake
element, same governance as the SQL it writes.

TRUST MODEL (AI proposes, determinism disposes -- never silently injected):
  1. Classify each drop. View-order table calcs (LOOKUP/LAST/PREVIOUS_VALUE/
     RUNNING_*) are SKIPPED with an honest reason -- no model can know the
     view's row order, so guessing is forbidden (house rule).
  2. Candidates go to Cortex with the REAL table schema (introspected via
     DESCRIBE) + the Tableau formula + any referenced calc definitions.
  3. Every proposal is EXECUTED against the real Snowflake table. A proposal
     that does not compile+run is marked FAILED and shown as-is.
  4. Output = reports/cortex_calc_proposals_<book>.md (+ .json) for HUMAN
     REVIEW. Nothing is written into the IR or the app automatically.

Usage:
    python cortex_calc_fallback.py <book>_ir.json --connection wbr
                                   [--mapping datasources.deploy.json]
                                   [--model claude-sonnet-4-5]
"""

import argparse
import json
import os
import re
import subprocess
import tempfile

from calc_translator import to_phys
from cortex_semantic import introspect_columns

ORDER_DEPENDENT = re.compile(
    r"\b(LOOKUP|LAST|FIRST|INDEX|PREVIOUS_VALUE|RUNNING_\w+)\s*\(", re.I)


def snow_sql(sql, connection, timeout=240):
    """Run one SQL text via snow CLI, return (ok, stdout)."""
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False,
                                     encoding="utf-8") as f:
        f.write(sql)
        path = f.name
    try:
        r = subprocess.run(["snow", "sql", "-f", path, "--format", "json",
                            "--connection", connection],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        os.unlink(path)


def json_payload(text):
    """First decodable JSON array in mixed CLI output (warnings, echoes)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        try:
            val, _ = dec.raw_decode(text[m.start():])
            if isinstance(val, list):
                return val
        except Exception:
            continue
    return None


def classify(formula):
    if ORDER_DEPENDENT.search(formula):
        return "order-dependent"            # never AI-guess view order
    if re.search(r"\[federated\.[^\]]+\]|\]\.\[", formula):
        return "blend"                      # references a second datasource
    if formula.count("FIXED") + formula.count("INCLUDE") + formula.count("EXCLUDE") >= 2:
        return "nested-lod"
    return "general"


def _deps(ir, formula):
    """Referenced calc definitions ([Calculation_...] / [caption]) -> lines
    the model needs to resolve nested references."""
    out, seen = [], set()
    names = set(re.findall(r"\[([^\]]+)\]", formula))
    inv = {v: k for k, v in ir.get("colmap", {}).items()}   # internal -> caption
    for n in names:
        cap = inv.get(n, n)
        c = ir.get("calcs", {}).get(cap) or ir.get("calcs", {}).get(n)
        if c and cap not in seen:
            seen.add(cap)
            out.append(f"  [{n}] (caption {cap!r}) already translates to SQL: {c['sql']}")
    return out


def blend_constraint(ir, formula):
    """The REAL link fields of any blend this formula references, as a prompt
    constraint. Returns "" when the formula is not a blend.

    WHY THIS EXISTS: the prompt used to say "join the two tables on the shared
    business keys visible in the schemas" -- i.e. it asked the model to GUESS the
    join. On Superstore's two blend calcs it guessed Region = Segment against the
    wrong source table: SQL that compiled and executed cleanly while being simply
    wrong, which is precisely why fallback output ships as REVIEW rather than as
    app code. The workbook has always DECLARED its link fields
    (<datasource-relationship><column-mapping>); nobody was reading them. Handing
    the model a fact instead of asking it to infer one is the same "give the AI
    more truth" pattern as every other fix in this project."""
    bl = ir.get("blends") or []
    if not bl:
        return ""
    refs = set(re.findall(r"\[(federated\.[^\]]+)\]", formula or ""))
    hits = [b for b in bl
            if b.get("secondary_name") in refs or b.get("primary_name") in refs]
    if not hits and len(bl) == 1:
        hits = bl                      # single blend in the workbook -- unambiguous
    if not hits:
        return ""
    out = ["", "BLEND LINK FIELDS -- these are declared by the workbook itself.",
           "Use EXACTLY these as the join keys. Do not infer keys from column names:"]
    for b in hits:
        pairs = ", ".join("%s = %s" % (l["primary_field"], l["secondary_field"])
                          for l in b["links"]) or "(none declared)"
        out.append(f"  '{b['primary']}' (primary) LEFT JOIN '{b['secondary']}' "
                   f"(secondary) ON {pairs}")
    out.append("Aggregate the SECONDARY to the link-field grain BEFORE joining -- "
               "Tableau blends link an aggregate, so a row-level join would fan "
               "out the primary and double-count its measures.")
    return "\n".join(out)


def build_prompt(cap, formula, kind, tables_ctx, deps, blend_ctx=""):
    return f"""You are translating one Tableau calculated field to Snowflake SQL.

Tableau field name: {cap}
Tableau formula:
{formula}

Construct class: {kind}
{chr(10).join(deps) if deps else ''}

Available Snowflake tables and their REAL column identifiers (quote identifiers
that are not bare uppercase):
{tables_ctx}
{blend_ctx}

Rules:
- Return ONE complete Snowflake SELECT statement that computes this field at
  the grain the LOD/blend semantics imply, aliasing the result as RESULT.
- Nested FIXED LODs: use CTEs or subqueries (window-in-window is illegal).
- Blends: use the declared BLEND LINK FIELDS above verbatim when present; never
  infer join keys from column names. Pre-aggregate the secondary to the link
  grain before joining.
- Use exactly the identifiers listed. No comments, no markdown fences, no
  explanation -- output the SQL statement only."""


def extract_sql(text):
    """COMPLETE returns prose sometimes despite instructions -- take the
    fenced block if present, else from the first SELECT/WITH."""
    if "\\n" in text:
        # snow CLI --format json double-escapes: decode literal \n / \t / \r
        text = text.replace("\\r", "").replace("\\n", "\n").replace("\\t", "  ")
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(WITH|SELECT)\b", text, re.I)
    return text[m.start():].strip() if m else text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ir_json")
    ap.add_argument("--connection", required=True)
    ap.add_argument("--mapping", default="datasources.deploy.json")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    a = ap.parse_args()
    ir = json.load(open(a.ir_json, encoding="utf-8"))
    mapping = json.load(open(a.mapping, encoding="utf-8"))
    drops = ir.get("calc_drops", {})
    if not drops:
        print("no calc_drops in IR -- nothing to do")
        return
    stem = os.path.splitext(os.path.basename(a.ir_json))[0].replace("_ir", "")
    # ONLY this workbook's datasources go into the prompt -- the full account
    # catalog invites the model to pick a same-columned foreign table (the AI
    # version of the Superstore-gravity bug; it happened, GlobalSales round 1).
    wb_mapping = {}
    for cap in ir.get("datasources", []):
        e = mapping.get(cap) or next(
            (v for k, v in mapping.items() if k.startswith(cap)), None)
        if e:
            wb_mapping[cap] = e
    if not wb_mapping:
        print("none of this workbook's datasources are in the mapping -- "
              "load them (load_snowflake.py) first")
        return
    real = introspect_columns(wb_mapping, a.connection)

    def _id(c):
        return c if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", c) else '"' + c + '"'
    tables_ctx = "\n".join(f"  {fqn}: {', '.join(_id(c) for c in cols)}"
                           for fqn, cols in real.items())

    results = []
    for cap, formula in drops.items():
        kind = classify(formula)
        row = {"caption": cap, "formula": formula, "class": kind}
        if kind == "order-dependent":
            row["status"] = "SKIPPED"
            row["reason"] = ("depends on the view's row order (LOOKUP/LAST/"
                             "RUNNING) -- refusing to guess, per accelerator rule")
            results.append(row)
            print(f"[skip]  {cap}: order-dependent")
            continue
        prompt = build_prompt(cap, formula, kind, tables_ctx, _deps(ir, formula),
                              blend_ctx=blend_constraint(ir, formula))
        if "$$" in prompt:
            row["status"] = "SKIPPED"
            row["reason"] = "formula contains $$ (dollar-quote clash)"
            results.append(row)
            continue
        print(f"[ask ]  {cap} ({kind}) -> Cortex {a.model} ...")
        ok, out = snow_sql(
            f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{a.model}', $${prompt}$$) AS R",
            a.connection)
        if not ok:
            row["status"] = "FAILED"
            row["reason"] = "Cortex call failed: " + out[-400:]
            results.append(row)
            continue
        payload = json_payload(out)
        raw = payload[0]["R"] if payload else out
        sql = extract_sql(raw)
        row["proposal"] = sql
        # Gate: the proposal must EXECUTE on the real account
        ok2, out2 = snow_sql(f"SELECT * FROM (\n{sql.rstrip(';')}\n) LIMIT 5",
                             a.connection)
        if ok2:
            rows = json_payload(out2) or []
            row["status"] = "VERIFIED-EXECUTABLE"
            row["sample"] = rows[:5]
            print(f"[ok  ]  {cap}: executes, {len(rows)} sample row(s)")
        else:
            err = re.search(r"Error(.*)", out2, re.S)
            row["status"] = "FAILED-EXECUTION"
            row["reason"] = (err.group(1) if err else out2)[-400:]
            print(f"[fail]  {cap}: proposal does not execute")
        results.append(row)

    os.makedirs("reports", exist_ok=True)
    jpath = os.path.join("reports", f"cortex_calc_proposals_{stem}.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    mpath = os.path.join("reports", f"cortex_calc_proposals_{stem}.md")
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(f"# Cortex calc-translation proposals -- {stem}\n\n"
                f"AI = SNOWFLAKE.CORTEX.COMPLETE({a.model!r}) in-account. Every\n"
                f"proposal below was execution-tested on the real tables. REVIEW\n"
                f"REQUIRED before adopting any of them -- nothing is auto-applied.\n\n")
        for r in results:
            f.write(f"## {r['caption']}  --  {r['status']} ({r['class']})\n\n"
                    f"Tableau formula:\n```\n{r['formula']}\n```\n\n")
            if r.get("proposal"):
                f.write(f"Cortex proposal:\n```sql\n{r['proposal']}\n```\n\n")
            if r.get("sample"):
                f.write(f"Sample result (LIMIT 5): `{json.dumps(r['sample'])[:400]}`\n\n")
            if r.get("reason"):
                f.write(f"Reason: {r['reason']}\n\n")
    n_ok = sum(1 for r in results if r["status"] == "VERIFIED-EXECUTABLE")
    print(f"-> {mpath}  ({n_ok}/{len(results)} proposals verified-executable)")


if __name__ == "__main__":
    main()

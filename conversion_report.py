"""
conversion_report.py  --  the conversion / unsupported-feature report for a
report-ir/1 conversion (Markdown for people, JSON for machines).

Every row of ir["features"] is listed. Nothing converted partially, held or
unsupported is left out; the report states what to do next for each.
"""

import json
from collections import Counter

LABELS = {"auto": "Automatically supported", "partial": "Partially supported",
          "manual": "Requires manual intervention", "unsupported": "Unsupported"}

RUNTIME_SECURITY_NOTE = (
    "**Data access.** The app runs its queries through the Streamlit app's "
    "Snowflake session. On the warehouse runtime (what this accelerator deploys) "
    "queries run with the app owner's rights, so a Snowflake row access policy that "
    "checks the viewer's role does not narrow rows per viewer. Power BI roles are "
    "therefore NOT enforced by this app. Before sharing a report whose model has "
    "roles: apply and test the proposed row access policies, and either restrict "
    "the app to viewers allowed to see all rows or move to a caller's-rights "
    "setup. Query results are cached per viewer, so one viewer is never served "
    "another viewer's cached rows.")


def summary(ir):
    return dict(Counter(f["support"] for f in ir["features"]))


def _md_escape(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(ir):
    src = ir["source"]
    s = summary(ir)
    lines = [f"# Conversion report: {src.get('name')}", "",
             f"Source: {src.get('kind')} ({src.get('format')}), `{src.get('path')}`", "",
             "| Support | Count |", "| --- | --- |"]
    for lvl in ("auto", "partial", "manual", "unsupported"):
        lines.append(f"| {LABELS[lvl]} | {s.get(lvl, 0)} |")
    held = [m for m in ir["model"]["measures"].values() if m["status"] == "held"]
    lines += ["", "## Held measures (not deployed)", ""]
    if held:
        lines.append("These are shown as *Pending verification* in the app until they are "
                     "translated and proven against the published report.")
        lines.append("")
        for m in held:
            lines.append(f"- **{m['name']}**: `{_md_escape(m['dax'])}`. Reason: "
                         f"{_md_escape('; '.join(m['notes']))}")
    else:
        lines.append("None.")
    lines += ["", "## Data bindings", ""]
    for t in ir["model"]["tables"].values():
        b = t.get("binding")
        where = f"`{b['database']}.{b['schema']}.{b['object']}`" if b else "not bound"
        lines.append(f"- {t['name']} ({t['kind']}): {where}")
    lines += ["", "## Security", "", RUNTIME_SECURITY_NOTE, ""]
    for r in ir["model"]["roles"]:
        lines.append(f"### Role {r['name']}")
        lines.append("")
        lines.append("Filters: " + "; ".join(f"`{k}`: `{v}`" for k, v in r["table_permissions"].items()))
        lines.append("")
        lines.append("Proposed policy (review only, not applied):")
        lines.append("")
        lines.append("```sql")
        lines += r["proposed_policy_sql"]
        lines.append("```")
        lines.append("")
    for lvl in ("unsupported", "manual", "partial", "auto"):
        rows = [f for f in ir["features"] if f["support"] == lvl]
        if not rows:
            continue
        lines += [f"## {LABELS[lvl]} ({len(rows)})", "",
                  "| Area | Item | Detail | Follow-up |", "| --- | --- | --- | --- |"]
        for f in rows:
            lines.append(f"| {_md_escape(f['area'])} | {_md_escape(f['item'])} | "
                         f"{_md_escape(f['detail'])} | {_md_escape(f['follow_up'])} |")
        lines.append("")
    lines += ["## Verification status", "",
              "Numbers in this conversion are checked by the accelerator's tests against "
              "known results for the sample data. A conversion is not proven against the "
              "original Power BI report until the published report's numbers are compared "
              "(Phase 2). Treat partial items as unverified until then.", ""]
    return "\n".join(lines)


def to_json(ir):
    return json.dumps({"source": ir["source"], "summary": summary(ir),
                       "features": ir["features"],
                       "held_measures": [m["name"] for m in ir["model"]["measures"].values()
                                         if m["status"] == "held"]}, indent=2)

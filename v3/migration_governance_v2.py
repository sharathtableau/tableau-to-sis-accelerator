"""Governance contracts for the isolated practical-demo accelerator.

This module is intentionally additive.  V1 does not import it and it never
creates, alters, or writes an object in ``WBR_DB.PUBLIC``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any


ARCHITECTURE_VERSION = "practical-demo-v2.0"
IR_SCHEMA_VERSION = "canonical-ir-v1"
TARGET_DATABASE = "WBR_DB"
TARGET_SCHEMA = "TABLEAU_ACCELERATOR_V2"
TARGET_APP = "TABLEAU_TO_SIS_AI_ACCELERATOR_DEMO"

FINAL_STATES = {"PASS", "NEEDS_REVIEW", "BLOCKED", "UNSUPPORTED"}
SUPPORT_LEVELS = {"SUPPORTED", "PARTIAL", "UNSUPPORTED", "AMBIGUOUS"}

SUPPORTED_CHARTS = {
    "area", "bar", "bubble", "combo", "dot", "dtbar", "gantt", "heatmap",
    "kpi", "line", "map", "mbar", "pie", "rank-table", "scatter", "table",
    "text", "treemap",
}
PARTIAL_CHARTS = {"boxplot", "bullet", "density", "histogram", "packed-bubble"}


def _safe(value: Any) -> Any:
    """Return a deterministic JSON-compatible copy of parser/runtime values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    return str(value)


def make_run_id(name: str, raw: bytes | None = None) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256((raw or name.encode("utf-8"))).hexdigest()[:12]
    return f"{stamp}-{digest}"


def _sheets(ir: dict) -> list[tuple[dict, dict]]:
    return [(dash, sheet) for dash in (ir.get("dashboards") or [])
            for sheet in (dash.get("sheets") or [])]


def assess_compatibility(ir: dict, discovery: dict | None = None,
                         model: list | None = None) -> dict:
    """Assess feature classes before approval; never infer successful rendering."""
    discovery = discovery or {}
    model = model or []
    feature_rows = []
    for dash, sheet in _sheets(ir):
        kind = str(sheet.get("kind") or sheet.get("mark") or "unknown").lower()
        if kind in SUPPORTED_CHARTS:
            support, confidence = "SUPPORTED", 0.95
        elif kind in PARTIAL_CHARTS:
            support, confidence = "PARTIAL", 0.65
        else:
            support, confidence = "AMBIGUOUS", 0.35
        feature_rows.append({
            "dashboard": dash.get("title") or dash.get("name"),
            "sheet": sheet.get("name"), "feature_class": f"chart:{kind}",
            "support": support, "confidence": confidence,
            "evidence": {"kind": kind, "mark": sheet.get("mark")},
        })

    blockers = []
    for caption in discovery.get("blocked") or []:
        blockers.append({"category": "DATA", "severity": "CRITICAL",
                         "item": caption, "detail": "Extract is not readable in this runtime"})
    for caption in discovery.get("missing") or []:
        blockers.append({"category": "DATA", "severity": "CRITICAL",
                         "item": caption, "detail": "Datasource is not available in Snowflake"})
    if not ir.get("dashboards"):
        blockers.append({"category": "LAYOUT", "severity": "CRITICAL",
                         "item": "workbook", "detail": "No dashboards were extracted"})

    counts = {level: sum(1 for row in feature_rows if row["support"] == level)
              for level in SUPPORT_LEVELS}
    denom = max(len(feature_rows), 1)
    confidence = round(sum(row["confidence"] for row in feature_rows) / denom, 3)
    if blockers:
        status = "BLOCKED"
    elif counts["AMBIGUOUS"] or counts["UNSUPPORTED"] or counts["PARTIAL"]:
        status = "PARTIAL"
    else:
        status = "SUPPORTED"
    return {
        "architecture_version": ARCHITECTURE_VERSION,
        "status": status,
        "confidence": confidence,
        "inventory": {
            "dashboards": len(ir.get("dashboards") or []),
            "worksheets": len(_sheets(ir)),
            "datasources": len(ir.get("datasources") or discovery.get("datasources") or []),
            "calculations": len(ir.get("calcs") or {}) + len(ir.get("calc_drops") or {}),
            "filters": sum(len(sheet.get("filters") or []) for _, sheet in _sheets(ir)),
            "parameters": len(ir.get("params") or {}),
            "relationships": sum(len(entry.get("joins") or []) for entry in model),
        },
        "feature_counts": counts,
        "features": feature_rows,
        "blockers": blockers,
        "findings": [{"category": "CALCULATION", "severity": "HIGH", "item": name,
                      "detail": str(formula), "support": "UNSUPPORTED"}
                     for name, formula in (ir.get("calc_drops") or {}).items()],
    }


def build_canonical_ir(ir: dict, discovery: dict | None = None,
                       model: list | None = None) -> dict:
    """Wrap the existing parser IR in the four permanent architecture domains."""
    discovery = discovery or {}
    model = model or []
    dashboards = ir.get("dashboards") or []
    return _safe({
        "schema_version": IR_SCHEMA_VERSION,
        "architecture_version": ARCHITECTURE_VERSION,
        "source": {"file": ir.get("source_file"),
                   "datasource_evidence": discovery.get("datasources") or []},
        "semantic": {
            "datasources": ir.get("datasources") or [],
            "calculations": ir.get("calcs") or {},
            "calculation_drops": ir.get("calc_drops") or {},
            "parameters": ir.get("params") or {},
            "parameter_domains": ir.get("param_domains") or {},
            "relationships": model,
        },
        "visual": {"dashboards": [{
            "name": d.get("name"), "title": d.get("title"),
            "sheets": [{k: v for k, v in s.items()
                        if k not in {"filters", "geom", "layout"}}
                       for s in (d.get("sheets") or [])],
        } for d in dashboards]},
        "layout": {"dashboards": [{
            "name": d.get("name"), "title": d.get("title"),
            "layout": d.get("layout"),
            "zones": [{"sheet": s.get("name"), "geometry": s.get("geom")}
                      for s in (d.get("sheets") or [])],
        } for d in dashboards]},
        "interaction": {
            "parameters": ir.get("params") or {},
            "dashboards": [{"name": d.get("name"),
                            "filters": d.get("filters") or [],
                            "actions": d.get("actions") or [],
                            "sheet_filters": [{"sheet": s.get("name"),
                                               "filters": s.get("filters") or []}
                                              for s in (d.get("sheets") or [])]}
                           for d in dashboards],
        },
    })


def build_dashboard_matrix(ir: dict, validation: dict | None = None) -> list[dict]:
    """One honest acceptance row per dashboard; missing evidence is NOT_TESTED."""
    validation = validation or {}
    summary = validation.get("summary") or {}
    data_state = "BLOCKED" if summary.get("measures_bug", 0) else (
        "PASS" if summary.get("measures_checked", 0) else "NOT_TESTED")
    rows = []
    for dash in ir.get("dashboards") or []:
        sheets = dash.get("sheets") or []
        ambiguous = [s for s in sheets if str(s.get("kind") or "unknown").lower()
                     not in SUPPORTED_CHARTS]
        rows.append({
            "dashboard": dash.get("title") or dash.get("name"),
            "expected_sheets": len(sheets),
            "structural": "PASS" if sheets and not ambiguous else
                          ("BLOCKED" if not sheets else "NEEDS_REVIEW"),
            "data": data_state,
            "charts": "NEEDS_REVIEW" if ambiguous else "STRUCTURAL_PASS",
            "layout": "NOT_TESTED",
            "interactions": "NOT_TESTED",
            "visual": "NOT_TESTED",
            "findings": [f"Unclassified chart: {s.get('name')} ({s.get('kind')})"
                         for s in ambiguous],
        })
    return rows


def decide(assessment: dict, matrix: list[dict], cortex_review: dict | None = None) -> dict:
    """Apply hard gates. AI findings can lower trust but can never approve a run."""
    reasons = []
    if assessment.get("blockers"):
        state = "BLOCKED"
        reasons.extend(b.get("detail", "Compatibility blocker")
                       for b in assessment["blockers"])
    elif any(r.get("structural") == "BLOCKED" or r.get("data") == "BLOCKED"
             for r in matrix):
        state = "BLOCKED"
        reasons.append("At least one dashboard has a structural or data blocker")
    elif any(r.get("charts") == "NEEDS_REVIEW" for r in matrix):
        state = "NEEDS_REVIEW"
        reasons.append("At least one chart mapping is ambiguous or partial")
    elif any(r.get(area) in {"NOT_TESTED", "NEEDS_REVIEW"}
             for r in matrix for area in ("layout", "interactions", "visual")):
        state = "NEEDS_REVIEW"
        reasons.append("Layout, interaction, or visual evidence is incomplete")
    else:
        state = "PASS"
    if cortex_review and cortex_review.get("status") in {"BLOCKED", "NEEDS_REVIEW", "UNSUPPORTED"}:
        if state == "PASS":
            state = "NEEDS_REVIEW"
        reasons.append("The advisory Cortex critic reported review findings")
    assert state in FINAL_STATES
    return {"state": state, "reasons": reasons,
            "architecture_version": ARCHITECTURE_VERSION,
            "decided_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}


CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "dashboard": {"type": "string"},
        "status": {"type": "string", "enum": ["PASS", "NEEDS_REVIEW", "BLOCKED", "UNSUPPORTED"]},
        "confidence": {"type": "number"},
        "discrepancies": {"type": "array", "items": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["DATA", "CALCULATION", "DATASOURCE_ROUTING", "FILTER", "PARAMETER", "CHART_TYPE", "ENCODING", "LAYOUT", "LABEL", "FORMAT", "INTERACTION", "UNSUPPORTED"]},
            "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
            "component": {"type": "string"}, "evidence": {"type": "string"},
            "recommended_action": {"type": "string"}, "safe_for_override": {"type": "boolean"},
        }, "required": ["category", "severity", "component", "evidence", "recommended_action", "safe_for_override"]}},
    },
    "required": ["dashboard", "status", "confidence", "discrepancies"],
}


def _sql_lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cortex_critic(session, payload: dict, model: str | None = None) -> dict:
    """Run schema-constrained Cortex review; unavailability never fails migration."""
    if session is None:
        return {"status": "UNAVAILABLE", "confidence": 0.0, "discrepancies": [],
                "error": "No Snowflake session"}
    model = model or os.environ.get("AI_ACCEL_CORTEX_MODEL", "claude-sonnet-4-5")
    prompt = ("You are a Tableau-to-Streamlit migration critic. Diagnose only from "
              "the supplied evidence. Do not invent source values and do not emit code. "
              "Return the required JSON object. Evidence:\n" +
              json.dumps(_safe(payload), separators=(",", ":")))
    fmt = {"type": "json", "schema": CRITIC_SCHEMA}
    sql = ("SELECT AI_COMPLETE(model => " + _sql_lit(model) +
           ", prompt => " + _sql_lit(prompt) +
           ", response_format => PARSE_JSON(" + _sql_lit(json.dumps(fmt)) + ")) AS R")
    try:
        rows = session.sql(sql).collect()
        raw = rows[0][0] if rows else None
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict) or value.get("status") not in FINAL_STATES:
            raise ValueError("Cortex response did not satisfy the critic contract")
        value["model"] = model
        return _safe(value)
    except Exception as exc:
        return {"status": "UNAVAILABLE", "confidence": 0.0, "discrepancies": [],
                "model": model, "error": f"{type(exc).__name__}: {exc}"}


DDL = (
    "CREATE TABLE IF NOT EXISTS WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_MIGRATION_RUNS "
    "(RUN_ID STRING, CREATED_AT TIMESTAMP_TZ, WORKBOOK_NAME STRING, ARCHITECTURE_VERSION STRING, FINAL_STATE STRING, PAYLOAD VARIANT)",
    "CREATE TABLE IF NOT EXISTS WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_ASSESSMENTS "
    "(RUN_ID STRING, CREATED_AT TIMESTAMP_TZ, ASSESSMENT VARIANT, CANONICAL_IR VARIANT)",
    "CREATE TABLE IF NOT EXISTS WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_CORTEX_REVIEWS "
    "(RUN_ID STRING, CREATED_AT TIMESTAMP_TZ, REVIEW VARIANT)",
    "CREATE TABLE IF NOT EXISTS WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_VALIDATION_EVIDENCE "
    "(RUN_ID STRING, CREATED_AT TIMESTAMP_TZ, DASHBOARD STRING, EVIDENCE VARIANT)",
)


def ensure_persistence(session) -> None:
    for statement in DDL:
        session.sql(statement).collect()


def persist_run(session, run: dict) -> dict:
    """Persist only to the dedicated V2 schema using bound values."""
    if session is None:
        return {"status": "SKIPPED", "reason": "No Snowflake session"}
    ensure_persistence(session)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    run_id = run["run_id"]
    session.sql(
        "INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_MIGRATION_RUNS "
        "SELECT ?, TO_TIMESTAMP_TZ(?), ?, ?, ?, PARSE_JSON(?)",
        params=[run_id, now, run.get("name"), ARCHITECTURE_VERSION,
                run["decision"]["state"], json.dumps(_safe({"origin": run.get("origin")}))],
    ).collect()
    session.sql(
        "INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_ASSESSMENTS "
        "SELECT ?, TO_TIMESTAMP_TZ(?), PARSE_JSON(?), PARSE_JSON(?)",
        params=[run_id, now, json.dumps(run["assessment"]), json.dumps(run["canonical_ir"])],
    ).collect()
    if run.get("cortex_review"):
        session.sql(
            "INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_CORTEX_REVIEWS "
            "SELECT ?, TO_TIMESTAMP_TZ(?), PARSE_JSON(?)",
            params=[run_id, now, json.dumps(run["cortex_review"])],
        ).collect()
    for row in run.get("dashboard_matrix") or []:
        session.sql(
            "INSERT INTO WBR_DB.TABLEAU_ACCELERATOR_V2.AI_ACCEL_VALIDATION_EVIDENCE "
            "SELECT ?, TO_TIMESTAMP_TZ(?), ?, PARSE_JSON(?)",
            params=[run_id, now, row.get("dashboard"), json.dumps(row)],
        ).collect()
    return {"status": "PERSISTED", "run_id": run_id}

from __future__ import annotations

import csv
import html
import json
import shutil
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


PASS = "PASS"
REVIEW = "REVIEW"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_VALIDATED = "NOT_VALIDATED"

_STATUS_PRIORITY = {PASS: 0, REVIEW: 1, NOT_VALIDATED: 2, BLOCKED: 3, FAIL: 4}


def _d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_id(value: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in cleaned.split("-") if part)


def _status_of(statuses: Iterable[str]) -> str:
    values = list(statuses)
    return max(values, key=lambda item: _STATUS_PRIORITY.get(item, 4), default=BLOCKED)


def derive_tolerance(measure: dict[str, Any], reference: Decimal | None = None) -> Decimal:
    """
    Derive a defensible tolerance from the compared value's precision.

    Required input convention:
    - Currency/number values are raw units, not formatted strings.
    - Percent values use either fraction scale (0.125) or percentage-point scale (12.5).
    - Counts, keys, ranks, dates and strings are exact.
    """
    if measure.get("absolute_tolerance") is not None:
        absolute = Decimal(str(measure["absolute_tolerance"]))
    else:
        kind = measure.get("kind", "number")
        decimals = int(measure.get("display_decimals", 2))
        if kind in {"count", "integer", "rank", "key"}:
            absolute = Decimal("0")
        elif kind == "percent" and measure.get("value_scale", "fraction") == "fraction":
            absolute = Decimal("0.5") * (Decimal(10) ** Decimal(-(decimals + 2)))
        else:
            absolute = Decimal("0.5") * (Decimal(10) ** Decimal(-decimals))

    relative = Decimal(str(measure.get("relative_tolerance", "0")))
    if reference is None or relative == 0:
        return absolute
    return max(absolute, abs(reference) * relative)


def format_value(value: Any, measure: dict[str, Any]) -> str:
    number = _d(value)
    if number is None:
        return "Missing"

    kind = measure.get("kind", "number")
    decimals = int(measure.get("display_decimals", 2))
    if kind == "currency":
        return f"${number:,.{decimals}f}"
    if kind in {"count", "integer", "rank"}:
        return f"{number:,.0f}"
    if kind == "percent":
        if measure.get("value_scale", "fraction") == "fraction":
            number *= Decimal(100)
        return f"{number:,.{decimals}f}%"
    return f"{number:,.{decimals}f}"


def format_tolerance(value: Decimal, measure: dict[str, Any]) -> str:
    kind = measure.get("kind", "number")
    decimals = int(measure.get("display_decimals", 2))
    if kind == "currency":
        shown = max(decimals, 2)
        return f"${value:,.{shown}f}"
    if kind == "percent":
        if measure.get("value_scale", "fraction") == "fraction":
            value *= Decimal(100)
        return f"{value:,.{max(decimals, 2)}f} pp"
    if kind in {"count", "integer", "rank", "key"}:
        return "Exact"
    return f"{value:,.{max(decimals, 3)}f}"


@dataclass
class IndexedRows:
    rows: dict[tuple[Any, ...], dict[str, Any]]
    order: list[tuple[Any, ...]]
    duplicates: list[tuple[Any, ...]]
    null_keys: list[int]


def _index_rows(rows: list[dict[str, Any]], grain: list[str]) -> IndexedRows:
    indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    duplicates: list[tuple[Any, ...]] = []
    null_keys: list[int] = []

    for position, row in enumerate(rows):
        key = tuple(row.get(field) for field in grain)
        if any(value is None or value == "" for value in key):
            null_keys.append(position)
        if key in indexed:
            duplicates.append(key)
        else:
            indexed[key] = row
            order.append(key)

    return IndexedRows(indexed, order, duplicates, null_keys)


def _formula_status(formula: dict[str, Any]) -> str:
    classification = formula.get("classification", "NOT_VALIDATED").upper()
    approved = bool(formula.get("approved", False))
    if classification in {"EXACT", "SEMANTICALLY_EQUIVALENT"}:
        return PASS
    if classification in {"EQUIVALENT_AT_CURRENT_GRAIN", "INTENTIONAL_DIFFERENCE"}:
        return PASS if approved else REVIEW
    if classification == "MISMATCH":
        return FAIL
    return BLOCKED


def compare_chart(chart: dict[str, Any]) -> dict[str, Any]:
    grain = list(chart.get("grain", []))
    measures = list(chart.get("measures", []))
    sources = {
        "tableau": _index_rows(list(chart.get("tableau_rows", [])), grain),
        "streamlit": _index_rows(list(chart.get("streamlit_rows", [])), grain),
        "backend": _index_rows(list(chart.get("backend_rows", [])), grain),
    }

    key_sets = {name: set(source.rows) for name, source in sources.items()}
    all_keys = set().union(*key_sets.values()) if key_sets else set()
    key_set_match = len({frozenset(value) for value in key_sets.values()}) == 1
    order_match = sources["tableau"].order == sources["streamlit"].order
    structural_failure = (
        not grain
        or not measures
        or not key_set_match
        or not order_match
        or any(source.duplicates or source.null_keys for source in sources.values())
    )

    comparison_rows: list[dict[str, Any]] = []
    failed_cells = 0
    max_difference = Decimal("0")

    tableau_order = sources["tableau"].order
    remaining = sorted(all_keys - set(tableau_order), key=lambda item: tuple(str(v) for v in item))
    ordered_keys = tableau_order + remaining

    for key in ordered_keys:
        row_result: dict[str, Any] = {
            "key": dict(zip(grain, key)),
            "key_tuple": key,
            "measures": [],
            "status": PASS,
        }
        for measure in measures:
            field = measure.get("field", measure["name"])
            values = {
                name: source.rows.get(key, {}).get(field)
                for name, source in sources.items()
            }
            decimals = {name: _d(value) for name, value in values.items()}
            missing = any(value is None for value in decimals.values())
            tolerance = derive_tolerance(measure, decimals["backend"])
            pair_diffs: dict[str, Decimal | None] = {
                "tableau_streamlit": None,
                "streamlit_backend": None,
                "tableau_backend": None,
            }
            if not missing:
                pair_diffs = {
                    "tableau_streamlit": abs(decimals["tableau"] - decimals["streamlit"]),
                    "streamlit_backend": abs(decimals["streamlit"] - decimals["backend"]),
                    "tableau_backend": abs(decimals["tableau"] - decimals["backend"]),
                }
                cell_max = max(value for value in pair_diffs.values() if value is not None)
                max_difference = max(max_difference, cell_max)
                cell_status = PASS if all(value <= tolerance for value in pair_diffs.values() if value is not None) else FAIL
            else:
                cell_max = None
                cell_status = BLOCKED

            if cell_status != PASS:
                failed_cells += 1
                row_result["status"] = cell_status

            row_result["measures"].append(
                {
                    "name": measure["name"],
                    "field": field,
                    "definition": measure,
                    "values": values,
                    "formatted": {name: format_value(value, measure) for name, value in values.items()},
                    "pair_diffs": pair_diffs,
                    "max_difference": cell_max,
                    "formatted_difference": "Missing" if cell_max is None else format_value(cell_max, measure),
                    "tolerance": tolerance,
                    "formatted_tolerance": format_tolerance(tolerance, measure),
                    "status": cell_status,
                }
            )
        comparison_rows.append(row_result)

    formula_results = []
    for formula in chart.get("formulas", []):
        result = dict(formula)
        result["status"] = _formula_status(formula)
        formula_results.append(result)

    interaction_results = []
    for interaction in chart.get("interactions", []):
        result = dict(interaction)
        result["status"] = interaction.get("status", NOT_VALIDATED).upper()
        interaction_results.append(result)

    evidence_complete = bool(
        chart.get("tableau_rows") is not None
        and chart.get("streamlit_rows") is not None
        and chart.get("backend_rows") is not None
    )
    statuses = [row["status"] for row in comparison_rows]
    statuses.extend(item["status"] for item in formula_results)
    statuses.extend(item["status"] for item in interaction_results)
    if structural_failure:
        statuses.append(FAIL)
    if not evidence_complete or not comparison_rows:
        statuses.append(BLOCKED)

    result = dict(chart)
    result.update(
        {
            "id": chart.get("id", _safe_id(chart["title"])),
            "source_counts": {name: len(source.rows) for name, source in sources.items()},
            "duplicates": {name: source.duplicates for name, source in sources.items()},
            "null_keys": {name: source.null_keys for name, source in sources.items()},
            "key_set_match": key_set_match,
            "order_match": order_match,
            "comparison_rows": comparison_rows,
            "formula_results": formula_results,
            "interaction_results": interaction_results,
            "failed_cells": failed_cells,
            "max_difference": max_difference,
            "evidence_complete": evidence_complete,
            "status": _status_of(statuses),
        }
    )
    return result


def _visual_status(dashboard: dict[str, Any], copied_images: dict[str, str | None]) -> str:
    visual = dashboard.get("visual", {})
    if not copied_images.get("tableau") or not copied_images.get("streamlit"):
        return BLOCKED
    semantic = [check.get("status", NOT_VALIDATED).upper() for check in visual.get("checks", [])]
    if any(status == FAIL for status in semantic):
        return FAIL
    if any(status in {BLOCKED, NOT_VALIDATED} for status in semantic):
        return BLOCKED
    similarity = visual.get("similarity")
    threshold = visual.get("threshold", 0.95)
    if similarity is None:
        return BLOCKED
    return PASS if float(similarity) >= float(threshold) else REVIEW


def _copy_image(source: str | None, destination: Path) -> str | None:
    if not source:
        return None
    path = Path(source)
    if not path.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination.as_posix()


def _image_destination(directory: Path, name: str, source: str | None) -> Path:
    suffix = Path(source).suffix if source else ".png"
    return directory / f"{name}{suffix or '.png'}"


def validate_run(spec: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    evidence_root = output_dir / "evidence"
    validated_dashboards = []

    for dashboard_position, dashboard in enumerate(spec.get("dashboards", []), start=1):
        dashboard_id = dashboard.get("id", _safe_id(dashboard["name"]))
        dashboard_dir = evidence_root / dashboard_id
        images = {
            "tableau": _copy_image(
                dashboard.get("visual", {}).get("tableau_screenshot"),
                _image_destination(dashboard_dir, "tableau", dashboard.get("visual", {}).get("tableau_screenshot")),
            ),
            "streamlit": _copy_image(
                dashboard.get("visual", {}).get("streamlit_screenshot"),
                _image_destination(dashboard_dir, "streamlit", dashboard.get("visual", {}).get("streamlit_screenshot")),
            ),
            "diff": _copy_image(
                dashboard.get("visual", {}).get("diff_image"),
                _image_destination(dashboard_dir, "visual-diff", dashboard.get("visual", {}).get("diff_image")),
            ),
        }

        charts = []
        for chart in dashboard.get("charts", []):
            result = compare_chart(chart)
            chart_dir = dashboard_dir / "charts" / result["id"]
            chart_dir.mkdir(parents=True, exist_ok=True)
            result["comparison_csv"] = str((chart_dir / "comparison.csv").relative_to(output_dir)).replace("\\", "/")
            _write_comparison_csv(result, output_dir / result["comparison_csv"])
            charts.append(result)

        formulas = _deduplicate_formulas(charts)
        interactions = [item for chart in charts for item in chart["interaction_results"]]
        visual_status = _visual_status(dashboard, images)
        dashboard_status = _status_of(
            [visual_status]
            + [chart["status"] for chart in charts]
            + [item["status"] for item in formulas]
            + [item["status"] for item in interactions]
        )
        validated = dict(dashboard)
        validated.update(
            {
                "id": dashboard_id,
                "position": dashboard_position,
                "images": {
                    key: (str(Path(value).relative_to(output_dir)).replace("\\", "/") if value else None)
                    for key, value in images.items()
                },
                "visual_status": visual_status,
                "charts": charts,
                "formulas": formulas,
                "interactions": interactions,
                "status": dashboard_status,
                "proof_complete": visual_status != BLOCKED and all(chart["evidence_complete"] for chart in charts),
            }
        )
        validated_dashboards.append(validated)

    workbook_status = _status_of([dashboard["status"] for dashboard in validated_dashboards])
    result = dict(spec)
    result["dashboards"] = validated_dashboards
    result["status"] = workbook_status
    result["summary"] = {
        "dashboards": len(validated_dashboards),
        "passed": sum(item["status"] == PASS for item in validated_dashboards),
        "review": sum(item["status"] == REVIEW for item in validated_dashboards),
        "failed_or_blocked": sum(item["status"] in {FAIL, BLOCKED} for item in validated_dashboards),
        "charts": sum(len(item["charts"]) for item in validated_dashboards),
    }
    return result


def _deduplicate_formulas(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formulas: dict[str, dict[str, Any]] = {}
    for chart in charts:
        for formula in chart["formula_results"]:
            key = formula.get("id") or formula.get("metric") or json.dumps(formula, sort_keys=True)
            if key not in formulas:
                formulas[key] = dict(formula)
                formulas[key]["used_by"] = [chart["title"]]
            else:
                formulas[key]["used_by"].append(chart["title"])
    return list(formulas.values())


def _write_comparison_csv(chart: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grain = list(chart["grain"])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            grain
            + ["measure", "tableau", "streamlit", "backend", "tableau_streamlit_diff",
               "streamlit_backend_diff", "tableau_backend_diff", "tolerance", "status"]
        )
        for row in chart["comparison_rows"]:
            key_values = [row["key"].get(field) for field in grain]
            for measure in row["measures"]:
                diffs = measure["pair_diffs"]
                writer.writerow(
                    key_values
                    + [
                        measure["name"],
                        measure["values"]["tableau"],
                        measure["values"]["streamlit"],
                        measure["values"]["backend"],
                        diffs["tableau_streamlit"],
                        diffs["streamlit_backend"],
                        diffs["tableau_backend"],
                        measure["tolerance"],
                        measure["status"],
                    ]
                )


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(status: str) -> str:
    css = {
        PASS: "pass",
        REVIEW: "review",
        FAIL: "fail",
        BLOCKED: "fail",
        NOT_VALIDATED: "na",
    }.get(status, "na")
    return f'<span class="badge {css}">{_e(status.replace("_", " "))}</span>'


def _image_or_missing(path: str | None, label: str) -> str:
    if path:
        return f'<img src="{_e(path)}" alt="{_e(label)}">'
    return f'<div class="missing">Missing required {_e(label)}</div>'


def _comparison_table(chart: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    grain = list(chart["grain"])
    parts = ['<div class="table-wrap"><table><thead><tr>']
    for field in grain:
        parts.append(f"<th>{_e(field)}</th>")
    parts.extend(
        [
            "<th>Measure</th>",
            '<th class="num">Tableau</th>',
            '<th class="num">Streamlit</th>',
            '<th class="num">Backend</th>',
            '<th class="num">Difference</th>',
            '<th class="num">Tolerance</th>',
            "<th>Status</th></tr></thead><tbody>",
        ]
    )
    for row in rows:
        for measure in row["measures"]:
            parts.append("<tr>")
            for field in grain:
                parts.append(f"<td>{_e(row['key'].get(field))}</td>")
            parts.extend(
                [
                    f"<td>{_e(measure['name'])}</td>",
                    f'<td class="num">{_e(measure["formatted"]["tableau"])}</td>',
                    f'<td class="num">{_e(measure["formatted"]["streamlit"])}</td>',
                    f'<td class="num">{_e(measure["formatted"]["backend"])}</td>',
                    f'<td class="num">{_e(measure["formatted_difference"])}</td>',
                    f'<td class="num">{_e(measure["formatted_tolerance"])}</td>',
                    f"<td>{_badge(measure['status'])}</td>",
                ]
            )
            parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _chart_html(chart: dict[str, Any], open_by_default: bool) -> str:
    rows = chart["comparison_rows"]
    failures = [row for row in rows if row["status"] != PASS]
    if len(rows) <= 20:
        preview = rows
        full_block = ""
        note = f"All {len(rows)} chart-grain rows are shown."
    else:
        preview_keys: set[tuple[Any, ...]] = set()
        preview = []
        for row in rows[:3] + failures:
            if row["key_tuple"] not in preview_keys:
                preview.append(row)
                preview_keys.add(row["key_tuple"])
        full_block = (
            f'<details class="full-data"><summary>View all {len(rows)} compared rows</summary>'
            f'<p><a class="button-link" href="{_e(chart["comparison_csv"])}" download>Download full comparison CSV</a></p>'
            f"{_comparison_table(chart, rows)}</details>"
        )
        note = f"Showing {len(preview)} preview or failed rows. The complete {len(rows)}-row comparison is available below."

    structural = (
        f'{chart["source_counts"]["tableau"]} / {chart["source_counts"]["streamlit"]} / '
        f'{chart["source_counts"]["backend"]} source rows; '
        f'key sets {"match" if chart["key_set_match"] else "do not match"}; '
        f'visual order {"matches" if chart["order_match"] else "does not match"}.'
    )
    return (
        f'<details class="chart" {"open" if open_by_default else ""}>'
        f'<summary><span><b>{_e(chart["title"])}</b><small>{_e(chart.get("chart_type", "Chart"))} | '
        f'Grain: {_e(", ".join(chart["grain"]))}</small></span>{_badge(chart["status"])}</summary>'
        f'<div class="chart-body"><p class="proof-line">{_e(structural)}</p>'
        f'{_comparison_table(chart, preview)}<p class="note">{_e(note)}</p>{full_block}</div></details>'
    )


def _formula_html(formulas: list[dict[str, Any]]) -> str:
    if not formulas:
        return '<p class="note">No formulas were supplied. Formula validation is blocked.</p>'
    rows = []
    for item in formulas:
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('metric'))}</td>"
            f"<td>{_e(', '.join(item.get('used_by', [])))}</td>"
            f"<td><code>{_e(item.get('tableau'))}</code></td>"
            f"<td><code>{_e(item.get('streamlit'))}</code></td>"
            f"<td>{_e(item.get('classification'))}</td>"
            f"<td>{_e(item.get('impact', 'None'))}</td>"
            f"<td>{_badge(item['status'])}</td></tr>"
        )
    status = _status_of(item["status"] for item in formulas)
    return (
        f'<details class="logic" {"open" if status != PASS else ""}><summary>'
        f'{len(formulas)} unique calculations {_badge(status)}</summary>'
        '<div class="table-wrap"><table><thead><tr><th>Metric</th><th>Used by</th><th>Tableau</th>'
        '<th>Streamlit / SQL</th><th>Classification</th><th>Impact</th><th>Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def _interaction_html(interactions: list[dict[str, Any]]) -> str:
    if not interactions:
        return '<p class="note">No interaction evidence supplied.</p>'
    rows = []
    for item in interactions:
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('name'))}</td><td>{_e(item.get('tableau'))}</td>"
            f"<td>{_e(item.get('streamlit'))}</td><td>{_e(item.get('proof'))}</td>"
            f"<td>{_badge(item['status'])}</td></tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Scenario</th><th>Tableau expected</th>'
        '<th>Streamlit observed</th><th>Proof</th><th>Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_html(run: dict[str, Any]) -> str:
    dashboard_nav = "".join(
        f'<a href="#{_e(item["id"])}">{index}. {_e(item["name"])}</a>'
        for index, item in enumerate(run["dashboards"], start=1)
    )
    summary_rows = "".join(
        "<tr>"
        f'<td><a href="#{_e(item["id"])}"><b>{_e(item["name"])}</b></a></td>'
        f"<td>{len(item['charts'])}</td><td>{_badge(item['visual_status'])}</td>"
        f"<td>{sum(chart['status'] == PASS for chart in item['charts'])} / {len(item['charts'])}</td>"
        f"<td>{_badge(item['status'])}</td></tr>"
        for item in run["dashboards"]
    )

    dashboard_sections = []
    for dashboard_index, dashboard in enumerate(run["dashboards"], start=1):
        chart_index = "".join(
            "<tr>"
            f"<td>{_e(chart['title'])}</td><td>{_e(', '.join(chart['grain']))}</td>"
            f"<td>{chart['source_counts']['tableau']} / {chart['source_counts']['streamlit']} / {chart['source_counts']['backend']}</td>"
            f"<td>{chart['failed_cells']}</td><td>{_badge(chart['status'])}</td></tr>"
            for chart in dashboard["charts"]
        )
        chart_details = "".join(
            _chart_html(chart, chart_position == 1 or chart["status"] != PASS)
            for chart_position, chart in enumerate(dashboard["charts"], start=1)
        )
        visual = dashboard.get("visual", {})
        checks = "".join(
            f"<tr><td>{_e(check.get('name'))}</td><td>{_e(check.get('observed'))}</td>"
            f"<td>{_e(check.get('threshold'))}</td><td>{_badge(check.get('status', NOT_VALIDATED).upper())}</td></tr>"
            for check in visual.get("checks", [])
        )
        dashboard_sections.append(
            f'<section class="dashboard" id="{_e(dashboard["id"])}">'
            f'<div class="dashboard-head"><div><small>Dashboard {dashboard_index} of {len(run["dashboards"])}</small>'
            f'<h2>{_e(dashboard["name"])}</h2></div>{_badge(dashboard["status"])}</div>'
            '<h3>A. Full dashboard visual proof</h3><div class="screens">'
            f'<figure>{_image_or_missing(dashboard["images"]["tableau"], "Tableau dashboard screenshot")}<figcaption>Tableau</figcaption></figure>'
            f'<figure>{_image_or_missing(dashboard["images"]["streamlit"], "Streamlit dashboard screenshot")}<figcaption>Streamlit</figcaption></figure>'
            '</div>'
            f'<p class="proof-line">Similarity: {_e(visual.get("similarity", "Not scored"))}; visual status: {_e(dashboard["visual_status"])}</p>'
            f'<div class="table-wrap"><table><thead><tr><th>Visual check</th><th>Observed</th><th>Threshold</th><th>Status</th></tr></thead><tbody>{checks}</tbody></table></div>'
            '<h3>B. Per-chart data validation</h3>'
            '<div class="table-wrap"><table><thead><tr><th>Chart</th><th>Validation grain</th>'
            '<th>Tableau / Streamlit / Backend rows</th><th>Failed values</th><th>Status</th></tr></thead>'
            f'<tbody>{chart_index}</tbody></table></div>{chart_details}'
            f'<h3>C. Logic and calculation validation</h3>{_formula_html(dashboard["formulas"])}'
            f'<h3>D. Filters and interactions</h3>{_interaction_html(dashboard["interactions"])}'
            '</section>'
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(run.get("workbook", "Workbook"))} Validation Report</title>
<style>
:root{{--ink:#17202a;--muted:#667085;--line:#d8dde5;--soft:#f6f8fa;--paper:#fff;--canvas:#eef1f4;--pass:#08735f;--pass-bg:#e5f5ef;--review:#906000;--review-bg:#fff3d1;--fail:#b42318;--fail-bg:#fde9e6}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.45 "Segoe UI",Arial,sans-serif}}
.page{{max-width:1360px;margin:auto;background:var(--paper);min-height:100vh}}header,main,footer{{padding:24px 30px}}header{{border-bottom:1px solid var(--line)}}
h1{{margin:0 0 6px;font-size:27px}}h2{{margin:0}}h3{{margin:20px 0 8px;font-size:16px}}p{{margin:6px 0}}
.meta{{color:var(--muted)}}nav{{position:sticky;top:0;z-index:5;display:flex;gap:6px;padding:8px 30px;overflow:auto;border-bottom:1px solid var(--line);background:#fff}}
nav a{{white-space:nowrap;padding:6px 8px;color:var(--ink);text-decoration:none}}.decision{{margin-top:12px;padding:12px;border-left:5px solid var(--fail);background:var(--fail-bg)}}
.table-wrap{{overflow:auto;border:1px solid var(--line);margin:8px 0}}table{{width:100%;border-collapse:collapse;min-width:740px}}th,td{{padding:9px 10px;border-bottom:1px solid #e8ebef;text-align:left;vertical-align:top}}
th{{background:var(--soft);font-size:11px;text-transform:uppercase}}tr:last-child td{{border-bottom:0}}.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.badge{{display:inline-block;padding:3px 7px;border-radius:3px;font-size:11px;font-weight:700}}.pass{{color:var(--pass);background:var(--pass-bg)}}.review{{color:var(--review);background:var(--review-bg)}}.fail{{color:var(--fail);background:var(--fail-bg)}}.na{{color:#4e5968;background:#edf0f3}}
.dashboard{{padding-top:28px;margin-top:28px;border-top:4px solid var(--ink)}}.dashboard-head{{display:flex;justify-content:space-between;gap:16px;align-items:start}}
.screens{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}figure{{margin:0;border:1px solid var(--line)}}figure img{{display:block;width:100%;height:auto}}figcaption{{padding:6px 9px;background:var(--soft);font-size:12px}}
.missing{{min-height:230px;display:grid;place-items:center;color:var(--fail);background:var(--fail-bg)}}details.chart,details.logic{{border:1px solid var(--line);margin-top:8px}}summary{{cursor:pointer;padding:11px 12px}}
details.chart>summary{{display:flex;justify-content:space-between;gap:12px}}summary small{{display:block;color:var(--muted);font-weight:400}}.chart-body{{padding:12px;border-top:1px solid var(--line);background:#fbfcfd}}
.proof-line{{padding:8px 10px;background:var(--soft)}}.note{{color:var(--muted);font-size:12px}}.full-data{{margin-top:10px;border-top:1px solid var(--line)}}.button-link{{display:inline-block;padding:7px 9px;border:1px solid var(--line);color:var(--ink);text-decoration:none}}
code{{font-family:Consolas,monospace}}footer{{border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
@media(max-width:700px){{header,main,footer{{padding:16px}}nav{{padding-left:8px}}.screens{{grid-template-columns:1fr}}}}
</style></head><body><div class="page">
<header><small>Automated migration assurance</small><h1>{_e(run.get("workbook"))}</h1>
<p class="meta">Run {_e(run.get("run_id"))} | Environment {_e(run.get("environment"))} | Generated {_e(run.get("generated_at"))}</p>
<div class="decision"><b>Workbook decision: {_e(run["status"])}</b><p>No proof, no pass. Status is derived from visual, chart-grain data, formula, interaction and evidence-completeness gates.</p></div></header>
<nav><a href="#summary">Summary</a>{dashboard_nav}<a href="#evidence">Evidence</a></nav>
<main><section id="summary"><h2>Workbook summary</h2>
<div class="table-wrap"><table><thead><tr><th>Dashboard</th><th>Charts</th><th>Visual</th><th>Charts passed</th><th>Status</th></tr></thead><tbody>{summary_rows}</tbody></table></div></section>
{"".join(dashboard_sections)}
<section id="evidence" class="dashboard"><h2>Evidence and reproducibility</h2>
<p>Every chart contains a downloadable comparison CSV under <code>evidence/&lt;dashboard&gt;/charts/&lt;chart&gt;/comparison.csv</code>.</p>
<p><b>Tolerance policy:</b> half the displayed unit for rounded numeric exports, exact equality for keys/counts/ranks, and explicit percentage-point precision for percentages.</p>
</section></main><footer>Generated by the Tableau to Streamlit migration accelerator validation module.</footer>
</div></body></html>"""


def generate_report(spec: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    validated = validate_run(spec, output)

    summary_path = output / "validation_summary.json"
    summary_path.write_text(json.dumps(validated, indent=2, default=str), encoding="utf-8")

    report_path = output / "validation_report.html"
    report_path.write_text(render_html(validated), encoding="utf-8")

    issues = []
    for dashboard in validated["dashboards"]:
        if dashboard["status"] != PASS:
            issues.append({"level": "dashboard", "dashboard": dashboard["name"], "status": dashboard["status"]})
        for chart in dashboard["charts"]:
            if chart["status"] != PASS:
                issues.append(
                    {
                        "level": "chart",
                        "dashboard": dashboard["name"],
                        "chart": chart["title"],
                        "status": chart["status"],
                        "failed_cells": chart["failed_cells"],
                    }
                )
    with (output / "issues.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["level", "dashboard", "chart", "status", "failed_cells"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for issue in issues:
            writer.writerow({field: issue.get(field, "") for field in fields})

    return {
        "status": validated["status"],
        "report": str(report_path),
        "summary": str(summary_path),
        "issues": str(output / "issues.csv"),
    }

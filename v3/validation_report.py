from __future__ import annotations

import csv
import datetime as _datetime
import html
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


PASS = "PASS"
REVIEW = "REVIEW"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_VALIDATED = "NOT_VALIDATED"

# Bumped whenever the REPORT (this file's rendering half) changes shape --
# never the comparison engine's decisions. Shown once, in the collapsed
# methodology section, so a reviewer comparing two packs can tell whether a
# layout difference is a regenerated report or a genuinely different run.
GENERATOR_VERSION = "2.0-progressive-disclosure"

_STATUS_PRIORITY = {PASS: 0, REVIEW: 1, NOT_VALIDATED: 2, BLOCKED: 3, FAIL: 4}


def _d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value)
    if isinstance(value, str):
        # Tableau's own crosstab export formats a number with a thousands
        # comma ("163,797.1638"), which Decimal() rejects outright.
        # FOUND LIVE 2026-08-11: every Tableau-side cell in a comma-
        # formatted export silently parsed to None -- never reported
        # wrong, just never actually compared, so tableau_streamlit_diff
        # stayed blank on every row while streamlit/backend still matched
        # and the row read PASS. Strip it before parsing, the same
        # comma-stripping already applied to Tableau-sourced numbers
        # elsewhere in this project (parity.py, validation_adapter.py).
        text = text.replace(",", "").strip()

        # SAME BUG CLASS, FOUND 2026-08-13 while closing B3: the comma was
        # only one of Tableau's display formats. A currency '$' ("$391,722")
        # and a trailing '%' ("11.9%") also failed Decimal() outright, so
        # those cells were never compared either. Real, not hypothetical --
        # B4's date-part sheets (Performance, Forecast) take their measures
        # from '$'-formatted wide columns, so B4 could not make a single one
        # of those charts compare until this was fixed.
        negative = False
        # ACCOUNTING NEGATIVES ARE REAL IN THIS DATA and must be handled
        # BEFORE the symbol strip: 'Lower Prediction Interval for Sales'
        # contains "($1,137)" and "($657)". Stripping '$' without reading the
        # parentheses would yield +1137 -- a SIGN INVERSION, which is worse
        # than the original failure: a silently wrong comparison instead of
        # an honest missing one.
        if len(text) > 1 and text.startswith("(") and text.endswith(")"):
            negative = True
            text = text[1:-1].strip()
        text = text.lstrip("$").strip()

        percent = text.endswith("%")
        if percent:
            text = text[:-1].strip()
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError, TypeError):
            # Not a number after all -- e.g. the real 'Achieved Quota' value
            # "75-100%", which is a RANGE LABEL (a dimension), not a measure.
            # It must stay None rather than become 75 or 0.75.
            return None
        if percent:
            # A literal '%' in the SOURCE means the fraction, and this
            # codebase stores percent measures as fractions: value_scale is
            # "fraction" everywhere (validation_adapter.py:63 is its only
            # producer) and format_value multiplies by 100 only to DISPLAY.
            # So "11.9%" is 0.119 here, not 11.9. Getting this backwards is a
            # 100x error, which is why it is scaled rather than just stripped.
            number /= Decimal(100)
        return -number if negative else number
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _safe_id(value: str) -> str:
    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in cleaned.split("-") if part)


_ISO_TEMPORAL_KEY = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[ T](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def _canonical_key_value(value: Any) -> Any:
    """Normalize equivalent chart-grain keys before indexing evidence.

    Snowflake commonly returns a DATE as ``YYYY-MM-DD`` while pandas and
    Streamlit serialize the same value as ``YYYY-MM-DD 00:00:00``. Those
    representations are the same daily mark and must not create two rows.
    Non-midnight timestamps remain timestamps so hourly grain is preserved.
    """
    if value is None:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, _datetime.datetime):
        if value.time().replace(tzinfo=None) == _datetime.time.min:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, _datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        match = _ISO_TEMPORAL_KEY.fullmatch(text)
        if match:
            time = match.group("time")
            if time is None or time == "00:00:00":
                return match.group("date")
        return text
    if hasattr(value, "isoformat"):
        try:
            return _canonical_key_value(value.isoformat(sep=" "))
        except TypeError:
            return _canonical_key_value(value.isoformat())
    return value


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
        key = tuple(_canonical_key_value(row.get(field)) for field in grain)
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
    # LOCAL ADDITION (accelerator integration): a chart the adapter could not
    # honestly extract carries `skip_reason`. It is reported as BLOCKED with
    # its stated reason rather than dropped from the report -- silently
    # omitting it would be indistinguishable from a pass, which is exactly
    # what this module's "no proof, no pass" rule exists to prevent.
    if chart.get("skip_reason"):
        result = dict(chart)
        result.update({
            "id": chart.get("id", _safe_id(chart["title"])),
            "grain": list(chart.get("grain", [])),
            "source_counts": {"tableau": 0, "streamlit": 0, "backend": 0},
            "duplicates": {}, "null_keys": {},
            "key_set_match": False, "order_match": False,
            "comparison_rows": [], "formula_results": [], "interaction_results": [],
            "failed_cells": 0, "blocked_cells": 0,
            "max_difference": Decimal("0"),
            "evidence_complete": False, "status": BLOCKED,
        })
        return result

    grain = list(chart.get("grain", []))
    measures = list(chart.get("measures", []))
    # LOCAL FIX (found integrating this module): `evidence_complete` below
    # deliberately treats a None row-set as "this proof is missing" (a
    # file-uploaded workbook has no Tableau export), but `list(None)` raises
    # TypeError -- so the BLOCKED path this module advertises could never
    # actually execute. Coerce None to an empty set here; evidence_complete
    # still sees the original None and correctly reports BLOCKED.
    sources = {
        "tableau": _index_rows(list(chart.get("tableau_rows") or []), grain),
        "streamlit": _index_rows(list(chart.get("streamlit_rows") or []), grain),
        "backend": _index_rows(list(chart.get("backend_rows") or []), grain),
    }

    key_sets = {name: set(source.rows) for name, source in sources.items()}
    all_keys = set().union(*key_sets.values()) if key_sets else set()
    # LOCAL FIX: a source that was never supplied at all (None) makes the
    # key-set and order comparisons UNKNOWABLE, not failed. Attributing them
    # as a structural FAIL reported "this migration is wrong" when the honest
    # answer is "this was never measured" -- the two must not look the same.
    # Missing proof still cannot pass: evidence_complete below marks it BLOCKED.
    # An EMPTY source is just as unmeasured as an absent one. Found live
    # 2026-08-14 on the deployed run: `Estimated Sales` (Tableau [] , 0 rows)
    # read FAIL while `Executive Overview - Profitability` (Tableau None, also
    # 0 rows) read BLOCKED -- identical evidence, opposite verdicts, decided
    # purely by whether alignment returned [] or None. Zero reference rows
    # cannot prove a migration WRONG; they prove nothing at all.
    missing_sources = [name for name in sources
                       if chart.get(f"{name}_rows") is None
                       or not sources[name].rows]
    key_set_match = len({frozenset(value) for value in key_sets.values()}) == 1
    order_match = sources["tableau"].order == sources["streamlit"].order
    # SHARATH'S RULING (D1, 2026-08-13): row ORDER is a structural failure
    # only for charts where display order carries meaning -- ranked lists and
    # top-N (order_is_semantic in validation_adapter.py, set on this chart
    # dict by both builder paths as `order_significant`). Everywhere else
    # (grouped bars, plain tables, ...) a sequence difference is informational
    # -- Regional Analysis had two fully-evidenced charts (every individual
    # cell PASS) read chart-level FAIL purely from row order on charts where
    # order was never semantic. Default True when the field is absent (an
    # older/test chart dict that predates this fix) so nothing silently
    # loosens for a caller that hasn't been taught the distinction yet.
    order_significant = bool(chart.get("order_significant", True))
    if missing_sources:
        structural_failure = not grain or not measures
    else:
        structural_failure = (
            not grain
            or not measures
            or not key_set_match
            or (order_significant and not order_match)
            or any(source.duplicates or source.null_keys for source in sources.values())
        )

    comparison_rows: list[dict[str, Any]] = []
    failed_cells = 0
    blocked_cells = 0
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
            tolerance = derive_tolerance(measure, decimals["backend"])
            # LOCAL CHANGE: compare every pair that IS available instead of
            # blocking the whole cell when any one source is missing. A
            # file-uploaded workbook has no Tableau export, and the original
            # all-or-nothing rule made every cell BLOCKED -- hiding the
            # Streamlit-vs-backend disagreement that WAS provable. Each
            # available pair is now judged on its merits; a cell with no
            # comparable pair at all is still BLOCKED, and the chart's own
            # evidence_complete gate below still refuses to pass a workbook
            # that never supplied Tableau proof.
            _pairs = (("tableau_streamlit", "tableau", "streamlit"),
                      ("streamlit_backend", "streamlit", "backend"),
                      ("tableau_backend", "tableau", "backend"))
            pair_diffs: dict[str, Decimal | None] = {name: None for name, _a, _b in _pairs}
            for name, left, right in _pairs:
                if decimals[left] is not None and decimals[right] is not None:
                    pair_diffs[name] = abs(decimals[left] - decimals[right])
            compared = [value for value in pair_diffs.values() if value is not None]
            all_sources_present = all(value is not None for value in decimals.values())
            if compared:
                cell_max = max(compared)
                max_difference = max(max_difference, cell_max)
                if any(value > tolerance for value in compared):
                    cell_status = FAIL
                elif all_sources_present:
                    cell_status = PASS
                else:
                    # A pair may independently pass, but the three-way cell
                    # is incomplete until all three sources are present.
                    cell_status = BLOCKED
            else:
                cell_max = None
                cell_status = BLOCKED

            if cell_status == FAIL:
                failed_cells += 1
            elif cell_status == BLOCKED:
                blocked_cells += 1
            if cell_status != PASS:
                row_result["status"] = _status_of(
                    [row_result["status"], cell_status]
                )

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

    # "No proof, no pass" has to survive the empty case too: a source that
    # supplied ZERO rows has proved nothing, so the chart must not reach PASS
    # on the strength of the other two legs agreeing with each other.
    evidence_complete = bool(
        chart.get("tableau_rows") is not None
        and chart.get("streamlit_rows") is not None
        and chart.get("backend_rows") is not None
        and not missing_sources
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
            "order_significant": order_significant,
            "comparison_rows": comparison_rows,
            "formula_results": formula_results,
            "interaction_results": interaction_results,
            "failed_cells": failed_cells,
            "blocked_cells": blocked_cells,
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
        # BLOCKED visual evidence (no screenshot -- e.g. no browser available in
        # this environment) is EXCLUDED from the rollup below; a PROVEN visual
        # result (FAIL/REVIEW/PASS -- a screenshot was captured and compared)
        # is still included exactly as before. FOUND 2026-08-10: `_status_of`
        # takes the WORST status, and BLOCKED outranks PASS -- so a single
        # missing screenshot was overriding every chart/formula/interaction
        # check that had genuinely passed, making a correctly-migrated
        # dashboard read BLOCKED with no way to tell "couldn't check" from
        # "checked and it's broken". "No proof, no pass" still holds for the
        # VISUAL claim itself (it stays BLOCKED, never upgraded to PASS) --
        # this only stops its absence from capping everything ELSE that WAS
        # measured.
        visual_for_rollup = [] if visual_status == BLOCKED else [visual_status]
        dashboard_status = _status_of(
            visual_for_rollup
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
    return f'<span class="badge {css}">{_e(str(status).replace("_", " "))}</span>'


def _badge_or(status: str | None, fallback: str) -> str:
    """A verdict badge, or an explicit '<fallback>' badge (styled the same
    as BLOCKED/NOT_VALIDATED) when there is nothing to judge yet -- e.g. a
    dashboard with zero charts, or zero interaction scenarios supplied.
    NOT TESTED must never look like a pass."""
    return _badge(status) if status else _badge(fallback)


def _image_or_missing(path: str | None, label: str, note: str | None = None) -> str:
    """The image, or an explicit statement of WHY there isn't one.

    "Missing required Streamlit dashboard screenshot" is true and useless: it
    does not say whether Playwright is absent, the app failed to start, or the
    pack was generated inside the SiS sandbox where no browser exists -- three
    causes with three different fixes. The reason is computed upstream; this
    renders it instead of discarding it. A missing image is still BLOCKED and
    is never substituted with a server-side re-render."""
    if path:
        return f'<img src="{_e(path)}" alt="{_e(label)}">'
    body = f"Missing required {_e(label)}"
    if note:
        body += f'<p class="note">{_e(str(note))}</p>'
    return f'<div class="missing">{body}</div>'


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
    # LOCAL ADDITION: a chart the adapter could not extract states WHY,
    # instead of rendering an empty comparison that reads like a pass. The
    # generic "why blocked charts are listed at all" policy is stated ONCE,
    # workbook-wide, in the methodology section -- repeating it under every
    # blocked chart drowned the one thing that differs per chart: its reason.
    if chart.get("skip_reason"):
        return (
            f'<details class="chart" id="chart-{_e(chart.get("id", ""))}" open>'
            f'<summary><span><b>{_e(chart["title"])}</b><small>{_e(chart.get("chart_type", "Chart"))}'
            f'</small></span>{_badge(BLOCKED)}</summary>'
            f'<div class="chart-body"><p class="proof-line">BLOCKED: '
            f'{_e(chart["skip_reason"])}</p></div></details>'
        )

    rows = chart["comparison_rows"]
    status = chart["status"]

    if chart["order_match"]:
        order_note = "visual order matches"
    elif chart["order_significant"]:
        order_note = "visual order does not match (rank/position is significant for this chart)"
    else:
        order_note = "visual order differs (informational only -- not significant for this chart kind)"
    # WHY a leg is empty, not just THAT it is empty -- kept visible for every
    # chart regardless of PASS/FAIL, since a PASS chart with an informational
    # order mismatch still needs that context stated, not just a good badge.
    structural = (
        f'{chart["source_counts"]["tableau"]} / {chart["source_counts"]["streamlit"]} / '
        f'{chart["source_counts"]["backend"]} source rows; '
        f'key sets {"match" if chart["key_set_match"] else "do not match"}; '
        f'{order_note}.'
    )

    csv_path = chart.get("comparison_csv")
    csv_link = (f'<a class="button-link" href="{_e(csv_path)}" download>'
                f'Download comparison CSV</a>') if csv_path else ""

    if not rows:
        rows_html = '<p class="note">No comparable rows.</p>'
    elif status == PASS:
        # A PASS chart's proof is real but not the thing a reviewer needs to
        # look at first -- collapse the row-level table behind an explicit
        # count, keep the CSV one click away either way.
        rows_html = (
            f'<p class="proof-line">&#10003; All {len(rows)} chart-grain rows within '
            f'tolerance. {csv_link}</p>'
            f'<details class="full-data"><summary>View all {len(rows)} compared rows</summary>'
            f"{_comparison_table(chart, rows)}</details>"
        )
    else:
        # FAIL/BLOCKED: lead with WHY, then at most 5 representative rows,
        # full evidence still one click away, never trimmed out of the file.
        failing = [row for row in rows if row["status"] != PASS]
        sample = failing[:5] if failing else rows[:5]
        sample_table = _comparison_table(chart, sample)
        if failing:
            sample_note = (
                f"Showing {len(sample)} of {len(failing)} failed/blocked row(s)."
                if len(failing) > len(sample) else
                f"Showing all {len(sample)} failed/blocked row(s)."
            )
        else:
            sample_note = f"Showing {len(sample)} of {len(rows)} row(s)."
        rows_html = (
            f"{sample_table}<p class=\"note\">{_e(sample_note)}</p>"
            f'<details class="full-data"><summary>View all {len(rows)} compared rows</summary>'
            f'{"<p>" + csv_link + "</p>" if csv_link else ""}{_comparison_table(chart, rows)}</details>'
        )

    return (
        f'<details class="chart" id="chart-{_e(chart.get("id", ""))}" '
        f'{"open" if open_by_default else ""}>'
        f'<summary><span><b>{_e(chart["title"])}</b><small>{_e(chart.get("chart_type", "Chart"))} | '
        f'Grain: {_e(", ".join(chart["grain"]))}</small></span>{_badge(status)}</summary>'
        f'<div class="chart-body"><p class="proof-line">{_e(structural)}</p>'
        f'{_evidence_gap_html(chart)}'
        f'{_failure_summary_html(chart) if status != PASS else ""}'
        f'{rows_html}</div></details>'
    )


def _row_missing_from(row: dict[str, Any], source: str) -> bool:
    measures = row.get("measures") or []
    return bool(measures) and all(m["values"].get(source) is None for m in measures)


def _failure_summary_html(chart: dict[str, Any]) -> str:
    """A concise, upfront statement of WHY a chart is FAIL/BLOCKED, derived
    entirely from fields `compare_chart` already computed (source counts,
    key-set match, duplicates, null keys, per-row status, the chart's own
    max_difference) -- no new comparison logic, purely a presentation-layer
    summary of an existing verdict so a reviewer does not have to read every
    row to learn what went wrong."""
    rows = chart.get("comparison_rows") or []
    bullets: list[str] = []

    counts = chart.get("source_counts", {})
    present = [v for v in counts.values() if v]
    if present and len(set(present)) > 1 and all(counts.get(n) for n in ("tableau", "streamlit", "backend")):
        bullets.append(
            f"Row-count mismatch: Tableau {counts.get('tableau', 0)} / "
            f"Streamlit {counts.get('streamlit', 0)} / Backend {counts.get('backend', 0)}."
        )

    if not chart.get("key_set_match", True) and rows:
        missing_bits = [
            f"{n} row(s) missing from {name.title()}"
            for name in ("tableau", "streamlit", "backend")
            if (n := sum(1 for r in rows if _row_missing_from(r, name)))
        ]
        bullets.append(
            "Missing keys: " + "; ".join(missing_bits) + "."
            if missing_bits else "Key sets do not match across sources."
        )

    for name in ("tableau", "streamlit", "backend"):
        dups = (chart.get("duplicates") or {}).get(name) or []
        if dups:
            bullets.append(f"{len(dups)} duplicate key(s) in {name.title()}.")
        nulls = (chart.get("null_keys") or {}).get(name) or []
        if nulls:
            bullets.append(f"{len(nulls)} row(s) with a null/blank grain key in {name.title()}.")

    mismatched = sum(1 for r in rows if r.get("status") == FAIL)
    if mismatched:
        bullets.append(f"{mismatched} row(s) with mismatched values.")

    max_diff = chart.get("max_difference")
    if max_diff:
        tolerance_note = ""
        for r in rows:
            hit = next((m for m in r.get("measures", []) if m.get("max_difference") == max_diff), None)
            if hit:
                tolerance_note = f" (tolerance {hit['formatted_tolerance']})"
                break
        bullets.append(f"Maximum difference observed: {max_diff}{tolerance_note}.")

    if chart.get("order_significant") and not chart.get("order_match", True):
        bullets.append("Row order does not match, and order is significant for this chart.")

    if not chart.get("evidence_complete", True):
        bullets.append("Required evidence is incomplete for at least one source leg.")

    if not bullets:
        return ""
    items = "".join(f"<li>{_e(b)}</li>" for b in bullets)
    return f'<div class="fail-summary"><ul>{items}</ul></div>'


def _evidence_gap_html(chart: dict[str, Any]) -> str:
    """Render the stated reason a source leg is missing or unusable."""
    gaps = [
        ("Tableau", chart.get("tableau_alignment_error")),
        ("Backend", chart.get("backend_error")),
    ]
    items = [
        f"<li><b>{label} evidence unavailable:</b> {_e(str(reason))}</li>"
        for label, reason in gaps if reason
    ]
    # Scoping is a real narrowing of what was compared, so it is stated rather
    # than applied quietly -- a reader must be able to see that Tableau
    # exported more rows than the chart draws.
    if chart.get("tableau_scope_note"):
        items.append(f"<li><b>Comparison scope:</b> "
                     f"{_e(str(chart['tableau_scope_note']))}</li>")
    if not items:
        return ""
    return (
        '<div class="evidence-gap"><p class="proof-line">'
        "Why this chart could not be fully compared:</p><ul>"
        + "".join(items)
        + "</ul></div>"
    )


def _formula_key(formula: dict[str, Any]) -> str:
    return formula.get("id") or formula.get("metric") or json.dumps(formula, sort_keys=True, default=str)


def _workbook_formula_catalog(run: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per unique calculation across the WHOLE workbook, keyed the
    same way `_deduplicate_formulas` already keys within one dashboard --
    this just extends that same dedup across dashboard boundaries so a
    calculation used on five dashboards is described once, not five times.
    Purely a presentation-layer aggregation of formula_results the engine
    already computed; decides no new status."""
    catalog: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for dashboard in run.get("dashboards", []):
        for formula in dashboard.get("formulas", []):
            key = _formula_key(formula)
            entry = catalog.get(key)
            if entry is None:
                entry = dict(formula)
                entry["key"] = key
                entry["catalog_id"] = _safe_id(f"formula-{key}")
                entry["dashboards"] = []
                entry["usage"] = []
                catalog[key] = entry
                order.append(key)
            if dashboard.get("name") not in entry["dashboards"]:
                entry["dashboards"].append(dashboard.get("name"))
            for chart_title in formula.get("used_by", []):
                label = f'{dashboard.get("name")} — {chart_title}'
                if label not in entry["usage"]:
                    entry["usage"].append(label)
    return [catalog[k] for k in order]


def _dashboard_formulas_html(formulas: list[dict[str, Any]], catalog_by_key: dict[str, str]) -> str:
    """The dashboard's own compact calculation summary: a pass/review/fail
    count line plus a thin table (metric, status, link into the one
    workbook-level catalog for the full Tableau/Streamlit formula text) --
    the full text is never repeated per dashboard when the same calculation
    is used elsewhere too."""
    if not formulas:
        return ""
    counts = Counter(item["status"] for item in formulas)
    count_line = " | ".join(f"{n} {status}" for status, n in counts.most_common())
    rows = []
    for item in formulas:
        anchor = catalog_by_key.get(_formula_key(item))
        detail = f'<a href="#{_e(anchor)}">View formula &#8594;</a>' if anchor else "&mdash;"
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('metric'))}</td>"
            f"<td>{_badge(item['status'])}</td>"
            f"<td>{detail}</td></tr>"
        )
    status = _status_of(item["status"] for item in formulas)
    return (
        f'<details class="logic" {"open" if status != PASS else ""}><summary>'
        f'Calculations: {_e(count_line)}</summary>'
        '<div class="table-wrap"><table><thead><tr><th>Metric</th><th>Status</th>'
        '<th>Detail</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def _formula_catalog_html(entries: list[dict[str, Any]]) -> str:
    """The ONE place the full Tableau vs. Streamlit/SQL formula text lives,
    workbook-wide, collapsed by default. Each row also states every
    dashboard/chart that uses it, so a dashboard section can link here
    instead of repeating the formula."""
    if not entries:
        return ""
    rows = []
    for item in entries:
        rows.append(
            f'<tr id="{_e(item["catalog_id"])}">'
            f"<td>{_e(item.get('metric'))}</td>"
            f"<td>{_e(', '.join(item.get('usage') or item.get('dashboards', [])))}</td>"
            f"<td><code>{_e(item.get('tableau'))}</code></td>"
            f"<td><code>{_e(item.get('streamlit'))}</code></td>"
            f"<td>{_e(item.get('classification'))}</td>"
            f"<td>{_e(item.get('impact', 'None'))}</td>"
            f"<td>{_badge(item['status'])}</td></tr>"
        )
    status = _status_of(item["status"] for item in entries)
    return (
        f'<details id="formula-catalog" {"open" if status != PASS else ""}>'
        f'<summary>Formula catalog &mdash; {len(entries)} unique calculation(s) '
        'across the workbook</summary>'
        '<div class="table-wrap"><table><thead><tr><th>Metric</th><th>Used by</th>'
        '<th>Tableau</th><th>Streamlit / SQL</th><th>Classification</th>'
        '<th>Impact</th><th>Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></details>'
    )


def _interaction_html(interactions: list[dict[str, Any]]) -> str:
    """Renders the interaction table. Callers only invoke this when
    `interactions` is non-empty -- a dashboard with no interaction evidence
    gets a compact 'NOT TESTED' badge in its header instead of an entire
    section that exists only to say nothing was supplied."""
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


def _capture_diagnostics_html(diag: dict[str, Any] | None) -> str:
    """ONE workbook-level <details> block with the full in-Snowflake capture
    attempt history (query id, SQLSTATE, incident id, elapsed time, error per
    attempt) -- never repeated under each dashboard. Each dashboard still
    carries its own short one-line reason (already concise by construction,
    from notebook_capture._capture_with_retry's summary text); this is the
    one place the full technical detail lives, present whether the eventual
    capture succeeded after a retry or failed after all attempts, so an
    attempt-1 transient failure is never lost just because attempt 2 passed.
    Returns "" when there is nothing to show."""
    attempts = (diag or {}).get("attempts") or []
    if not attempts:
        return ""

    def _row(a: dict[str, Any]) -> str:
        elapsed = a.get("elapsed_s")
        elapsed_text = f"{elapsed:.1f}s" if elapsed is not None else "—"
        cells = [
            _e(a.get("attempt")),
            f"<code>{_e(a.get('run_id'))}</code>",
            _badge(PASS if a.get("ok") else FAIL),
            f"<code>{_e(a.get('query_id') or '—')}</code>",
            _e(a.get("sqlstate") or "—"),
            _e(a.get("incident_id") or "—"),
            _e(elapsed_text),
            _e(a.get("error") or "—"),
        ]
        return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"

    rows = "".join(_row(a) for a in attempts)
    summary = diag.get("summary") or (
        f"succeeded after {len(attempts)} attempt(s)"
        if attempts[-1].get("ok") else
        f"failed after {len(attempts)} attempt(s)")
    return (
        '<details class="capture-diagnostics" id="capture-diagnostics">'
        '<summary>Streamlit capture diagnostics</summary>'
        f'<p>{_e(summary)}</p>'
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Attempt</th><th>Run ID</th><th>Result</th><th>Query ID</th>'
        '<th>SQLSTATE</th><th>Incident</th><th>Elapsed</th><th>Error</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        '</details>'
    )


def _workbook_coverage(run: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, int]:
    """Compact workbook-wide coverage counters for the summary section.
    Pure aggregation over statuses `validate_run`/`compare_chart` already
    decided -- no new judgment is made here."""
    dashboards = run.get("dashboards", [])
    dash_status = Counter(d["status"] for d in dashboards)
    charts_total = sum(len(d.get("charts", [])) for d in dashboards)
    charts_validated = sum(
        1 for d in dashboards for c in d.get("charts", []) if c.get("evidence_complete")
    )
    formula_status = Counter(item["status"] for item in catalog)
    return {
        "dashboards_total": len(dashboards),
        "dashboards_passed": dash_status.get(PASS, 0),
        "dashboards_review": dash_status.get(REVIEW, 0),
        "dashboards_failed": dash_status.get(FAIL, 0),
        "dashboards_blocked": dash_status.get(BLOCKED, 0) + dash_status.get(NOT_VALIDATED, 0),
        "visual_completed": sum(1 for d in dashboards if d["visual_status"] != BLOCKED),
        "charts_total": charts_total,
        "charts_validated": charts_validated,
        "formulas_total": len(catalog),
        "formulas_pass": formula_status.get(PASS, 0),
        "interactions_tested": sum(1 for d in dashboards if d.get("interactions")),
        "failed_or_blocked": (
            sum(1 for d in dashboards if d["status"] in (FAIL, BLOCKED))
            + sum(1 for d in dashboards for c in d.get("charts", []) if c["status"] in (FAIL, BLOCKED))
            + sum(1 for item in catalog if item["status"] in (FAIL, BLOCKED))
            + sum(1 for d in dashboards for i in d.get("interactions", []) if i["status"] in (FAIL, BLOCKED))
        ),
    }


def _coverage_html(cov: dict[str, int]) -> str:
    def tile(value: str, label: str) -> str:
        return f'<div><b>{_e(value)}</b>{_e(label)}</div>'

    return (
        '<div class="coverage">'
        + tile(f"{cov['dashboards_passed']} / {cov['dashboards_total']}", "Dashboards passed")
        + tile(f"{cov['dashboards_failed']}", "Dashboards failed")
        + tile(f"{cov['dashboards_blocked']}", "Dashboards blocked")
        + tile(f"{cov['dashboards_review']}", "Dashboards in review")
        + tile(f"{cov['visual_completed']} / {cov['dashboards_total']}", "Visual comparisons completed")
        + tile(f"{cov['charts_validated']} / {cov['charts_total']}", "Charts validated")
        + tile(f"{cov['formulas_total']} ({cov['formulas_pass']} pass)", "Formulas validated")
        + tile(f"{cov['interactions_tested']} / {cov['dashboards_total']}", "Dashboards with interactions tested")
        + tile(f"{cov['failed_or_blocked']}", "Failed or blocked checks")
        + '</div>'
    )


def _dashboard_badges_html(dashboard: dict[str, Any]) -> str:
    """The compact Overall / Visual / Data / Formulas / Interactions badge
    row for one dashboard header. 'NOT TESTED' (not a real status the engine
    ever emits) marks a leg that was never exercised -- e.g. zero charts, or
    zero interaction scenarios supplied -- so it never reads as a pass."""
    charts = dashboard.get("charts", [])
    data_status = _status_of(c["status"] for c in charts) if charts else None
    formulas = dashboard.get("formulas", [])
    formula_status = _status_of(f["status"] for f in formulas) if formulas else None
    interactions = dashboard.get("interactions", [])
    interaction_status = _status_of(i["status"] for i in interactions) if interactions else None

    def cell(label: str, status: str | None) -> str:
        return f'<span class="dash-badge"><b>{_e(label)}</b>{_badge_or(status, "NOT TESTED")}</span>'

    # Display-only: the same opt-in metadata hook _visual_proof_html honors.
    # `dashboard["visual_status"]` itself (used for every rollup/coverage
    # count) is never touched -- only the badge TEXT shown here changes, so
    # the header and the detail section underneath it agree.
    visual_meta = dashboard.get("visual") or {}
    visual_display = ("NOT_APPLICABLE"
                       if bool(visual_meta.get("not_applicable")) or bool(dashboard.get("non_data"))
                       else dashboard["visual_status"])

    return (
        '<div class="dash-badges">'
        + cell("Overall", dashboard["status"])
        + cell("Visual", visual_display)
        + cell("Data", data_status)
        + cell("Formulas", formula_status)
        + cell("Interactions", interaction_status)
        + '</div>'
    )


def _visual_proof_html(dashboard: dict[str, Any], capture_diag_present: bool) -> str:
    images = dashboard.get("images") or {}
    visual = dashboard.get("visual") or {}
    screens = (
        '<div class="screens">'
        f'<figure>{_image_or_missing(images.get("tableau"), "Tableau dashboard screenshot", visual.get("tableau_screenshot_note"))}<figcaption>Tableau</figcaption></figure>'
        f'<figure>{_image_or_missing(images.get("streamlit"), "Streamlit dashboard screenshot", visual.get("streamlit_screenshot_note"))}<figcaption>Streamlit</figcaption></figure>'
        '</div>'
    )

    # Opt-in metadata hook only -- no producer sets this today, so every
    # dashboard with a missing screenshot correctly falls through to BLOCKED
    # below. Wired so a future producer that DOES prove "no visual content"
    # (e.g. a genuinely empty placeholder sheet) can say NOT APPLICABLE
    # without this renderer ever guessing it on its own.
    not_applicable = bool(visual.get("not_applicable")) or bool(dashboard.get("non_data"))

    missing = [label for label, key in (("Tableau", "tableau"), ("Streamlit", "streamlit"))
               if not images.get(key)]
    if not_applicable:
        line = (f'<p class="visual-line">{_badge("NOT_APPLICABLE")} '
                'Workbook metadata confirms this dashboard has no visual content to compare.</p>')
    elif missing:
        diag_link = (' <a href="#capture-diagnostics">See capture diagnostics.</a>'
                     if capture_diag_present else "")
        line = (f'<p class="visual-line">{_badge(BLOCKED)} '
                f'{_e(" and ".join(missing))} screenshot{"s" if len(missing) > 1 else ""} '
                f'unavailable.{diag_link}</p>')
    else:
        similarity = visual.get("similarity")
        threshold = visual.get("threshold", 0.95)
        sim_text = f"{float(similarity) * 100:.1f}%" if similarity is not None else "Not scored"
        thr_text = f"{float(threshold) * 100:.1f}%"
        line = (f'<p class="visual-line">Similarity {_e(sim_text)} | Threshold {_e(thr_text)} '
                f'{_badge(dashboard["visual_status"])}</p>')

    checks = visual.get("checks") or []
    checks_html = ""
    if checks:
        rows = "".join(
            f"<tr><td>{_e(c.get('name'))}</td><td>{_e(c.get('observed'))}</td>"
            f"<td>{_e(c.get('threshold'))}</td><td>{_badge(c.get('status', NOT_VALIDATED).upper())}</td></tr>"
            for c in checks
        )
        checks_html = (
            f'<details class="full-data"><summary>View {len(checks)} individual visual '
            'check(s)</summary><div class="table-wrap"><table><thead><tr>'
            '<th>Visual check</th><th>Observed</th><th>Threshold</th><th>Status</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div></details>'
        )
    return screens + line + checks_html


def _methodology_html(run: dict[str, Any]) -> str:
    return (
        '<details id="methodology"><summary>Validation methodology and evidence artifacts</summary>'
        '<div class="chart-body">'
        '<p><b>Blocked-chart policy:</b> a chart the adapter could not honestly extract is '
        'reported BLOCKED with its stated reason rather than omitted -- an omitted chart is '
        'indistinguishable from a validated one, so every blocked chart states its own precise '
        'reason instead of being dropped from the report.</p>'
        '<p><b>Tolerance policy:</b> half the displayed unit for rounded numeric exports, '
        'derived from the workbook\'s own declared number format.</p>'
        '<p><b>Key / count / rank policy:</b> grain keys, row counts and ranks are compared for '
        'exact equality -- there is no tolerance band for identity fields.</p>'
        '<p><b>Percentage-point policy:</b> percent measures are toleranced on the fraction scale '
        'internally and reported to the display decimals as a percentage-point (pp) difference.</p>'
        '<p><b>Status definitions:</b></p><ul>'
        '<li><b>PASS</b> -- validated and within policy.</li>'
        '<li><b>FAIL</b> -- comparison ran and found a mismatch.</li>'
        '<li><b>BLOCKED</b> -- required evidence or execution was unavailable.</li>'
        '<li><b>REVIEW</b> -- evidence exists but requires judgment.</li>'
        '<li><b>NOT TESTED</b> -- validation was not executed.</li>'
        '<li><b>NOT APPLICABLE</b> -- validation does not apply and is supported by metadata.</li>'
        '</ul>'
        '<p><b>Evidence layout:</b> every chart\'s downloadable comparison CSV lives at '
        '<code>evidence/&lt;dashboard&gt;/charts/&lt;chart&gt;/comparison.csv</code>; dashboard '
        'screenshots at <code>evidence/&lt;dashboard&gt;/tableau.png</code> and '
        '<code>evidence/&lt;dashboard&gt;/streamlit.png</code>; the machine-readable verdict tree '
        'is <code>validation_summary.json</code> and every non-PASS item is also listed in '
        '<code>issues.csv</code>.</p>'
        f'<p><b>Run:</b> {_e(run.get("run_id"))} &middot; Environment {_e(run.get("environment"))} '
        f'&middot; Generated {_e(run.get("generated_at"))} &middot; Generator {_e(GENERATOR_VERSION)}.</p>'
        '</div></details>'
    )


def render_html(run: dict[str, Any]) -> str:
    dashboards = run.get("dashboards", [])
    catalog = _workbook_formula_catalog(run)
    catalog_by_key = {item["key"]: item["catalog_id"] for item in catalog}
    cov = _workbook_coverage(run, catalog)

    summary_rows = "".join(
        "<tr>"
        f'<td><a href="#{_e(item["id"])}"><b>{_e(item["name"])}</b></a></td>'
        f"<td>{len(item['charts'])}</td><td>{_badge(item['visual_status'])}</td>"
        f"<td>{sum(chart['status'] == PASS for chart in item['charts'])} / {len(item['charts'])}</td>"
        f"<td>{_badge(item['status'])}</td></tr>"
        for item in dashboards
    )

    capture_diagnostics_html = _capture_diagnostics_html(run.get("capture_diagnostics"))
    capture_diag_present = bool(capture_diagnostics_html)

    dashboard_sections = []
    for dashboard_index, dashboard in enumerate(dashboards, start=1):
        charts = dashboard.get("charts", [])
        chart_index = "".join(
            "<tr>"
            f'<td><a href="#chart-{_e(chart.get("id", ""))}">{_e(chart["title"])}</a></td>'
            f"<td>{_e(', '.join(chart['grain']))}</td>"
            f"<td>{chart['source_counts']['tableau']} / {chart['source_counts']['streamlit']} / {chart['source_counts']['backend']}</td>"
            f"<td>{chart['failed_cells']}</td><td>{_badge(chart['status'])}</td></tr>"
            for chart in charts
        )
        chart_details = "".join(
            _chart_html(chart, chart_position == 1 or chart["status"] != PASS)
            for chart_position, chart in enumerate(charts, start=1)
        )
        chart_section = (
            '<h3>Chart data validation</h3>'
            '<div class="table-wrap"><table><thead><tr><th>Chart</th><th>Validation grain</th>'
            '<th>Tableau / Streamlit / Backend rows</th><th>Failed values</th><th>Status</th></tr></thead>'
            f'<tbody>{chart_index}</tbody></table></div>{chart_details}'
            if charts else
            '<h3>Chart data validation</h3><p class="note">No charts on this dashboard.</p>'
        )

        interactions = dashboard.get("interactions", [])
        interactions_section = (
            f'<h3>Filters and interactions</h3>{_interaction_html(interactions)}'
            if interactions else ""
        )
        formulas_html = _dashboard_formulas_html(dashboard.get("formulas", []), catalog_by_key)
        formulas_section = f'<h3>Calculations</h3>{formulas_html}' if formulas_html else ""

        dashboard_sections.append(
            f'<section class="dashboard" id="{_e(dashboard["id"])}">'
            f'<div class="dashboard-head"><div><small>Dashboard {dashboard_index} of {len(dashboards)}</small>'
            f'<h2>{_e(dashboard["name"])}</h2></div>{_dashboard_badges_html(dashboard)}</div>'
            f'<h3>Visual proof</h3>{_visual_proof_html(dashboard, capture_diag_present)}'
            f'{chart_section}{formulas_section}{interactions_section}'
            '</section>'
        )

    formula_catalog_html = _formula_catalog_html(catalog)
    methodology_html = _methodology_html(run)

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
.coverage{{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}}.coverage div{{flex:1 1 150px;padding:10px 12px;border:1px solid var(--line);background:var(--soft);font-size:11px;color:var(--muted)}}.coverage b{{display:block;font-size:19px;color:var(--ink);font-weight:700}}
.table-wrap{{overflow:auto;border:1px solid var(--line);margin:8px 0}}table{{width:100%;border-collapse:collapse;min-width:740px}}th,td{{padding:9px 10px;border-bottom:1px solid #e8ebef;text-align:left;vertical-align:top}}
th{{background:var(--soft);font-size:11px;text-transform:uppercase}}tr:last-child td{{border-bottom:0}}.num{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
.badge{{display:inline-block;padding:3px 7px;border-radius:3px;font-size:11px;font-weight:700}}.pass{{color:var(--pass);background:var(--pass-bg)}}.review{{color:var(--review);background:var(--review-bg)}}.fail{{color:var(--fail);background:var(--fail-bg)}}.na{{color:#4e5968;background:#edf0f3}}
.dashboard{{padding-top:28px;margin-top:28px;border-top:4px solid var(--ink)}}.dashboard-head{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:16px;align-items:start}}
.dash-badges{{display:flex;flex-wrap:wrap;gap:8px}}.dash-badge{{display:flex;flex-direction:column;gap:2px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.02em}}.dash-badge b{{font-weight:700}}
.visual-line{{padding:8px 10px;background:var(--soft);display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.fail-summary{{border-left:4px solid var(--fail);background:var(--fail-bg);padding:8px 10px;margin:8px 0}}.fail-summary ul{{margin:0;padding-left:18px}}.fail-summary li{{margin:2px 0}}
.screens{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}figure{{margin:0;border:1px solid var(--line)}}figure img{{display:block;width:100%;height:auto}}figcaption{{padding:6px 9px;background:var(--soft);font-size:12px}}
.missing{{min-height:230px;display:grid;place-items:center;color:var(--fail);background:var(--fail-bg)}}details.chart,details.logic{{border:1px solid var(--line);margin-top:8px}}summary{{cursor:pointer;padding:11px 12px}}
details.chart>summary{{display:flex;justify-content:space-between;gap:12px}}summary small{{display:block;color:var(--muted);font-weight:400}}.chart-body{{padding:12px;border-top:1px solid var(--line);background:#fbfcfd}}
.proof-line{{padding:8px 10px;background:var(--soft)}}.note{{color:var(--muted);font-size:12px}}.full-data{{margin-top:10px;border-top:1px solid var(--line)}}.button-link{{display:inline-block;padding:7px 9px;border:1px solid var(--line);color:var(--ink);text-decoration:none}}
details.capture-diagnostics{{border:1px solid var(--line);margin:14px 0;background:#fbfcfd}}details.capture-diagnostics>summary{{cursor:pointer;padding:11px 12px;font-weight:700}}details.capture-diagnostics>p{{padding:0 12px}}details.capture-diagnostics .table-wrap{{margin:8px 12px 12px}}
#methodology,#formula-catalog{{border:1px solid var(--line);margin:20px 0;background:#fbfcfd}}#methodology>summary,#formula-catalog>summary{{cursor:pointer;padding:11px 12px;font-weight:700}}
code{{font-family:Consolas,monospace}}footer{{border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
@media(max-width:700px){{header,main,footer{{padding:16px}}nav{{padding-left:8px}}.screens{{grid-template-columns:1fr}}.coverage div{{flex-basis:120px}}}}
</style></head><body><div class="page">
<header><small>Automated migration assurance</small><h1>{_e(run.get("workbook"))}</h1>
<p class="meta">Run {_e(run.get("run_id"))} | Environment {_e(run.get("environment"))} | Generated {_e(run.get("generated_at"))}</p>
<div class="decision"><b>Workbook decision: {_e(run["status"])}</b><p>Missing evidence is BLOCKED, never PASS. Overall status combines visual, chart-data, formula and interaction validation.</p></div></header>
<nav><a href="#summary">Summary</a><a href="#formula-catalog">Formulas</a><a href="#methodology">Methodology</a></nav>
<main><section id="summary"><h2>Workbook summary</h2>
{_coverage_html(cov)}
<div class="table-wrap"><table><thead><tr><th>Dashboard</th><th>Charts</th><th>Visual</th><th>Charts passed</th><th>Status</th></tr></thead><tbody>{summary_rows}</tbody></table></div>{capture_diagnostics_html}</section>
{"".join(dashboard_sections)}
<section class="dashboard">{formula_catalog_html}{methodology_html}</section>
</main><footer>Generated by the Tableau to Streamlit migration accelerator validation module.</footer>
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

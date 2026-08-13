"""Evidence bridge for the Tableau -> Streamlit validation pack.

Drop this module beside validation_adapter.py and validation_report.py.

It fixes the integration gap between evidence acquisition and the existing
proof-first report engine:

* keeps Tableau and Streamlit dashboard screenshots separate;
* supplies Tableau sheet rows from REST CSV/crosstab exports or files;
* accepts explicit chart payloads emitted by the Streamlit rendering engine;
* independently recomputes backend rows through validation_adapter.backend_rows;
* never converts absent or ambiguous evidence into PASS.

The current report engine remains authoritative for comparison, tolerance,
formula, interaction, dashboard, and workbook status.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence


Row = dict[str, Any]
Rows = list[Row]


def canonical(value: Any) -> str:
    """Stable comparison key for workbook/dashboard/sheet/field names."""
    text = "" if value is None else str(value)
    text = re.sub(r"^(sum|avg|min|max|attr|agg|cntd|countd)\s*\((.*)\)$", r"\2", text, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def display_name(obj: Mapping[str, Any]) -> str:
    return str(obj.get("title") or obj.get("caption") or obj.get("name") or "")


def records(value: Any) -> Rows:
    """Convert dataframe-like or row-like input into JSON-safe records."""
    if value is None:
        return []
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict("records")
        except TypeError:
            value = value.to_dict()
    if isinstance(value, Mapping):
        value = [value]
    out: Rows = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("chart evidence rows must be mappings or a dataframe")
        row: Row = {}
        for key, cell in item.items():
            if hasattr(cell, "item") and not isinstance(cell, (str, bytes)):
                try:
                    cell = cell.item()
                except Exception:
                    pass
            if hasattr(cell, "isoformat"):
                try:
                    cell = cell.isoformat(sep=" ")
                except TypeError:
                    cell = cell.isoformat()
            if isinstance(cell, float) and (math.isnan(cell) or math.isinf(cell)):
                cell = None
            row[str(key)] = cell
        out.append(row)
    return out


def csv_rows(text: str | bytes) -> Rows:
    """Parse a Tableau CSV/crosstab export, including UTF-8 BOM output."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    else:
        text = text.lstrip("\ufeff")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _resolve_columns(columns: Iterable[str], expected: Sequence[str]) -> tuple[dict[str, str] | None, str | None]:
    """Resolve source columns to expected captions without ambiguous guessing."""
    by_key: dict[str, list[str]] = {}
    for column in columns:
        by_key.setdefault(canonical(column), []).append(str(column))

    rename: dict[str, str] = {}
    missing: list[str] = []
    ambiguous: list[str] = []
    for wanted in expected:
        matches = by_key.get(canonical(wanted), [])
        if len(matches) == 1:
            rename[matches[0]] = wanted
        elif len(matches) > 1:
            ambiguous.append(f"{wanted}: {matches}")
        else:
            missing.append(wanted)
    if ambiguous:
        return None, "ambiguous source columns: " + "; ".join(ambiguous)
    if missing:
        return None, "missing source columns: " + ", ".join(missing)
    return rename, None


def align_rows(raw_rows: Any, expected: Sequence[str]) -> tuple[Rows | None, str | None]:
    """Rename a source rowset to the validation contract's exact captions."""
    if raw_rows is None:
        return None, "source evidence was not supplied"
    source = records(raw_rows)
    if not source:
        return [], None
    rename, reason = _resolve_columns(source[0].keys(), expected)
    if rename is None:
        return None, reason
    return [{target: row.get(source_name) for source_name, target in rename.items()} for row in source], None


def _align_chart_rows(
    raw_rows: Any,
    grain: Sequence[str],
    measures: Sequence[Mapping[str, Any]],
) -> tuple[Rows | None, str | None]:
    """Align source captions or aliases to the report's final field names."""
    if raw_rows is None:
        return None, "source evidence was not supplied"
    source = records(raw_rows)
    if not source:
        return [], None

    columns = [str(column) for column in source[0].keys()]
    targets: list[tuple[str, set[str]]] = [
        (str(field), {canonical(field)}) for field in grain
    ]
    for measure in measures:
        target = str(measure.get("field", measure["name"]))
        targets.append((target, {canonical(target), canonical(measure["name"])}))

    rename: dict[str, str] = {}
    used: set[str] = set()
    errors: list[str] = []
    for target, aliases in targets:
        matches = [column for column in columns if canonical(column) in aliases]
        if len(matches) != 1:
            label = "missing" if not matches else "ambiguous"
            errors.append(f"{label} '{target}' (matches: {matches})")
            continue
        if matches[0] in used:
            errors.append(f"source column '{matches[0]}' maps to multiple fields")
            continue
        used.add(matches[0])
        rename[matches[0]] = target
    if errors:
        return None, "; ".join(errors)
    return [
        {target: row.get(source_name) for source_name, target in rename.items()}
        for row in source
    ], None


@dataclass(frozen=True)
class ChartPayload:
    dashboard: str
    sheet: str
    chart_type: str
    grain: tuple[str, ...]
    measures: tuple[dict[str, Any], ...]
    rows: tuple[Row, ...]
    query: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    sort: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        dashboard: str,
        sheet: str,
        chart_type: str,
        grain: Sequence[str],
        measures: Sequence[str | Mapping[str, Any]],
        rows: Any,
        query: str | None = None,
        filters: Mapping[str, Any] | None = None,
        sort: Sequence[str] | None = None,
    ) -> "ChartPayload":
        definitions = []
        for measure in measures:
            if isinstance(measure, str):
                definitions.append({"name": measure, "field": measure, "kind": "number", "display_decimals": 2})
            else:
                item = dict(measure)
                item.setdefault("field", item["name"])
                item.setdefault("kind", "number")
                item.setdefault("display_decimals", 2)
                definitions.append(item)
        aligned, reason = _align_chart_rows(rows, grain, definitions)
        if aligned is None:
            raise ValueError(f"invalid Streamlit payload for {dashboard}/{sheet}: {reason}")
        return cls(
            dashboard=str(dashboard), sheet=str(sheet), chart_type=str(chart_type),
            grain=tuple(str(x) for x in grain), measures=tuple(definitions),
            rows=tuple(aligned), query=query, filters=dict(filters or {}), sort=tuple(sort or ()),
        )


class ChartEvidenceRegistry:
    """In-memory registry populated by engine chart builders during validation."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ChartPayload] = {}

    def clear(self) -> None:
        self._items.clear()

    def record(self, payload: ChartPayload) -> None:
        key = (canonical(payload.dashboard), canonical(payload.sheet))
        if key in self._items:
            raise ValueError(f"duplicate chart evidence payload: {payload.dashboard}/{payload.sheet}")
        self._items[key] = payload

    def record_chart(self, **kwargs: Any) -> ChartPayload:
        payload = ChartPayload.create(**kwargs)
        self.record(payload)
        return payload

    def get(self, dashboard: str, sheet: str) -> ChartPayload | None:
        return self._items.get((canonical(dashboard), canonical(sheet)))

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps([asdict(v) for v in self._items.values()], indent=2, default=str), encoding="utf-8")


REGISTRY = ChartEvidenceRegistry()


@dataclass
class DashboardEvidence:
    tableau_screenshot: str | None = None
    streamlit_screenshot: str | None = None
    similarity: float | None = None
    threshold: float = 0.85
    checks: list[dict[str, Any]] = field(default_factory=list)
    diff_image: str | None = None


class EvidenceBundle:
    """All external and app-emitted evidence for one validation run."""

    def __init__(self, registry: ChartEvidenceRegistry | None = None) -> None:
        self.registry = registry or REGISTRY
        self._visuals: dict[str, DashboardEvidence] = {}
        self._tableau_rows: dict[tuple[str, str], Rows] = {}

    def set_visual(self, dashboard: str, **kwargs: Any) -> None:
        self._visuals[canonical(dashboard)] = DashboardEvidence(**kwargs)

    def visual_for(self, dashboard: str) -> DashboardEvidence:
        return self._visuals.get(canonical(dashboard), DashboardEvidence())

    def set_tableau_rows(self, dashboard: str, sheet: str, rows: Any) -> None:
        self._tableau_rows[(canonical(dashboard), canonical(sheet))] = records(rows)

    def set_tableau_csv(self, dashboard: str, sheet: str, text: str | bytes) -> None:
        self.set_tableau_rows(dashboard, sheet, csv_rows(text))

    def tableau_rows_for(self, dashboard: Mapping[str, Any] | str, sheet: Mapping[str, Any] | str) -> Rows | None:
        dname = display_name(dashboard) if isinstance(dashboard, Mapping) else str(dashboard)
        sname = display_name(sheet) if isinstance(sheet, Mapping) else str(sheet)
        return self._tableau_rows.get((canonical(dname), canonical(sname)))

    @classmethod
    def from_manifest(cls, manifest_path: str | Path, registry: ChartEvidenceRegistry | None = None) -> "EvidenceBundle":
        path = Path(manifest_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        bundle = cls(registry=registry)
        for dashboard_name, dashboard in raw.get("dashboards", {}).items():
            def resolved(value: str | None) -> str | None:
                if not value:
                    return None
                candidate = Path(value)
                return str(candidate if candidate.is_absolute() else path.parent / candidate)

            visual = dashboard.get("visual", {})
            bundle.set_visual(
                dashboard_name,
                tableau_screenshot=resolved(visual.get("tableau_screenshot")),
                streamlit_screenshot=resolved(visual.get("streamlit_screenshot")),
                similarity=visual.get("similarity"), threshold=float(visual.get("threshold", 0.85)),
                checks=list(visual.get("checks", [])), diff_image=resolved(visual.get("diff_image")),
            )
            for sheet_name, chart in dashboard.get("charts", {}).items():
                if chart.get("tableau_csv"):
                    csv_path = Path(resolved(chart["tableau_csv"]))
                    bundle.set_tableau_csv(dashboard_name, sheet_name, csv_path.read_bytes())
        return bundle


def save_png_map(images: Mapping[str, bytes | bytearray | str | Path], directory: str | Path, prefix: str) -> dict[str, str]:
    """Persist screenshot bytes so validation_report can copy real files."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for name, value in images.items():
        if isinstance(value, (str, Path)):
            out[name] = str(value)
            continue
        path = directory / f"{prefix}-{canonical(name)}.png"
        path.write_bytes(bytes(value))
        out[name] = str(path)
    return out


def visual_similarity(
    tableau_path: str | Path,
    streamlit_path: str | Path,
    diff_path: str | Path | None = None,
    size: tuple[int, int] = (320, 180),
) -> tuple[float, str | None]:
    """Dependency-light structural image similarity and optional diff artifact.

    This is a triage metric, not semantic vision. A low score becomes REVIEW;
    semantic checks can still explicitly FAIL or BLOCK the visual gate.
    """
    from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

    def normalized(path: str | Path) -> Any:
        image = Image.open(path).convert("L").resize(size)
        image = ImageOps.autocontrast(image).filter(ImageFilter.FIND_EDGES)
        return image

    left = normalized(tableau_path)
    right = normalized(streamlit_path)
    delta = ImageChops.difference(left, right)
    mean = ImageStat.Stat(delta).mean[0]
    score = max(0.0, min(1.0, 1.0 - (mean / 255.0)))
    saved = None
    if diff_path:
        target = Path(diff_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        delta.save(target)
        saved = str(target)
    return score, saved


def rest_tableau_loader(
    client: Any,
    sheet_view_ids: Mapping[tuple[str, str], str] | Mapping[str, str],
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], Rows | None]:
    """Build a Tableau row callback from sheet view IDs and query_view_data_csv."""
    keyed: dict[tuple[str, str], str] = {}
    for key, value in sheet_view_ids.items():
        if isinstance(key, tuple):
            keyed[(canonical(key[0]), canonical(key[1]))] = value
        else:
            # Allows a globally unique sheet name, but tuple keys are safer.
            keyed[("", canonical(key))] = value

    def load(dashboard: Mapping[str, Any], sheet: Mapping[str, Any]) -> Rows | None:
        dkey, skey = canonical(display_name(dashboard)), canonical(display_name(sheet))
        view_id = keyed.get((dkey, skey)) or keyed.get(("", skey))
        if not view_id:
            return None
        return csv_rows(client.query_view_data_csv(view_id))

    return load


def hydrate_tableau_rows(bundle: EvidenceBundle, ir: Mapping[str, Any], loader: Callable[[Mapping[str, Any], Mapping[str, Any]], Rows | None]) -> None:
    """Fetch Tableau rows once and store them in the run evidence bundle."""
    for dashboard in ir.get("dashboards", []):
        for sheet in dashboard.get("sheets", []):
            loaded = loader(dashboard, sheet)
            if loaded is not None:
                bundle.set_tableau_rows(display_name(dashboard), display_name(sheet), loaded)


def _rebuild_from_payload(
    payload: ChartPayload,
    old_chart: MutableMapping[str, Any],
    raw_tableau: Rows | None,
    ir: Mapping[str, Any],
    sheet: Mapping[str, Any],
    table: str,
    all_metrics: Any,
) -> dict[str, Any]:
    import validation_adapter as VA

    measures = [dict(item) for item in payload.measures]
    streamlit = [dict(item) for item in payload.rows]
    tableau, tableau_reason = _align_chart_rows(raw_tableau, payload.grain, measures)
    backend_raw, backend_or_reason = VA.backend_rows(
        ir, sheet, table, list(payload.grain), measures, streamlit, all_metrics
    )
    backend, backend_alignment_reason = _align_chart_rows(
        backend_raw, payload.grain, measures
    )
    chart = {
        "id": old_chart.get("id") or canonical(payload.sheet),
        "title": old_chart.get("title") or payload.sheet,
        "chart_type": payload.chart_type,
        "grain": list(payload.grain),
        "measures": measures,
        "tableau_rows": tableau,
        "streamlit_rows": streamlit,
        "backend_rows": backend,
        # Keep the application query and the independently generated validation
        # query separate. Reusing the app query as backend proof is circular.
        "app_sql": payload.query,
        "backend_sql": backend_or_reason if backend is not None else None,
        "formulas": list(old_chart.get("formulas", [])),
        "interactions": list(old_chart.get("interactions", [])),
        "filters": dict(payload.filters),
        "sort": list(payload.sort),
    }
    if raw_tableau is not None and tableau is None:
        chart["tableau_alignment_error"] = tableau_reason
    if backend is None:
        chart["backend_error"] = str(
            backend_or_reason if backend_raw is None else backend_alignment_reason
        )
    return chart


def build_complete_validation_spec(
    ir: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
    book_name: str,
    table_for_dashboard: Callable[[Mapping[str, Any]], str],
    evidence: EvidenceBundle,
    run_id: str | None = None,
    environment: str = "UAT",
    visual_artifact_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build the existing validation_report spec with every evidence leg wired."""
    import cortex_semantic as CS
    import validation_adapter as VA

    # The old builder still supplies formula/interaction extraction and all
    # current fallbacks. We then replace its incomplete visual and chart legs.
    spec = VA.build_validation_spec(
        ir, sections, book_name, table_for_dashboard,
        screenshots=None,
        tableau_rows_for=evidence.tableau_rows_for,
        run_id=run_id,
        environment=environment,
    )
    all_metrics, _ = CS.build_metrics(ir)
    ir_dashboards = list(ir.get("dashboards", []))
    artifact_dir = Path(visual_artifact_dir) if visual_artifact_dir else None

    for index, dashboard_spec in enumerate(spec.get("dashboards", [])):
        dashboard = ir_dashboards[index]
        dashboard_name = display_name(dashboard)
        visual = evidence.visual_for(dashboard_name)
        similarity = visual.similarity
        diff_image = visual.diff_image
        if visual.tableau_screenshot and visual.streamlit_screenshot and similarity is None:
            diff_target = artifact_dir / f"{canonical(dashboard_name)}-diff.png" if artifact_dir else None
            similarity, diff_image = visual_similarity(
                visual.tableau_screenshot, visual.streamlit_screenshot, diff_target
            )
        dashboard_spec["visual"] = {
            "tableau_screenshot": visual.tableau_screenshot,
            "streamlit_screenshot": visual.streamlit_screenshot,
            "similarity": similarity,
            "threshold": visual.threshold,
            "checks": list(visual.checks),
            "diff_image": diff_image,
        }

        table = table_for_dashboard(dashboard)
        sheets = list(dashboard.get("sheets", []))
        old_charts = list(dashboard_spec.get("charts", []))
        old_by_name = {
            canonical(chart.get("title") or chart.get("id")): chart
            for chart in old_charts
        }
        rebuilt = []
        for sheet in sheets:
            sheet_name = display_name(sheet)
            old_chart = old_by_name.get(canonical(sheet_name), {})
            payload = evidence.registry.get(dashboard_name, sheet_name)
            raw_tableau = evidence.tableau_rows_for(dashboard, sheet)
            if payload is not None:
                chart = _rebuild_from_payload(
                    payload, old_chart, raw_tableau, ir, sheet, table, all_metrics
                )
            else:
                chart = dict(old_chart)
                if not chart.get("skip_reason"):
                    expected = list(chart.get("grain", [])) + [
                        m.get("field", m["name"]) for m in chart.get("measures", [])
                    ]
                    aligned, reason = align_rows(raw_tableau, expected)
                    chart["tableau_rows"] = aligned
                    if raw_tableau is not None and aligned is None:
                        chart["tableau_alignment_error"] = reason
            rebuilt.append(chart)

        # Keep formula/interaction evidence on one comparable chart exactly as
        # the original adapter intends, including payload-rebuilt charts.
        for chart in rebuilt:
            chart["formulas"] = []
            chart["interactions"] = []
        comparable = [chart for chart in rebuilt if not chart.get("skip_reason")]
        if comparable:
            section = next((item for item in sections if item.get("title") == dashboard_name), {})
            comparable[0]["formulas"] = VA.formulas_for_section(section)
            comparable[0]["interactions"] = VA.interactions_for_section(section)
        dashboard_spec["charts"] = rebuilt

    return spec


def manifest_example() -> dict[str, Any]:
    """Return a minimal manifest shape for documentation and tests."""
    return {
        "dashboards": {
            "Customer Analysis": {
                "visual": {
                    "tableau_screenshot": "tableau/customer-analysis.png",
                    "streamlit_screenshot": "streamlit/customer-analysis.png",
                    "threshold": 0.85,
                    "checks": [],
                },
                "charts": {
                    "Customer Ranking": {
                        "tableau_csv": "tableau/customer-ranking.csv"
                    }
                },
            }
        }
    }


__all__ = [
    "ChartPayload", "ChartEvidenceRegistry", "REGISTRY", "DashboardEvidence",
    "EvidenceBundle", "align_rows", "build_complete_validation_spec",
    "canonical", "csv_rows", "hydrate_tableau_rows", "manifest_example",
    "records", "rest_tableau_loader", "save_png_map", "visual_similarity",
]

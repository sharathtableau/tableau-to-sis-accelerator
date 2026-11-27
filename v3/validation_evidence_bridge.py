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
import datetime
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


def unpivot_measure_names(source: Rows, grain: Sequence[str]) -> tuple[Rows | None, str | None]:
    """Collapse Tableau's `Measure Names`/`Measure Values` crosstab to one row
    per grain value. Returns (rows, None) unchanged when the export is not that
    shape, or (None, reason) when it IS that shape but cannot be reshaped
    unambiguously -- never a guess, and never a silently-wrong grain.

    THE SHAPE, from a real REST pull (`tests/fixtures/tableau_crosstabs/`), is
    NOT the textbook long format. Tableau emits the measure columns AND the
    Measure Names/Values pair, repeating each grain row once per measure:

        Measure Names        Region  Profit Ratio  Measure Values      Sales
        Count of Customers   South   11.9%         512                 $391,722
        Sales                South   11.9%         391,721.905000001   $391,722
        Quantity             South   11.9%         6,209               $391,722

    So the old failure was NOT "missing source columns" -- alignment SUCCEEDED
    and handed the comparison 24 rows where the chart has 4, each carrying the
    DISPLAY-FORMATTED wide value ('$391,722', '11.9%') while the full-precision
    number sat unread in Measure Values ('391721.905000001', '0.119343416').

    Grouping is by the chart's OWN DECLARED grain, never by dimensions inferred
    from the CSV. That distinction is load-bearing: on the real Commission Model
    export the non-measure columns include `Rank over 3` and `Total Compensation`
    -- measures, not dimensions -- and inferring the grain from the data would
    have grouped by them. It gives the right row count there by luck, and would
    over-split the moment a measure varied independently of the grain."""
    if not source:
        return source, None
    columns = [str(column) for column in source[0].keys()]
    name_col = next((c for c in columns if canonical(c) == "measurenames"), None)
    value_col = next((c for c in columns if canonical(c) == "measurevalues"), None)
    if name_col is None or value_col is None:
        return source, None

    grain_cols: list[str] = []
    for wanted in grain:
        matches = [c for c in columns if canonical(c) == canonical(wanted)]
        if len(matches) != 1:
            label = "is missing from" if not matches else "is ambiguous in"
            return None, (f"pivoted Tableau export (Measure Names/Measure Values) "
                          f"cannot be reshaped: grain field '{wanted}' {label} the "
                          f"export (matches: {matches})")
        grain_cols.append(matches[0])

    groups: "dict[tuple, Rows]" = {}
    for row in source:
        groups.setdefault(tuple(row.get(c) for c in grain_cols), []).append(row)

    out: Rows = []
    for key, members in groups.items():
        seen = [m.get(name_col) for m in members]
        if len(set(seen)) != len(seen):
            return None, ("pivoted Tableau export cannot be reshaped: a measure "
                          "repeats within one grain group, so the chart's declared "
                          f"grain {list(grain)} does not identify a row")
        row: Row = dict(zip(grain_cols, key))
        # Carry any column that is genuinely constant across the group. One that
        # varies cannot be represented at this grain, so it is left out and the
        # caller's own resolver reports it missing rather than picking a value.
        for column in columns:
            if column in (name_col, value_col) or column in grain_cols:
                continue
            values = {m.get(column) for m in members}
            if len(values) == 1:
                row[column] = members[0].get(column)
        # Measure Values wins over a same-named wide column: same number, but
        # full precision instead of Tableau's display formatting.
        for member in members:
            row[str(member.get(name_col))] = member.get(value_col)
        out.append(row)
    return out, None


def declared_date_parts(sheet: Mapping[str, Any]) -> dict[str, str]:
    """{caption: Tableau's header word} for grain fields the sheet groups by a
    DATE PART -- e.g. {"Order Date": "Month"} for a monthly chart, which is how
    that column is headed in Tableau's crosstab ("Month of Order Date").

    Two scoping rules, both load-bearing and both learned from the real IR:

    1. GRAIN CHANNELS ONLY -- the same ("x", "y", "color", "detail") list
       validation_adapter._grain_expr uses, reused rather than re-derived.
       Superstore's `applied_filters` ALSO carry dateparts (ShipSummary filters
       Order Date by 'qr' and 'yr'), and a filter's date part is not the
       chart's displayed grain. Aliasing off a filter would point a monthly
       chart at a quarterly column.
    2. The part token -> unit mapping is validation_adapter._TRUNC_PART, which
       is itself engine.py's mapping. A third copy here would be a third thing
       to drift.
    """
    import validation_adapter as VA

    parts: dict[str, str] = {}
    for channel in ("x", "y", "color", "detail"):
        cfg = sheet.get(channel)
        if not isinstance(cfg, Mapping):
            continue
        caption = cfg.get("caption")
        if not caption:
            continue
        unit = VA._TRUNC_PART.get(str(cfg.get("datepart") or "").lower())
        if unit:
            parts[str(caption)] = unit.title()
    return parts


_MONTH_NUMBERS: dict[str, int] = {}
for _i, _full in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1):
    _MONTH_NUMBERS[_full.lower()] = _i
    _MONTH_NUMBERS[_full[:3].lower()] = _i


def _month_number(token: str) -> int | None:
    token = token.strip().lower().rstrip(".")
    if token in _MONTH_NUMBERS:
        return _MONTH_NUMBERS[token]
    if token.isdigit() and 1 <= int(token) <= 12:
        return int(token)
    return None


def reconcile_date_part_values(
    source: Rows, grain: Sequence[str], date_parts: Mapping[str, str] | None
) -> tuple[Rows | None, str | None]:
    """B4b. Rewrite a date-part column's DISPLAYED Tableau values into the
    same shape the app's own chart data carries, so the two legs' row keys can
    actually match. Returns (None, reason) rather than guessing.

    B4a made the COLUMN align; the VALUES still could not be compared, because
    the two sides describe the same grain differently:

        Tableau  "Month of Order Date" = 'January'      (+ 'Year of Order Date')
        app      DATE_TRUNC('MONTH', ..)  = 2023-03-01  (engine.py:2078)

    THREE REAL SHAPES, all three present in the committed fixtures, and the
    difference between them is exactly why this is not a one-line map:

      1. `mn`  (discrete part)  -- 'January', with the year in a SEPARATE
         'Year of Order Date' column. Tableau's discrete part collapses every
         January across years, so the year column is REQUIRED to rebuild the
         app's truncated date; without it the value is genuinely ambiguous.
      2. `tmn` (truncated)      -- 'April 2021'. The year is INSIDE the same
         column and there is no year column at all.
      3. Neither -- 'January' with no year column anywhere, and rows valued
         'All' (a Tableau grand-total row, at no grain at all). Not
         reconcilable; refused with a stated reason.

    Values are emitted through records(), the project's OWN normalizer that
    the Streamlit leg already passes through, so both sides are shaped by one
    code path instead of two that could drift.
    """
    if not source or not date_parts:
        return source, None

    columns = [str(column) for column in source[0].keys()]
    out = [dict(row) for row in source]

    for field_name in grain:
        part = (date_parts or {}).get(str(field_name))
        if not part:
            continue
        part_col = next(
            (c for c in columns if canonical(c) == canonical(f"{part} of {field_name}")),
            None)
        if part_col is None:
            # The export used the plain field name; its values are already
            # whatever Tableau renders for the underlying field, so there is
            # no part encoding to undo here.
            continue
        if part == "Year":
            # The app renders a yearly axis with EXTRACT(YEAR) -- a number,
            # not a date (engine.py:2077) -- so the year value maps straight
            # across and needs no rebuilding.
            converted = []
            for row in out:
                token = str(row.get(part_col, "")).strip()
                if not token.isdigit():
                    return None, (f"date-part column '{part_col}' holds "
                                  f"{token!r}, which is not a year")
                converted.append(int(token))
            for row, value in zip(out, converted):
                row[part_col] = value
            continue
        if part not in {"Month", "Quarter", "Day"}:
            return None, (f"date part '{part}' on '{field_name}' has no proven "
                          "value mapping (only Year/Quarter/Month/Day are "
                          "reconciled); refusing rather than guessing")

        year_col = next(
            (c for c in columns if canonical(c) == canonical(f"Year of {field_name}")),
            None)
        converted = []
        for row in out:
            token = str(row.get(part_col, "")).strip()
            if not token:
                return None, f"date-part column '{part_col}' has an empty value"
            pieces = token.split()
            year: int | None = None
            # Shape 2: the year travels inside the part column ('April 2021').
            if len(pieces) == 2 and pieces[-1].isdigit() and len(pieces[-1]) == 4:
                year = int(pieces[-1])
                token = pieces[0]
            elif year_col is not None:
                raw_year = str(row.get(year_col, "")).strip()
                if not raw_year.isdigit():
                    return None, (f"'{year_col}' holds {raw_year!r}, which is "
                                  f"not a year, so '{part_col}' cannot be dated")
                year = int(raw_year)
            if year is None:
                # Shape 3. 'All' is Tableau's grand-total row and belongs to no
                # grain at all, so name it explicitly -- it is the difference
                # between "we need a year column" and "this row is not a row".
                extra = (" (value 'All' is a Tableau grand-total row, not a "
                         "grain value)" if token.lower() == "all" else "")
                return None, (f"date-part column '{part_col}' is {part.lower()}-only "
                              f"({token!r}) and the export carries no "
                              f"'Year of {field_name}' column, so it cannot be "
                              f"resolved to the chart's date grain{extra}")
            if part == "Month":
                month = _month_number(token)
                if month is None:
                    return None, f"unrecognized month value {token!r} in '{part_col}'"
                converted.append(datetime.datetime(year, month, 1))
            elif part == "Quarter":
                digits = token.lstrip("Qq")
                if not digits.isdigit() or not 1 <= int(digits) <= 4:
                    return None, f"unrecognized quarter value {token!r} in '{part_col}'"
                converted.append(datetime.datetime(year, 3 * (int(digits) - 1) + 1, 1))
            else:  # Day
                parsed = None
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        parsed = datetime.datetime.strptime(token, fmt)
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return None, f"unrecognized day value {token!r} in '{part_col}'"
                converted.append(parsed)
        # records() is the SAME normalizer the Streamlit leg goes through, so
        # both legs' keys are produced by one path, not two.
        normalized = records([{part_col: value} for value in converted])
        for row, item in zip(out, normalized):
            row[part_col] = item[part_col]
    return out, None


def _align_chart_rows(
    raw_rows: Any,
    grain: Sequence[str],
    measures: Sequence[Mapping[str, Any]],
    date_parts: Mapping[str, str] | None = None,
    displayed_rows: Any = None,
) -> tuple[Rows | None, str | None]:
    """Align source captions or aliases to the report's final field names."""
    if raw_rows is None:
        return None, "source evidence was not supplied"
    source = records(raw_rows)
    if not source:
        return [], None

    def aliases_for_grain(field_name: str) -> set[str]:
        aliases = {canonical(field_name)}
        part = (date_parts or {}).get(str(field_name))
        if part:
            aliases.add(canonical(f"{part} of {field_name}"))
        return aliases

    def aliases_for_measure(measure: Mapping[str, Any]) -> set[str]:
        target = str(measure.get("field", measure["name"]))
        aliases = {canonical(target), canonical(measure["name"])}
        source_caption = measure.get("source_caption")
        if source_caption:
            aliases.add(canonical(source_caption))
            aggregation = canonical(measure.get("aggregation"))
            if aggregation in {"ctd", "cntd", "countd", "distinctcount"}:
                aliases.update({
                    canonical(f"Distinct count of {source_caption}"),
                    canonical(f"Count distinct of {source_caption}"),
                    canonical(f"COUNTD({source_caption})"),
                    canonical(f"CNTD({source_caption})"),
                })
        return aliases

    # Tableau's UI crosstab exporter has three legitimate header shapes:
    # complete headers, a blank measure header (common for a parameter-driven
    # axis), and no header row at all. csv.DictReader necessarily consumes the
    # first data row as field names in the third shape. Recover it only when
    # the evidence is deterministic: the column count exactly matches the
    # chart contract, no source heading matches that contract, and at least
    # one consumed heading is numeric. This avoids inventing mappings for an
    # unfamiliar but genuinely headed export.
    columns = [str(column) for column in source[0].keys()]
    expected = [(str(field_name), aliases_for_grain(str(field_name)))
                for field_name in grain]
    expected.extend((str(measure.get("field", measure["name"])),
                     aliases_for_measure(measure)) for measure in measures)
    matched_columns = {
        column for column in columns
        if any(canonical(column) in aliases for _target, aliases in expected)
    }

    def numeric_heading(value: str) -> bool:
        token = value.strip().replace(",", "").replace("$", "")
        token = token.rstrip("%").strip()
        if token.startswith("(") and token.endswith(")"):
            token = "-" + token[1:-1]
        try:
            float(token)
            return True
        except ValueError:
            return False

    if (len(columns) == len(expected) and not matched_columns
            and any(numeric_heading(column) for column in columns)):
        first_row = {target: column for column, (target, _aliases)
                     in zip(columns, expected)}
        remapped = [first_row]
        for row in source:
            remapped.append({target: row.get(column)
                             for column, (target, _aliases)
                             in zip(columns, expected)})
        source = remapped
    else:
        # A real header can still contain one unnamed value column, e.g.
        # `Month of Order Date,` for a parameter-selected measure. Fill blank
        # headings only when the remaining contract has one exact answer.
        blank_columns = [column for column in columns if not canonical(column)]
        unmatched = [(target, aliases) for target, aliases in expected
                     if not any(canonical(column) in aliases for column in columns)]
        if len(blank_columns) == 1 and len(unmatched) == 1:
            blank = blank_columns[0]
            target = unmatched[0][0]
            source = [{(target if key == blank else key): value
                       for key, value in row.items()} for row in source]

    # Some Tableau KPI sheets export one aggregate row with measure columns
    # but no physical dimension. The adapter uses a constant synthetic grain
    # (`KPI tile`) so all three evidence legs can share a comparison contract.
    # Copy that constant only when the rendered chart proves exactly one value.
    shown = records(displayed_rows)
    if shown:
        for field_name in grain:
            if any(canonical(column) in aliases_for_grain(str(field_name))
                   for column in source[0]):
                continue
            values = {row.get(str(field_name)) for row in shown
                      if row.get(str(field_name)) is not None}
            if len(values) == 1:
                value = next(iter(values))
                for row in source:
                    row[str(field_name)] = value

    source, unpivot_reason = unpivot_measure_names(source, grain)
    if source is None:
        return None, unpivot_reason

    # B4b: rebuild date-part VALUES into the app's own shape before the rename,
    # while the sibling 'Year of <field>' column is still present -- the rename
    # drops every column that is not a grain field or a measure.
    source, date_value_reason = reconcile_date_part_values(source, grain, date_parts)
    if source is None:
        return None, date_value_reason

    columns = [str(column) for column in source[0].keys()]
    targets: list[tuple[str, set[str]]] = []
    for field_name in grain:
        # B4: a date-part-grouped Tableau column is headed "Month of Order
        # Date" while the chart's own grain is "Order Date", so the two never
        # matched and the column was never compared. Alias ONLY the part the
        # sheet actually declares -- a real export carries several parts of the
        # SAME field side by side (Performance has both "Month of Order Date"
        # and "Year of Order Date"), so aliasing every part would match two
        # columns and be reported ambiguous, which is no better than missing.
        targets.append((str(field_name), aliases_for_grain(str(field_name))))
    for measure in measures:
        target = str(measure.get("field", measure["name"]))
        targets.append((target, aliases_for_measure(measure)))

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
    # WHY an image is absent. Without this the report can only say "Missing
    # required Streamlit dashboard screenshot" -- true, and useless: it does
    # not distinguish "Playwright is not installed" from "the app failed to
    # start" from "this ran inside SiS, which has no browser". The reason is
    # computed by app_screenshot.available()/capture_app and was previously
    # reaching the accelerator UI only, never the report a reviewer reads.
    tableau_screenshot_note: str | None = None
    streamlit_screenshot_note: str | None = None


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


def scope_to_displayed_keys(
    tableau: Rows | None, streamlit: Rows, grain: Sequence[str]
) -> tuple[Rows | None, str | None]:
    """Narrow the Tableau leg to the keys the chart actually displays, exactly
    as VA.backend_rows already narrows the backend leg. Returns the rows plus a
    stated note when anything was excluded -- never a silent narrowing.

    Tableau's crosstab export returns a view's UNDERLYING data, not its drawn
    marks, so a top-N or detail table exports far more rows than it displays.

    FOUND LIVE 2026-08-14, as a regression this project introduced: once B3
    taught the aligner to reshape a pivoted export, Product Detail Sheet began
    yielding 5111 real Tableau rows, which were compared against the 30 rows
    the app displays -- 20,324 "failed values" and a chart-level FAIL on a
    chart that is very likely correct. Before B3 the same chart was honestly
    BLOCKED. Converting a BLOCKED into a false FAIL is the worst outcome this
    codebase can produce, hence scoping.

    Keys the chart DOES display but Tableau lacks are deliberately left in
    play: those are real missing-key failures and must stay visible."""
    if not tableau or not streamlit:
        return tableau, None
    displayed = {tuple(row.get(field) for field in grain) for row in streamlit}
    scoped = [row for row in tableau
              if tuple(row.get(field) for field in grain) in displayed]
    surplus = len(tableau) - len(scoped)
    if not surplus or not scoped:
        # Nothing excluded, or excluding would empty the leg entirely -- the
        # latter means the two sides share no keys at all, which is a real
        # finding and must not be hidden behind an empty scoped set.
        return tableau, None
    note = (f"Tableau's export carried {len(tableau)} rows; compared at the "
            f"{len(displayed)} key(s) this chart actually displays. {surplus} "
            "exported row(s) are not drawn by the chart -- Tableau exports a "
            "view's underlying data, not its displayed marks. Keys the chart "
            "displays but Tableau lacks are still reported as failures.")
    return scoped, note


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
    # B4: only the TABLEAU leg needs date-part aliasing. Tableau heads a
    # date-part column "Month of Order Date"; the backend leg is built by our
    # own SQL against the chart's grain, so it already carries the plain name.
    date_parts = declared_date_parts(sheet)
    tableau, tableau_reason = _align_chart_rows(
        raw_tableau, payload.grain, measures, date_parts, streamlit
    )
    # Scope the TABLEAU leg to the keys the chart actually displays, exactly as
    # VA.backend_rows already scopes the backend leg. Tableau's crosstab export
    # returns the view's UNDERLYING data, not its displayed marks, so a top-N
    # or detail table exports far more rows than it draws.
    #
    # FOUND LIVE 2026-08-14, and it was a regression I introduced: once B3
    # taught the aligner to reshape a pivoted export, Product Detail Sheet
    # started yielding 5111 real Tableau rows, which were then compared against
    # the 30 rows the app displays -- 20,324 "failed values" and a chart-level
    # FAIL on a chart that is very likely correct. Before B3 the same chart was
    # honestly BLOCKED. Turning a BLOCKED into a false FAIL is the worst
    # outcome available here, so the comparison is scoped and the surplus is
    # REPORTED rather than dropped.
    tableau, tableau_scope_note = scope_to_displayed_keys(
        tableau, streamlit, payload.grain)
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
        "order_significant": VA.order_is_semantic(sheet, payload.chart_type),
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
    if tableau_scope_note:
        chart["tableau_scope_note"] = tableau_scope_note
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
            # Carried through so the REPORT can state why an image is absent.
            # These reached the accelerator UI only; the report a reviewer
            # actually reads showed a bare "Missing required ...".
            "tableau_screenshot_note": visual.tableau_screenshot_note,
            "streamlit_screenshot_note": visual.streamlit_screenshot_note,
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
                    # SAME unpivot + date-part reconciliation the explicit-
                    # payload branch above uses -- not the older align_rows(),
                    # which only renames columns 1:1 and has no idea "Order
                    # Date" and "Month of Order Date" are the same field.
                    #
                    # FOUND LIVE 2026-08-17: this branch is where every
                    # ORDINARY Altair chart lands -- record_chart() in
                    # engine.py only fires for map/treemap/table/rank-table,
                    # so a plain bar or line chart never gets a ChartPayload
                    # and payload is always None here. Every date-part-grouped
                    # chart of that kind (which is most of them -- bar and
                    # line charts grouped by month are the common case, not
                    # the exception) was failing align_rows() with "missing
                    # source columns: Order Date" and reporting 0 Tableau rows
                    # -- not because no data existed, but because this branch
                    # was never taught the column-header aliasing B4 already
                    # built and gated for the OTHER branch. Confirmed against
                    # the real account: Performance and Forecast (both here,
                    # both real charts with real Tableau data) went from 0 to
                    # 428 and 180 real rows once routed through the same
                    # function the explicit-payload path already used.
                    grain = list(chart.get("grain", []))
                    measures = list(chart.get("measures", []))
                    date_parts = declared_date_parts(sheet)
                    aligned, reason = _align_chart_rows(
                        raw_tableau, grain, measures, date_parts,
                        chart.get("streamlit_rows") or [])
                    if aligned and grain:
                        streamlit_rows = chart.get("streamlit_rows") or []
                        aligned, scope_note = scope_to_displayed_keys(
                            aligned, streamlit_rows, grain)
                        if scope_note:
                            chart["tableau_scope_note"] = scope_note
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
            interactions = VA.interactions_for_section(section)
            if not interactions:
                # Do not let declared filters/parameters disappear merely
                # because the live interaction harness produced no row. They
                # remain explicit NOT_VALIDATED coverage gaps until a real
                # before/after proof is captured.
                declared = []
                for item in dashboard.get("filters", []) or []:
                    name = item.get("caption") or item.get("field") or item.get("name")
                    if name:
                        declared.append(f"Filter: {name}")
                dashboard_text = str(dashboard)
                for name in (ir.get("params") or {}):
                    if name in dashboard_text:
                        declared.append(f"Parameter: {name}")
                interactions = [{
                    "name": name,
                    "tableau": "Declared in the Tableau workbook",
                    "streamlit": "Generated control present; behavioral proof not captured",
                    "proof": "Run a before/after selection test and compare affected chart rows.",
                    "status": "NOT_VALIDATED",
                } for name in dict.fromkeys(declared)]
            comparable[0]["interactions"] = interactions
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
    "unpivot_measure_names",
    "records", "rest_tableau_loader", "save_png_map", "visual_similarity",
]

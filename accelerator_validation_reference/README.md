# Tableau to Streamlit Validation Reference

This is a dependency-free Python reference implementation for integrating proof-first validation into a migration accelerator.

## What it does

- Organizes the report by workbook, dashboard and chart.
- Compares Tableau, Streamlit and backend rows at each chart's displayed grain.
- Detects missing keys, extra keys, duplicates, null keys and visual-order changes.
- Derives tolerance from numeric precision instead of applying a global threshold.
- Exports a complete comparison CSV for every chart.
- Shows every row for charts with 20 rows or fewer.
- Shows preview/failed rows plus a full expandable table and CSV for longer charts.
- Deduplicates formulas at dashboard level.
- Derives chart, dashboard and workbook status. Missing proof cannot pass.
- Generates:
  - `validation_report.html`
  - `validation_summary.json`
  - `issues.csv`
  - `evidence/<dashboard>/charts/<chart>/comparison.csv`

## Run the example

```powershell
cd accelerator_validation_reference
python example_run.py
```

Open:

```text
example_output/validation_report.html
```

No third-party Python packages are required.

## Integrate it

Import the generator:

```python
from validation_report import generate_report

result = generate_report(validation_spec, "reports/my_workbook_validation")
print(result["status"])
print(result["report"])
```

Build one chart specification from the three chart-grain datasets:

```python
chart = {
    "id": "sales-by-region",
    "title": "Sales by Region",
    "chart_type": "Bar chart",

    # These fields define the marks visible in the chart.
    "grain": ["Region"],

    # Supply raw values. Formatting is applied only in the report.
    "measures": [
        {
            "name": "Sales",
            "field": "Sales",
            "kind": "currency",
            "display_decimals": 0,
        }
    ],

    # Apply identical filters and aggregation before supplying these rows.
    "tableau_rows": tableau_export_rows,
    "streamlit_rows": streamlit_dataframe.to_dict("records"),
    "backend_rows": backend_dataframe.to_dict("records"),

    "formulas": [
        {
            "id": "sales",
            "metric": "Sales",
            "tableau": "SUM([Sales])",
            "streamlit": "SUM(SALES)",
            "classification": "EXACT",
            "impact": "None",
        }
    ],

    "interactions": [
        {
            "name": "Region = West",
            "tableau": "One West bar",
            "streamlit": "One West bar",
            "proof": "Filtered comparison CSV and screenshots",
            "status": "PASS",
        }
    ],
}
```

Then put charts inside dashboards:

```python
validation_spec = {
    "workbook": "Superstore.twb",
    "run_id": "VAL-2026-001",
    "environment": "UAT",
    "generated_at": "2026-08-05T10:30:00+05:30",
    "dashboards": [
        {
            "id": "customer-analysis",
            "name": "Customer Analysis",
            "visual": {
                "tableau_screenshot": "captures/tableau/customer-analysis.png",
                "streamlit_screenshot": "captures/streamlit/customer-analysis.png",
                "diff_image": "captures/diffs/customer-analysis.png",
                "similarity": 0.981,
                "threshold": 0.95,
                "checks": [
                    {
                        "name": "Marks",
                        "observed": "4 bars in both",
                        "threshold": "Exact",
                        "status": "PASS",
                    }
                ],
            },
            "charts": [chart],
        }
    ],
}
```

## Tolerance rules

The validator derives absolute tolerance from the measure definition:

| Measure | Configuration | Derived tolerance |
|---|---|---:|
| Whole-dollar export | `kind="currency", display_decimals=0` | +/-$0.50 |
| Currency with cents | `kind="currency", display_decimals=2` | +/-$0.005 |
| Count/rank/key | `kind="count"` / `"rank"` / `"key"` | Exact |
| Percentage displayed as 12.5% | `kind="percent", display_decimals=1, value_scale="fraction"` | +/-0.05 percentage points |
| Explicit business tolerance | `absolute_tolerance=...` | Supplied value |

A relative tolerance may be added with `relative_tolerance`. The effective tolerance is the larger of the precision-derived absolute tolerance and the relative tolerance.

Do not infer `display_decimals` from a formatted screenshot. Capture it from the Tableau workbook metadata or the exported view specification.

## Required adapters in your accelerator

The reference implementation deliberately does not couple itself to one Tableau or Streamlit extraction method. Your accelerator should provide these adapters:

1. **Tableau adapter**
   - Resolve dashboard, worksheet, dimensions, measures, filters, sort and aggregation from TWB.
   - Export worksheet data at the displayed grain.
   - Capture dashboard screenshots under a deterministic viewport and filter state.

2. **Streamlit adapter**
   - Capture the exact dataframe passed to each chart.
   - Capture the rendered dashboard under the same filter state and viewport.
   - Record the SQL or Python calculation used for each displayed measure.

3. **Backend adapter**
   - Generate canonical SQL using the chart dimensions, filters and measure aggregation.
   - Return one row per chart key.
   - Use a fixed data snapshot shared by all three sources.

4. **Validation orchestrator**
   - Normalize field names and data types.
   - Call `generate_report`.
   - Fail the migration release gate when the returned status is `FAIL` or `BLOCKED`.

## Formula classifications

Use semantic classifications instead of a Boolean text match:

- `EXACT`
- `SEMANTICALLY_EQUIVALENT`
- `EQUIVALENT_AT_CURRENT_GRAIN`
- `INTENTIONAL_DIFFERENCE`
- `MISMATCH`
- `NOT_VALIDATED`

`EQUIVALENT_AT_CURRENT_GRAIN` and unapproved `INTENTIONAL_DIFFERENCE` produce `REVIEW`. `MISMATCH` produces `FAIL`. `NOT_VALIDATED` produces `BLOCKED`.


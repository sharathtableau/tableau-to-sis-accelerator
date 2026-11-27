from datetime import date, datetime

import validation_report as report
import validation_report_dashboard as dashboard_report
import deep_validation


def _chart(**overrides):
    chart = {
        "title": "Trend",
        "grain": ["Order Date"],
        "measures": [{
            "name": "Sales",
            "field": "Sales",
            "kind": "currency",
            "display_decimals": 2,
        }],
        "tableau_rows": [{"Order Date": "2021-01-01", "Sales": 10}],
        "streamlit_rows": [{"Order Date": "2021-01-01 00:00:00", "Sales": 10}],
        "backend_rows": [{"Order Date": date(2021, 1, 1), "Sales": 10}],
        "formulas": [],
        "interactions": [],
        "order_significant": False,
    }
    chart.update(overrides)
    return chart


def test_equivalent_date_representations_share_one_key():
    result = report.compare_chart(_chart())

    assert result["status"] == report.PASS
    assert result["key_set_match"] is True
    assert len(result["comparison_rows"]) == 1
    assert result["failed_cells"] == 0
    assert result["blocked_cells"] == 0


def test_non_midnight_timestamp_is_not_collapsed_to_date():
    indexed = report._index_rows(
        [{"When": datetime(2021, 1, 1, 12, 30)}], ["When"]
    )

    assert indexed.order == [("2021-01-01 12:30:00",)]


def test_missing_tableau_is_blocked_not_a_numeric_mismatch():
    result = report.compare_chart(_chart(tableau_rows=None))

    assert result["status"] == report.BLOCKED
    assert result["failed_cells"] == 0
    assert result["blocked_cells"] == 1
    assert result["comparison_rows"][0]["status"] == report.BLOCKED
    assert result["comparison_rows"][0]["measures"][0]["status"] == report.BLOCKED


def test_client_chart_html_has_clean_diagnostics_and_no_broken_csv_link():
    result = report.compare_chart(_chart(tableau_rows=None))
    result.update({
        "id": "trend",
        "chart_type": "line",
        "comparison_csv": "evidence/trend/comparison.csv",
        "tableau_alignment_error": "missing 'Order Date' (matches: [])",
    })

    html = dashboard_report._chart_record(result)

    assert "numeric mismatches" in html
    assert "values not fully verified" in html
    assert "matches: []" not in html
    assert "comparison.csv" not in html
    assert ">BLOCKED<" in html


def test_tableau_view_lookup_registers_canonical_name_and_content_url(monkeypatch):
    monkeypatch.setattr(
        deep_validation.TS,
        "pull_all_view_csvs",
        lambda *_args, **_kwargs: [{
            "view": "Sales-KPI Parameter Dashboard",
            "csv": "Category,Sales\nFurniture,10\n",
            "error": None,
            "content_url": "views/SalesKPI/Dashboard",
        }],
    )

    csvs, urls = deep_validation._pull_tableau_sheet_csvs({
        "server_url": "https://tableau.example",
        "site_content_url": "site",
        "workbook_id": "workbook",
    })

    assert csvs["saleskpiparameterdashboard"].startswith("Category,Sales")
    assert urls["saleskpiparameterdashboard"] == "views/SalesKPI/Dashboard"

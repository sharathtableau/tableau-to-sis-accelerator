from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from validation_report import generate_report


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "example_assets"
OUTPUT = ROOT / "example_output"


def write_dashboard_svg(path: Path, title: str, color: str, missing_chart: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    missing = (
        '<rect x="48" y="250" width="410" height="150" fill="#fde9e6" stroke="#b42318"/>'
        '<text x="253" y="330" text-anchor="middle" fill="#b42318">Chart unavailable</text>'
        if missing_chart
        else (
            f'<rect x="48" y="250" width="85" height="105" fill="{color}"/>'
            f'<rect x="145" y="210" width="85" height="145" fill="{color}"/>'
            f'<rect x="242" y="230" width="85" height="125" fill="{color}"/>'
            f'<rect x="339" y="185" width="85" height="170" fill="{color}"/>'
        )
    )
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540">
<rect width="960" height="540" fill="#f2f4f7"/>
<text x="36" y="38" font-family="Segoe UI" font-size="22" font-weight="700">{title}</text>
<rect x="36" y="58" width="888" height="42" fill="#ffffff" stroke="#d8dde5"/>
<rect x="36" y="118" width="205" height="90" fill="#ffffff" stroke="#d8dde5"/>
<rect x="263" y="118" width="205" height="90" fill="#ffffff" stroke="#d8dde5"/>
<rect x="490" y="118" width="205" height="90" fill="#ffffff" stroke="#d8dde5"/>
<rect x="717" y="118" width="207" height="90" fill="#ffffff" stroke="#d8dde5"/>
<text x="54" y="155" font-family="Segoe UI" font-size="16">$2.30M Sales</text>
<text x="281" y="155" font-family="Segoe UI" font-size="16">$286K Profit</text>
<text x="508" y="155" font-family="Segoe UI" font-size="16">12.5% Ratio</text>
<text x="735" y="155" font-family="Segoe UI" font-size="16">793 Customers</text>
<rect x="36" y="224" width="432" height="210" fill="#ffffff" stroke="#d8dde5"/>
<rect x="490" y="224" width="434" height="210" fill="#ffffff" stroke="#d8dde5"/>
<text x="48" y="244" font-family="Segoe UI" font-size="13">Sales by Region</text>
{missing}
<polyline points="520,380 585,340 650,350 715,285 780,305 855,255" fill="none" stroke="{color}" stroke-width="4"/>
<text x="502" y="244" font-family="Segoe UI" font-size="13">Monthly Sales</text>
</svg>""",
        encoding="utf-8",
    )


def formula(metric: str, tableau: str, streamlit: str, classification: str = "EXACT") -> dict:
    return {
        "id": metric.lower().replace(" ", "_"),
        "metric": metric,
        "tableau": tableau,
        "streamlit": streamlit,
        "classification": classification,
        "impact": "None",
    }


def build_spec() -> dict:
    write_dashboard_svg(ASSETS / "customer-tableau.svg", "Customer Analysis - Tableau", "#4a7fba")
    write_dashboard_svg(ASSETS / "customer-streamlit.svg", "Customer Analysis - Streamlit", "#168a77")
    write_dashboard_svg(ASSETS / "shipment-tableau.svg", "Shipment Trends - Tableau", "#4a7fba")
    write_dashboard_svg(ASSETS / "shipment-streamlit.svg", "Shipment Trends - Streamlit", "#168a77", missing_chart=True)

    tableau_region = [
        {"Region": "Central", "Sales": 503171, "Profit": 39706},
        {"Region": "East", "Sales": 691828, "Profit": 91523},
        {"Region": "South", "Sales": 391722, "Profit": 46749},
        {"Region": "West", "Sales": 739814, "Profit": 108419},
    ]
    streamlit_region = [
        {"Region": "Central", "Sales": 503170.67, "Profit": 39706.31},
        {"Region": "East", "Sales": 691828.17, "Profit": 91522.78},
        {"Region": "South", "Sales": 391721.91, "Profit": 46749.11},
        {"Region": "West", "Sales": 739813.61, "Profit": 108418.82},
    ]

    monthly_backend = []
    monthly_tableau = []
    for index in range(24):
        year = 2024 + index // 12
        month = index % 12 + 1
        key = f"{year}-{month:02d}"
        value = 50000 + ((index * 7919) % 62000) + ((index % 4) * 137.23)
        monthly_backend.append({"Order Month": key, "Sales": round(value, 2)})
        monthly_tableau.append({"Order Month": key, "Sales": round(value)})

    customer_charts = [
        {
            "id": "sales-by-region",
            "title": "Sales by Region",
            "chart_type": "Bar chart",
            "grain": ["Region"],
            "measures": [
                {"name": "Sales", "kind": "currency", "display_decimals": 0},
                {"name": "Profit", "kind": "currency", "display_decimals": 0},
            ],
            "tableau_rows": tableau_region,
            "streamlit_rows": streamlit_region,
            "backend_rows": streamlit_region,
            "formulas": [
                formula("Sales", "SUM([Sales])", "SUM(SALES)"),
                formula("Profit", "SUM([Profit])", "SUM(PROFIT)"),
            ],
            "interactions": [
                {
                    "name": "Region = West",
                    "tableau": "West bar remains; $739,814 displayed",
                    "streamlit": "West bar remains; $739,814 displayed",
                    "proof": "Filtered comparison CSV and screenshot",
                    "status": "PASS",
                }
            ],
        },
        {
            "id": "monthly-sales",
            "title": "Monthly Customer Sales",
            "chart_type": "Line chart",
            "grain": ["Order Month"],
            "measures": [{"name": "Sales", "kind": "currency", "display_decimals": 0}],
            "tableau_rows": monthly_tableau,
            "streamlit_rows": monthly_backend,
            "backend_rows": monthly_backend,
            "formulas": [formula("Sales", "SUM([Sales])", "SUM(SALES)")],
            "interactions": [
                {
                    "name": "Date range = 2025",
                    "tableau": "12 month points",
                    "streamlit": "12 month points",
                    "proof": "12 chart-grain comparison rows",
                    "status": "PASS",
                }
            ],
        },
        {
            "id": "customer-kpis",
            "title": "Customer KPIs",
            "chart_type": "KPI group",
            "grain": ["KPI"],
            "measures": [{"name": "Value", "kind": "number", "display_decimals": 0}],
            "tableau_rows": [{"KPI": "Customers", "Value": 793}],
            "streamlit_rows": [{"KPI": "Customers", "Value": 793}],
            "backend_rows": [{"KPI": "Customers", "Value": 793}],
            "formulas": [
                formula(
                    "Distinct Customers",
                    "COUNTD([Customer ID])",
                    "COUNT(DISTINCT CUSTOMER_ID)",
                    "SEMANTICALLY_EQUIVALENT",
                )
            ],
            "interactions": [],
        },
    ]

    blocked_chart = {
        "id": "on-time-trend",
        "title": "On-Time Trend",
        "chart_type": "Line chart",
        "grain": ["Ship Month"],
        "measures": [
            {
                "name": "On-Time Rate",
                "kind": "percent",
                "display_decimals": 1,
                "value_scale": "fraction",
            }
        ],
        "tableau_rows": [],
        "streamlit_rows": [],
        "backend_rows": [],
        "formulas": [
            {
                "id": "on_time_rate",
                "metric": "On-Time Rate",
                "tableau": "SUM(IIF([Shipment Status]='On Time',1,0)) / COUNT([Order ID])",
                "streamlit": "Not generated: Shipment Status unresolved",
                "classification": "NOT_VALIDATED",
                "impact": "Blocks chart and workbook sign-off",
            }
        ],
        "interactions": [
            {
                "name": "Ship Mode filter",
                "tableau": "Updates the trend",
                "streamlit": "Trend unavailable",
                "proof": "No chart-grain output",
                "status": "BLOCKED",
            }
        ],
    }

    return {
        "workbook": "Superstore.twb",
        "run_id": "VAL-EXAMPLE-001",
        "environment": "UAT",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dashboards": [
            {
                "id": "customer-analysis",
                "name": "Customer Analysis",
                "visual": {
                    "tableau_screenshot": str(ASSETS / "customer-tableau.svg"),
                    "streamlit_screenshot": str(ASSETS / "customer-streamlit.svg"),
                    "similarity": 0.981,
                    "threshold": 0.95,
                    "checks": [
                        {"name": "Components", "observed": "4 KPIs and 2 charts matched", "threshold": "Exact", "status": "PASS"},
                        {"name": "Layout", "observed": "Maximum shift 4 px", "threshold": "<= 8 px", "status": "PASS"},
                        {"name": "Marks", "observed": "Bar and line mark counts matched", "threshold": "Exact", "status": "PASS"},
                    ],
                },
                "charts": customer_charts,
            },
            {
                "id": "shipment-trends",
                "name": "Shipment Trends",
                "visual": {
                    "tableau_screenshot": str(ASSETS / "shipment-tableau.svg"),
                    "streamlit_screenshot": str(ASSETS / "shipment-streamlit.svg"),
                    "similarity": 0.72,
                    "threshold": 0.95,
                    "checks": [
                        {"name": "Components", "observed": "On-Time Trend missing", "threshold": "Exact", "status": "FAIL"}
                    ],
                },
                "charts": [blocked_chart],
            },
        ],
    }


if __name__ == "__main__":
    result = generate_report(build_spec(), OUTPUT)
    print(json.dumps(result, indent=2))


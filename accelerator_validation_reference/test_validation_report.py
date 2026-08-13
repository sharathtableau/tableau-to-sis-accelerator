from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from validation_report import BLOCKED, FAIL, PASS, compare_chart, derive_tolerance, generate_report


def chart(tableau: list[dict], streamlit: list[dict], backend: list[dict]) -> dict:
    return {
        "title": "Sales by Region",
        "grain": ["Region"],
        "measures": [{"name": "Sales", "kind": "currency", "display_decimals": 0}],
        "tableau_rows": tableau,
        "streamlit_rows": streamlit,
        "backend_rows": backend,
        "formulas": [
            {
                "metric": "Sales",
                "tableau": "SUM([Sales])",
                "streamlit": "SUM(SALES)",
                "classification": "EXACT",
            }
        ],
        "interactions": [],
    }


class ToleranceTests(unittest.TestCase):
    def test_whole_dollar_tolerance_is_half_dollar(self) -> None:
        value = derive_tolerance({"kind": "currency", "display_decimals": 0})
        self.assertEqual(value, Decimal("0.5"))

    def test_cent_precision_tolerance_is_half_cent(self) -> None:
        value = derive_tolerance({"kind": "currency", "display_decimals": 2})
        self.assertEqual(value, Decimal("0.005"))

    def test_percentage_fraction_tolerance_is_half_display_increment(self) -> None:
        value = derive_tolerance(
            {"kind": "percent", "display_decimals": 1, "value_scale": "fraction"}
        )
        self.assertEqual(value, Decimal("0.0005"))

    def test_count_is_exact(self) -> None:
        value = derive_tolerance({"kind": "count", "display_decimals": 0})
        self.assertEqual(value, Decimal("0"))


class ComparisonTests(unittest.TestCase):
    def test_difference_within_rounding_tolerance_passes(self) -> None:
        result = compare_chart(
            chart(
                [{"Region": "West", "Sales": 100}],
                [{"Region": "West", "Sales": 99.51}],
                [{"Region": "West", "Sales": 99.51}],
            )
        )
        self.assertEqual(result["status"], PASS)

    def test_difference_outside_rounding_tolerance_fails(self) -> None:
        result = compare_chart(
            chart(
                [{"Region": "West", "Sales": 100}],
                [{"Region": "West", "Sales": 99.49}],
                [{"Region": "West", "Sales": 99.49}],
            )
        )
        self.assertEqual(result["status"], FAIL)

    def test_missing_dimension_key_fails(self) -> None:
        result = compare_chart(
            chart(
                [{"Region": "West", "Sales": 100}, {"Region": "East", "Sales": 90}],
                [{"Region": "West", "Sales": 100}],
                [{"Region": "West", "Sales": 100}],
            )
        )
        self.assertEqual(result["status"], FAIL)
        self.assertFalse(result["key_set_match"])

    def test_unvalidated_formula_blocks_empty_chart(self) -> None:
        spec = chart([], [], [])
        spec["formulas"] = [
            {
                "metric": "On-Time Rate",
                "tableau": "SUM([On Time]) / COUNT([Order ID])",
                "streamlit": "Not generated",
                "classification": "NOT_VALIDATED",
            }
        ]
        result = compare_chart(spec)
        self.assertEqual(result["status"], BLOCKED)


class ReportTests(unittest.TestCase):
    def test_generator_writes_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tableau_image = root / "tableau.svg"
            streamlit_image = root / "streamlit.svg"
            tableau_image.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
            streamlit_image.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
            spec = {
                "workbook": "Test.twb",
                "run_id": "TEST-1",
                "environment": "test",
                "generated_at": "2026-08-05T00:00:00Z",
                "dashboards": [
                    {
                        "name": "Dashboard",
                        "visual": {
                            "tableau_screenshot": str(tableau_image),
                            "streamlit_screenshot": str(streamlit_image),
                            "similarity": 1.0,
                            "checks": [{"name": "Marks", "status": "PASS"}],
                        },
                        "charts": [
                            chart(
                                [{"Region": "West", "Sales": 100}],
                                [{"Region": "West", "Sales": 99.75}],
                                [{"Region": "West", "Sales": 99.75}],
                            )
                        ],
                    }
                ],
            }
            result = generate_report(spec, root / "output")
            self.assertEqual(result["status"], PASS)
            self.assertTrue(Path(result["report"]).exists())
            self.assertTrue(Path(result["summary"]).exists())
            comparison_files = list((root / "output" / "evidence").rglob("comparison.csv"))
            self.assertEqual(len(comparison_files), 1)
            self.assertIn("tableau_streamlit_diff", comparison_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


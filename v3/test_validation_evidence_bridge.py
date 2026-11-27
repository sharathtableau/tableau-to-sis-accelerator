import tempfile
import unittest
from pathlib import Path

import validation_evidence_bridge as bridge


class EvidenceBridgeTests(unittest.TestCase):
    def test_aligns_tableau_wrapped_columns_and_keeps_all_rows(self):
        source = [
            {"Region": "Central", "SUM(Sales)": "501.25"},
            {"Region": "East", "SUM(Sales)": "902.50"},
            {"Region": "South", "SUM(Sales)": "701.75"},
            {"Region": "West", "SUM(Sales)": "1100.00"},
        ]
        rows, reason = bridge.align_rows(source, ["Region", "Sales"])
        self.assertIsNone(reason)
        self.assertEqual(4, len(rows))
        self.assertEqual("902.50", rows[1]["Sales"])

    def test_ambiguous_columns_are_blocked_instead_of_guessed(self):
        source = [{"Sales": 1, "SUM(Sales)": 1}]
        rows, reason = bridge.align_rows(source, ["Sales"])
        self.assertIsNone(rows)
        self.assertIn("ambiguous source columns", reason)

    def test_registry_uses_explicit_chart_rows(self):
        registry = bridge.ChartEvidenceRegistry()
        payload = registry.record_chart(
            dashboard="Executive Overview",
            sheet="Sales by Region",
            chart_type="bar",
            grain=["Region"],
            measures=[{"name": "Sales", "kind": "currency"}],
            rows=[
                {"Region": "Central", "Sales": 501.25},
                {"Region": "East", "Sales": 902.50},
            ],
            query="select region, sum(sales) from app_source group by region",
        )
        self.assertIs(payload, registry.get("executive overview", "sales-by-region"))
        self.assertEqual(2, len(payload.rows))

    def test_measure_caption_is_normalized_to_internal_field(self):
        payload = bridge.ChartPayload.create(
            dashboard="Overview",
            sheet="Sales by Region",
            chart_type="bar",
            grain=["Region"],
            measures=[{"name": "Sales", "field": "VAL"}],
            rows=[{"Region": "East", "Sales": 902.50}],
        )
        self.assertEqual({"Region": "East", "VAL": 902.50}, dict(payload.rows[0]))

    def test_csv_bom_is_removed(self):
        rows = bridge.csv_rows("\ufeffRegion,SUM(Sales)\nEast,12.50\n")
        self.assertEqual("East", rows[0]["Region"])

    def test_manifest_keeps_tableau_and_streamlit_images_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tableau.png").write_bytes(b"tableau")
            (root / "streamlit.png").write_bytes(b"streamlit")
            manifest = root / "manifest.json"
            manifest.write_text(
                '{"dashboards":{"Overview":{"visual":{'
                '"tableau_screenshot":"tableau.png",'
                '"streamlit_screenshot":"streamlit.png"}}}}',
                encoding="utf-8",
            )
            bundle = bridge.EvidenceBundle.from_manifest(manifest)
            visual = bundle.visual_for("Overview")
            self.assertTrue(visual.tableau_screenshot.endswith("tableau.png"))
            self.assertTrue(visual.streamlit_screenshot.endswith("streamlit.png"))
            self.assertNotEqual(visual.tableau_screenshot, visual.streamlit_screenshot)


if __name__ == "__main__":
    unittest.main()

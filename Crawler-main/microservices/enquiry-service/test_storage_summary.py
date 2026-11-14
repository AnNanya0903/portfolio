import unittest
from datetime import datetime
from unittest.mock import patch

import storage
from models import Enquiry


class SummaryMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        storage._SUMMARY_CACHE.clear()

    def test_extract_material_preserves_frc_7060(self) -> None:
        self.assertEqual(storage._extract_material_from_text("Material: FRC 7060"), "FRC 7060")

    def test_extract_material_ignores_extended_ag_code_in_unstructured_text(self) -> None:
        self.assertIsNone(
            storage._extract_material_from_text("Compound code AG-ESF-35200", allow_extended_ag_code=False)
        )

    @patch("storage._extract_pdf_insights_with_gemini")
    @patch("storage.list_enquiries")
    def test_summary_prefers_structured_material_over_pdf_code_noise(self, mock_list_enquiries, mock_pdf_insights) -> None:
        mock_list_enquiries.return_value = [
            Enquiry(
                enquiry_id="ENQ-1",
                customer_name="Demo",
                subject="Profile quote",
                channel="email",
                status="open",
                priority="medium",
                description="Customer RFQ",
                material_customer="EPDM",
                received_at=datetime(2026, 4, 6, 12, 0, 0),
            )
        ]
        mock_pdf_insights.return_value = {"ENQ-1": {"material": "AG-ESF-35200"}}

        summary = storage.summarize_enquiries()

        self.assertEqual(summary.metrics.by_material, {"EPDM": 1})


if __name__ == "__main__":
    unittest.main()


class SummaryHighlightWordingTests(unittest.TestCase):
    def setUp(self) -> None:
        storage._SUMMARY_CACHE.clear()

    def _enquiry(self, enquiry_id, priority):
        return Enquiry(enquiry_id=enquiry_id, customer_name="Demo", subject="Seal", channel="email", status="open",
                       priority=priority, description="RFQ", material_customer="EPDM",
                       received_at=datetime(2026, 4, 6, 12, 0, 0))

    @patch("storage._summary_use_gemini", return_value=False)
    @patch("storage._extract_pdf_insights_with_gemini", return_value={})
    @patch("storage.list_enquiries")
    def test_a_tie_names_the_more_urgent_priority_and_counts_read_well(self, mock_list, _pdf, _gemini) -> None:
        mock_list.return_value = [self._enquiry("ENQ-1", "low"), self._enquiry("ENQ-2", "high")]
        details = {h.title: h.detail for h in storage.summarize_enquiries().highlights}
        self.assertEqual(details["Focus Priority"], "'high' has the heaviest load with 1 enquiry.")
        self.assertEqual(details["Material Hotspot"], "'EPDM' features in 2 enquiries this cycle.")
        self.assertTrue(details["Overall Volume"].startswith("2 enquiries tracked"))

    def test_enquiry_count(self) -> None:
        self.assertEqual([storage._enquiry_count(n) for n in (0, 1, 2)],
                         ["0 enquiries", "1 enquiry", "2 enquiries"])

    @patch("storage._summary_use_gemini", return_value=False)
    @patch("storage._extract_pdf_insights_with_gemini", return_value={})
    @patch("storage.list_enquiries")
    def test_narrative_lists_priorities_high_medium_low(self, mock_list, _pdf, _gemini) -> None:
        mock_list.return_value = [self._enquiry("ENQ-1", "low"), self._enquiry("ENQ-2", "medium"),
                                  self._enquiry("ENQ-3", "high")]
        self.assertIn("Priority mix – high: 1, medium: 1, low: 1.", storage.summarize_enquiries().narrative)

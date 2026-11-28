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


class SummaryWithoutWaitingForAiTests(unittest.TestCase):
    """Gemini can take minutes; the page must get the plain summary straight away."""

    def setUp(self) -> None:
        storage._SUMMARY_CACHE.clear()
        storage._SUMMARY_BUILDS.clear()
        self.release = __import__("threading").Event()
        self.ai_calls = 0
        patches = [
            patch.dict("os.environ", {"GEMINI_API_KEY": "test", "ENQUIRY_SUMMARY_USE_GEMINI": "true",
                                      "ENQUIRY_SUMMARY_AI_WAIT_SECONDS": "0.2",
                                      "ENQUIRY_SUMMARY_CACHE_SECONDS": "600"}),
            patch("storage.list_enquiries", return_value=[
                Enquiry(enquiry_id="ENQ-1", customer_name="Demo", subject="Seal", channel="email", status="open",
                        priority="high", description="RFQ", material_customer="EPDM",
                        received_at=datetime(2026, 4, 6, 12, 0, 0))]),
            patch("storage._extract_pdf_insights_with_gemini", side_effect=self._slow_ai),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._finish_builds)

    def _slow_ai(self, enquiries):
        self.ai_calls += 1
        self.release.wait(5)  # stands in for minutes of Gemini work
        return {}

    def _finish_builds(self):
        self.release.set()
        self._wait_for_build()
        storage._SUMMARY_CACHE.clear()
        storage._SUMMARY_BUILDS.clear()

    def _wait_for_build(self):
        for thread in list(storage._SUMMARY_BUILDS.values()):
            thread.join(5)

    def test_plain_summary_first_then_the_full_one_from_the_cache(self) -> None:
        import time as _time
        started = _time.time()
        first = storage.summarize_enquiries()
        self.assertLess(_time.time() - started, 2)
        self.assertTrue(first.ai_pending)
        self.assertEqual(first.metrics.total_enquiries, 1)

        second = storage.summarize_enquiries()  # still building: no second Gemini run
        self.assertTrue(second.ai_pending)
        self.assertEqual(self.ai_calls, 1)

        self.release.set()
        self._wait_for_build()
        done = storage.summarize_enquiries()
        self.assertFalse(done.ai_pending)
        self.assertEqual(self.ai_calls, 1)

    def test_each_filter_gets_its_own_build(self) -> None:
        storage.summarize_enquiries(period="all")
        storage.summarize_enquiries(period="weekly")
        self.assertEqual(self.ai_calls, 2)

    def test_a_fast_ai_answer_is_returned_directly(self) -> None:
        self.release.set()
        self.assertFalse(storage.summarize_enquiries().ai_pending)

    def test_without_ai_the_summary_is_built_directly(self) -> None:
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            response = storage.summarize_enquiries()
        self.assertFalse(response.ai_pending)
        self.assertEqual(self.ai_calls, 0)


class SchemaCheckRunsOnceTests(unittest.TestCase):
    def test_tables_and_columns_are_checked_once_not_on_every_request(self):
        import storage
        from unittest import mock

        engine = object()
        with mock.patch.object(storage, "_get_engine", return_value=engine), \
                mock.patch.object(storage, "_SCHEMA_READY_FOR", None), \
                mock.patch.object(storage._metadata, "create_all") as create_all, \
                mock.patch.object(storage, "_ensure_optional_columns") as columns:
            for _ in range(5):
                storage._ensure_schema()
            self.assertEqual(create_all.call_count, 1)
            self.assertEqual(columns.call_count, 1)
            self.assertEqual(storage._SCHEMA_READY_FOR, id(engine))

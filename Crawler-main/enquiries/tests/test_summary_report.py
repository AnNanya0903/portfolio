"""The enquiry summary report: counts add up, priorities in order, and times in Indian time."""
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from kyc.tests import *  # noqa: F401,F403  (Python 3.14 test-client fix for Django 4.2)


def _summary(**metrics):
    data = {
        "total_enquiries": 6, "open_enquiries": 2, "closed_enquiries": 4, "accepted_enquiries": 1,
        "rejected_enquiries": 1, "converted_enquiries": 0,
        "by_status": {"open": 2, "accepted": 1, "rejected": 1, "closed": 2},
        "by_priority": {"high": 1, "low": 3, "medium": 2}, "by_channel": {}, "by_material": {},
    }
    data.update(metrics)
    # The service sends generated_at as naive UTC.
    return {"generated_at_dt": datetime(2026, 11, 14, 10, 2), "metrics": data, "highlights": [],
            "narrative": "Summary"}


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class SummaryReportTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("sales", password="x"))

    def _get(self, summary, rows=None, **params):
        with patch("enquiries.views.services.fetch_summary", return_value=summary), \
                patch("enquiries.views.services.list_enquiries", return_value=rows or []):
            return self.client.get(reverse("enquiries:summary"), params)

    def test_closed_row_makes_the_counts_add_up(self):
        response = self._get(_summary())
        metrics = response.context["summary"]["metrics"]
        self.assertEqual(metrics["closed_status_enquiries"], 2)  # status "closed" only, not every non-open one
        shown = sum(metrics[key] for key in ("open_enquiries", "accepted_enquiries", "rejected_enquiries",
                                             "converted_enquiries", "closed_status_enquiries"))
        self.assertEqual(shown, metrics["total_enquiries"])
        self.assertContains(response, "Closed enquiries")

    def test_closed_count_without_by_status_from_an_older_service(self):
        summary = _summary()
        del summary["metrics"]["by_status"]
        metrics = self._get(summary).context["summary"]["metrics"]
        self.assertEqual(metrics["closed_status_enquiries"], 2)  # 6 - 2 open - 1 accepted - 1 rejected

    def test_priorities_are_high_medium_low(self):
        metrics = self._get(_summary(by_priority={"low": 3, "high": 1, "urgent?": 1, "medium": 2})) \
            .context["summary"]["metrics"]
        self.assertEqual(list(metrics["by_priority"]), ["high", "medium", "low", "urgent?"])

    def test_times_are_shown_in_indian_time(self):
        rows = [{"enquiry_id": "ENQ-1", "customer_name": "Acme", "subject": "Seal", "status": "open",
                 "priority": "high", "received_at_dt": timezone.now().replace(hour=4, minute=0, second=0,
                                                                              microsecond=0, tzinfo=None)}]
        response = self._get(_summary(), rows, period="all", applied="1")
        self.assertContains(response, "Generated 14 Nov 2026, 15:32 IST")  # 10:02 UTC
        self.assertContains(response, "Received (IST)")
        self.assertContains(response, ", 09:30")  # 04:00 UTC

    def test_a_missing_summary_still_renders(self):
        response = self._get(None)
        self.assertContains(response, "Unable to load summary data")

    def test_a_note_while_the_ai_parts_are_being_prepared(self):
        pending = _summary()
        pending["ai_pending"] = True
        self.assertContains(self._get(pending), "data-summary-ai-pending")
        self.assertNotContains(self._get(_summary()), "data-summary-ai-pending")

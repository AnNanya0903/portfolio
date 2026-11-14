"""The board offers "View comments" only while the quotation is Rejected, not after it is accepted."""
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

import marketing.tests  # noqa: F401  (Python 3.14 test-client fix for Django 4.2)

REASON = "Rejection reason (via share link): Price is higher than our current supplier"


def _row(enquiry_id, status, remarks):
    # Like services._prepare_enquiry, which sets rejection_reason from the remarks whatever the status.
    row = {"enquiry_id": enquiry_id, "status": status, "subject": f"Seal {enquiry_id}", "customer_name": "Acme",
           "remarks": remarks, "received_at_dt": datetime(2026, 11, 1)}
    if "Rejection reason (via share link):" in remarks:
        row["rejection_reason"] = "Price is higher than our current supplier"
    return row


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class ViewCommentsTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", "b@example.com", "x"))

    def _board(self, rows):
        with patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in rows]):
            return self.client.get(reverse("enquiries:board"))

    def _comment_buttons(self, response):
        return response.content.decode().count('data-rejection-reason="')

    def test_rejected_quotation_shows_view_comments(self):
        response = self._board([_row("E1", "rejected", REASON)])
        self.assertEqual(self._comment_buttons(response), 1)
        self.assertContains(response, "Price is higher than our current supplier")

    def test_accepted_after_rejection_hides_view_comments(self):
        for status in ("accepted", "converted", "open"):
            response = self._board([_row("E1", status, "Old note\n" + REASON)])
            self.assertEqual(self._comment_buttons(response), 0, status)
            self.assertNotContains(response, ">\n                  View comments", msg_prefix=status)

    def test_only_the_rejected_one_keeps_it(self):
        response = self._board([_row("E1", "rejected", REASON), _row("E2", "accepted", REASON)])
        self.assertEqual(self._comment_buttons(response), 1)
        self.assertContains(response, 'data-enquiry-id="E1"')
        self.assertNotContains(response, 'data-rejection-reason="Price is higher than our current supplier"\n'
                                         '                  data-enquiry-id="E2"')

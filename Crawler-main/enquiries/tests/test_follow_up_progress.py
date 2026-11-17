"""The Enquiries board shows where the automatic follow-ups stand: the sequence starts when the quotation or
proforma invoice is emailed, counts only the current stage, shows the closing email, and ignores Marketing's."""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries.models import CustomerStatusEmail, FollowUpSent, FollowUpStart

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
SETTINGS = dict(FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_CLOSE_AFTER_DAYS=2,
                FOLLOW_UP_AUTO_ENABLED=True, **STATIC)


@override_settings(**SETTINGS)
class BoardProgressTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("sales", "sales@ourcompany.example", "x")
        self.client.force_login(self.user)
        self.row = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme",
                    "received_at_dt": datetime(2026, 5, 1)}
        patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(self.row)]).start()
        patch("enquiries.views.services.fetch_enquiry", side_effect=lambda *a, **k: dict(self.row)).start()
        self.addCleanup(patch.stopall)

    def _start(self, stage="waiting_po", days_ago=1):
        return FollowUpStart.objects.create(enquiry_id="E1", stage=stage, gmail_thread_id="T1",
                                            customer_email="buyer@acme.example", started_by=self.user,
                                            started_at=timezone.now() - timedelta(days=days_ago))

    def _sent(self, n, stage="waiting_po", last_days_ago=0):
        for i in range(n):
            FollowUpSent.objects.create(
                enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.user, status="sent",
                gmail_thread_id="T1", stage=stage, source="auto",
                sent_at=timezone.now() - timedelta(days=last_days_ago + 2 * (n - 1 - i), minutes=1),
                next_follow_up_on=timezone.localdate() + timedelta(days=2))

    def _board(self):
        response = self.client.get(reverse("enquiries:board"))
        return response, response.context["enquiries"][0].get("follow_up")

    def test_quotation_emailed_shows_the_first_follow_up_before_any_is_sent(self):
        self._start(days_ago=1)
        response, fu = self._board()
        self.assertEqual((fu["count"], fu["started_with"], fu["last_stage"]), (0, "Quotation sent", "waiting_po"))
        self.assertEqual(fu["due"], timezone.localdate(timezone.now() - timedelta(days=1)) + timedelta(days=2))
        self.assertContains(response, "Quotation sent")
        self.assertContains(response, 'data-follow-up-due="later"')

    def test_proforma_invoice_restarts_the_count(self):
        self._start("waiting_po", days_ago=9)
        self._sent(3, last_days_ago=3)
        self._start("payment_pending", days_ago=1)
        _, fu = self._board()
        self.assertEqual((fu["count"], fu["last_stage"], fu["started_with"]), (0, "payment_pending", "Proforma invoice sent"))

    def test_progress_counts_only_the_current_stage(self):
        self._start(days_ago=9)
        self._sent(2, last_days_ago=1)
        _, fu = self._board()
        self.assertEqual((fu["count"], fu["steps"]), (2, [True, True, False, False]))

    def test_all_follow_ups_sent_shows_the_closing_email_date(self):
        self._start(days_ago=12)
        self._sent(4, last_days_ago=1)
        response, fu = self._board()
        self.assertEqual(fu["due_state"], "closing")
        self.assertEqual(fu["closing_on"], timezone.localdate(timezone.now() - timedelta(days=1)) + timedelta(days=2))
        self.assertContains(response, 'data-follow-up-state="closing"')

    @override_settings(FOLLOW_UP_AUTO_ENABLED=False)
    def test_with_automatic_follow_ups_off_it_still_says_completed(self):
        self._start(days_ago=12)
        self._sent(4, last_days_ago=1)
        response, fu = self._board()
        self.assertEqual(fu["due_state"], "limit")
        self.assertContains(response, 'data-follow-up-state="limit"')

    def test_closed_after_the_closing_email_says_no_reply(self):
        self._start(days_ago=14)
        self._sent(4, last_days_ago=3)
        self.row["status"] = "closed"
        CustomerStatusEmail.objects.create(enquiry_id="E1", kind="closing", event_key="closing", status="sent")
        response, fu = self._board()
        self.assertTrue(fu["closed_no_reply"])
        self.assertContains(response, "Closed, no reply")

    def test_marketing_emails_are_not_shown_on_the_enquiries_board(self):
        self.row["status"] = "rejected"
        FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                     customer_email="buyer@acme.example", started_at=timezone.now())
        FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.user,
                                    status="sent", gmail_thread_id="TM", stage="marketing", source="auto")
        _, fu = self._board()
        self.assertIsNone(fu)

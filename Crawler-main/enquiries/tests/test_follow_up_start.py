"""Start follow-ups for a quotation / proforma invoice that was emailed from Gmail outside MagicWand."""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries import auto_follow_up
from enquiries.models import EnquiryContact, FollowUpStart

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
SETTINGS = dict(FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_AUTO_ENABLED=True,
                FOLLOW_UP_DEFAULT_CC="customerservice@avigiri.com", **STATIC)


def conversation(thread="T-Q", email="buyer@acme.example", days_ago=3, subject="Quotation Q-77 - Gaskets"):
    return {"thread_id": thread, "subject": subject, "customer_email": email, "latest_message_id": "m9",
            "last_date": datetime.now(dt_timezone.utc) - timedelta(days=days_ago), "message_count": 1,
            "snippet": "Please find our quotation"}


@override_settings(**SETTINGS)
class StartFollowUpsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("cro", "customerservice@avigiri.com", "x")
        self.client.force_login(self.user)
        self.enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme"}
        patch("enquiries.views.services.fetch_enquiry", side_effect=lambda *a, **k: dict(self.enquiry)).start()
        self.gmail = MagicMock()
        self.gmail.find_enquiry_conversations.return_value = [conversation()]
        patch("enquiries.views.GmailService", return_value=self.gmail).start()
        self.addCleanup(patch.stopall)
        self.url = reverse("enquiries:follow_up_start", args=["E1"])

    def _post(self, **data):
        sent = (timezone.localdate() - timedelta(days=3)).isoformat()
        payload = {"thread": "T-Q", "document": "quotation", "sent_on": sent}
        payload.update(data)
        return self.client.post(self.url, payload, follow=True)

    def test_page_lists_the_conversation_found_in_the_signed_in_gmail(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'data-start-candidate="T-Q"')
        self.assertContains(response, "Quotation Q-77 - Gaskets")
        self.assertContains(response, "customerservice@avigiri.com")  # which mailbox is searched
        self.assertContains(response, "Nothing is emailed from this page")
        self.gmail.send_reply.assert_not_called()
        self.gmail.send_email.assert_not_called()

    def test_starting_records_the_thread_stage_and_sent_date(self):
        response = self._post()
        sent = timezone.localdate() - timedelta(days=3)
        start = FollowUpStart.objects.get()
        self.assertEqual((start.enquiry_id, start.stage, start.gmail_thread_id, start.customer_email),
                         ("E1", "waiting_po", "T-Q", "buyer@acme.example"))
        self.assertEqual(timezone.localdate(start.started_at), sent)
        self.assertEqual(start.cc, "customerservice@avigiri.com")
        self.assertEqual(start.started_by, self.user)
        self.assertContains(response, "Follow-ups started for enquiry #E1")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="E1").customer_email, "buyer@acme.example")
        self.gmail.send_reply.assert_not_called()
        self.gmail.send_email.assert_not_called()  # it only records; nothing is emailed

    def test_the_engine_then_follows_up_two_days_after_the_sent_date_in_that_thread(self):
        self._post(sent_on=(timezone.localdate() - timedelta(days=3)).isoformat())
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        self.assertEqual((cycle.stage, cycle.count, cycle.thread_id), ("waiting_po", 0, "T-Q"))
        self.assertEqual(cycle.next_on, timezone.localdate() - timedelta(days=1))  # sent 3 days ago + 2: due now

    def test_proforma_invoice_starts_the_payment_follow_ups(self):
        self._post(document="proforma")
        self.assertEqual(FollowUpStart.objects.get().stage, "payment_pending")

    def test_only_a_conversation_found_by_the_search_can_be_chosen(self):
        response = self._post(thread="T-OTHER")
        self.assertContains(response, "Please pick the Gmail conversation")
        self.assertFalse(FollowUpStart.objects.exists())

    def test_bad_dates_and_missing_choices_are_refused(self):
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertContains(self._post(sent_on=future), "cannot be in the future")
        self.assertContains(self._post(sent_on=""), "enter the date")
        self.assertContains(self._post(document=""), "quotation or a proforma invoice")
        self.assertFalse(FollowUpStart.objects.exists())

    def test_starting_twice_in_the_same_conversation_does_not_duplicate(self):
        self._post()
        response = self._post()
        self.assertContains(response, "already running")
        self.assertEqual(FollowUpStart.objects.count(), 1)

    def test_nothing_found_explains_what_to_check(self):
        self.gmail.find_enquiry_conversations.return_value = []
        response = self.client.get(self.url)
        self.assertContains(response, "data-start-none")
        self.assertContains(response, "signed in as the mailbox that sent it")

    def test_falls_back_to_the_customer_email_when_the_subject_does_not_match(self):
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example")
        self.gmail.find_enquiry_conversations.side_effect = [[], [conversation(subject="Offer 5521")]]
        self.assertContains(self.client.get(self.url), "Offer 5521")
        second = self.gmail.find_enquiry_conversations.call_args_list[1].kwargs
        self.assertEqual((second["subject"], second["customer_email"]), ("", "buyer@acme.example"))

    def test_closed_enquiries_cannot_start_follow_ups(self):
        self.enquiry["status"] = "closed"
        response = self.client.get(self.url, follow=True)
        self.assertContains(response, "no follow-up is needed")
        self.assertFalse(FollowUpStart.objects.exists())


@override_settings(**SETTINGS)
class BoardLinkTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("cro", "c@avigiri.com", "x"))
        rows = [{"enquiry_id": "E1", "status": "open", "subject": "A", "customer_name": "Acme",
                 "received_at_dt": datetime(2026, 5, 1)},
                {"enquiry_id": "E2", "status": "converted", "subject": "B", "customer_name": "Beta",
                 "received_at_dt": datetime(2026, 5, 2)}]
        patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in rows]).start()
        self.addCleanup(patch.stopall)

    def test_link_only_on_open_or_accepted_enquiries_without_a_sequence(self):
        response = self.client.get(reverse("enquiries:board"))
        self.assertContains(response, reverse("enquiries:follow_up_start", args=["E1"]))
        self.assertNotContains(response, reverse("enquiries:follow_up_start", args=["E2"]))
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T", customer_email="b@x.example")
        response = self.client.get(reverse("enquiries:board"))
        self.assertNotContains(response, reverse("enquiries:follow_up_start", args=["E1"]))

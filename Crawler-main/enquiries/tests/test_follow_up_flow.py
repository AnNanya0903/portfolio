"""Quotation / proforma invoice follow-ups every 2 days, the closing email, and Marketing's follow-ups after a
rejection, all in ONE Gmail thread per enquiry."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.follow_up_templates import build_follow_up_body
from enquiries import auto_follow_up, status_emails
import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from enquiries.models import CustomerStatusEmail, FollowUpSent, FollowUpStart, ShareLink
from enquiries.tests.share_helpers import make_share
from enquiries.views import _create_share_link_from_values
from marketing.models import MarketingActivity, MarketingLead

TODAY = timezone.localdate()
FLOW = dict(
    FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=2,
    MARKETING_AUTO_FOLLOW_UP=True, MARKETING_FOLLOW_UP_COUNT=4,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
)
OURS = "sales@ourcompany.example"


def _gmail(thread_id="T-QUOTE", last_sender=OURS):
    svc = MagicMock()
    svc.own_addresses.return_value = {OURS}
    # A thread that passes the thread check: our quotation to the buyer, same subject, naming the enquiry.
    svc.get_thread_messages.return_value = [
        {"id": "m1", "thread_id": thread_id, "sender": OURS, "to": "buyer@acme.example", "subject": "Quotation Q-77",
         "body_text": "Quotation for E1"},
        {"id": "m2", "thread_id": thread_id, "sender": last_sender, "to": "buyer@acme.example",
         "subject": "Re: Quotation Q-77"}]
    svc.get_message.return_value = {"id": "m2", "subject": "Quotation Q-77 - Gaskets", "thread_id": thread_id}
    svc.send_reply.return_value = {"id": "r1", "threadId": thread_id}
    return svc


@override_settings(**FLOW)
class EngineTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme"}
        self.share_url, self.share_password = make_share("E1")
        for name in ("list_enquiries", "fetch_enquiry"):
            p = patch.object(auto_follow_up.services, name,
                             side_effect=(lambda *a, **k: [self.enquiry]) if name == "list_enquiries"
                             else (lambda *a, **k: self.enquiry))
            p.start()
            self.addCleanup(p.stop)

    def _start(self, stage="waiting_po", days_ago=2, thread="T-QUOTE"):
        return FollowUpStart.objects.create(enquiry_id="E1", stage=stage, gmail_thread_id=thread,
                                            customer_email="buyer@acme.example", subject="Quotation Q-77",
                                            started_at=timezone.now() - timedelta(days=days_ago))

    def _run(self, gmail):
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            return auto_follow_up.run_auto_follow_up("E1")

    def test_first_follow_up_is_due_two_days_after_the_quotation(self):
        self._start(days_ago=1)
        self.assertEqual(auto_follow_up.due_follow_ups(), [])
        FollowUpStart.objects.update(started_at=timezone.now() - timedelta(days=2))
        due = auto_follow_up.due_follow_ups()
        self.assertEqual([(d["enquiry_id"], d["attempt"], d["stage"]) for d in due], [("E1", 1, "waiting_po")])

    def test_follow_up_replies_in_the_quotation_thread_every_two_days(self):
        self._start()
        gmail = _gmail()
        result = self._run(gmail)
        self.assertEqual(result["result"], "sent")
        gmail.get_thread_messages.assert_called_with("T-QUOTE", limit=50)
        kwargs = gmail.send_reply.call_args.kwargs
        self.assertEqual(kwargs["original_message"]["thread_id"], "T-QUOTE")
        self.assertIn("quotation we shared for Gaskets", kwargs["body"])
        self.assertIn(self.share_url, kwargs["body"])
        row = FollowUpSent.objects.get()
        self.assertEqual((row.gmail_thread_id, row.stage, row.next_follow_up_on),
                         ("T-QUOTE", "waiting_po", TODAY + timedelta(days=2)))
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        self.assertEqual((cycle.count, cycle.thread_id), (1, "T-QUOTE"))

    def test_customer_reply_stops_the_follow_ups(self):
        self._start()
        gmail = _gmail(last_sender="Buyer <buyer@acme.example>")
        result = self._run(gmail)
        self.assertEqual(result["result"], "skipped")
        self.assertIn("customer replied", result["reason"])
        gmail.send_reply.assert_not_called()

    def test_proforma_replaces_the_quotation_follow_ups(self):
        self._start(days_ago=6)
        FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales,
                                    status="sent", gmail_thread_id="T-QUOTE", stage="waiting_po",
                                    sent_at=timezone.now() - timedelta(days=4), next_follow_up_on=TODAY)
        self._start(stage="payment_pending", days_ago=2)
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        self.assertEqual((cycle.stage, cycle.count, cycle.next_on), ("payment_pending", 0, TODAY))
        result = self._run(_gmail())
        self.assertEqual((result["stage"], result["attempt"]), ("payment_pending", 1))

    def test_follow_up_button_counts_only_its_stage(self):
        for stage, days in (("general", 9), ("general", 7), ("waiting_po", 2)):
            FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales,
                                        status="sent", gmail_thread_id="T1", stage=stage,
                                        sent_at=timezone.now() - timedelta(days=days), next_follow_up_on=TODAY)
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        self.assertEqual((cycle.stage, cycle.count), ("waiting_po", 1))

    def test_closing_email_after_four_follow_ups_and_two_days(self):
        self._start(days_ago=12)
        for days in (10, 8, 6, 2):
            FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales,
                                        status="sent", gmail_thread_id="T-QUOTE", stage="waiting_po",
                                        sent_at=timezone.now() - timedelta(days=days, minutes=5))
        FollowUpSent.objects.update(next_follow_up_on=TODAY)
        self.assertEqual([(d["enquiry_id"], d["attempt"]) for d in auto_follow_up.due_follow_ups()], [("E1", 5)])
        FollowUpSent.objects.filter(sent_at__lt=timezone.now() - timedelta(days=9)).delete()
        self.assertEqual([d["attempt"] for d in auto_follow_up.due_follow_ups()], [4])  # only 3 of 4 sent


@override_settings(**FLOW)
class MarketingTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme"}
        p = patch.object(auto_follow_up.services, "fetch_enquiry", side_effect=lambda *a, **k: self.enquiry)
        p.start()
        self.addCleanup(p.stop)

    def _marketing(self, sent=0):
        # An enquiry thread exists too (the quotation); Marketing's thread is T-MKT, separate from it.
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T-QUOTE",
                                     customer_email="buyer@acme.example",
                                     started_at=timezone.now() - timedelta(days=30))
        FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                     customer_email="buyer@acme.example",
                                     started_at=timezone.now() - timedelta(days=2 * sent + 2, minutes=5))
        for n in range(sent):
            FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales,
                                        status="sent", gmail_thread_id="T-MKT", stage="marketing",
                                        sent_at=timezone.now() - timedelta(days=2 * (sent - n), minutes=1),
                                        next_follow_up_on=TODAY)

    def _run(self):
        gmail = _gmail(thread_id="T-MKT")
        gmail.send_email.return_value = {"id": "n1", "threadId": "T-MKT"}
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            return auto_follow_up.run_auto_follow_up("E1"), gmail

    def test_marketing_thread_never_becomes_the_enquiry_thread(self):
        from enquiries import share_email
        self._marketing(sent=2)
        self.assertEqual(share_email.existing_thread("E1")["thread_id"], "T-QUOTE")  # Send to customer
        # The manual Follow-up button / status emails look it up the same way.
        from enquiries.views import _resolve_follow_up_conversation
        gmail = _gmail()
        gmail.describe_conversation.return_value = {"thread_id": "T-QUOTE", "latest_message_id": "m2",
                                                    "customer_email": "buyer@acme.example"}
        outcome, conversation = _resolve_follow_up_conversation("E1", self.enquiry, gmail)
        self.assertEqual(outcome, "ok")
        gmail.describe_conversation.assert_called_with("T-QUOTE")

    @override_settings(MARKETING_AUTO_FOLLOW_UP=False)
    def test_marketing_can_be_switched_off(self):
        self._marketing(sent=0)
        self.assertIn("turned off", self._run()[0]["reason"])


class TemplateTests(TestCase):
    def test_marketing_closing_text(self):
        body = build_follow_up_body("marketing", reference="E1", subject="Gaskets", attempt=5)
        self.assertTrue(body.startswith("Dear Sir/Madam,"))
        self.assertIn("we won't send further follow-ups", body)
        self.assertIn("our team will get back to you promptly", body)
        self.assertIn("Kind Regards,\nKumar Govind R\nClient Support Officer\n", body)
        self.assertTrue(body.endswith("Visit: www.avigiri.com\n\n"
                                      "Reply STOP if you'd prefer not to hear from us."))


@override_settings(**FLOW)
class SendToCustomerStartsTheFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("sales", OURS, "x")
        self.client.force_login(self.user)
        result = _create_share_link_from_values(RequestFactory().get("/"), {"subject": "Gaskets"},
                                                share_password="Secret-123", enquiry_id="E1",
                                                pdf_bytes=b"%PDF-q", pdf_filename="q.pdf")
        self.record = ShareLink.objects.get(token=result["token"])
        patch("enquiries.views.services.fetch_enquiry",
              return_value={"enquiry_id": "E1", "subject": "Gaskets", "general_attachments": []}).start()
        self.gmail = _gmail(thread_id="T-NEW")
        self.gmail.send_email.return_value = {"id": "s1", "threadId": "T-NEW"}
        patch("enquiries.views.GmailService", return_value=self.gmail).start()
        self.addCleanup(patch.stopall)

    def _post(self, **data):
        payload = {"share_id": self.record.id, "share_password": "Secret-123", "to": "buyer@acme.example",
                   "subject": "Quotation E1 - Gaskets", "body": "Dear Sir/Madam, ...", "document": "quotation"}
        payload.update(data)
        return self.client.post(reverse("enquiries:send_share_email"), payload)

    def test_sending_the_quotation_starts_the_follow_ups_in_its_thread(self):
        response = self._post()
        self.assertContains(response, "Automatic follow-ups will reply in this email thread every 2 days")
        self.gmail.send_email.assert_called_once()
        self.gmail.send_reply.assert_not_called()
        start = FollowUpStart.objects.get()  # no button: the sequence starts now
        self.assertEqual((start.gmail_thread_id, start.customer_email, start.subject, start.stage),
                         ("T-NEW", "buyer@acme.example", "Quotation E1 - Gaskets", "waiting_po"))
        self._post()  # a revised quotation does not restart it
        self.assertEqual(FollowUpStart.objects.count(), 1)

    def test_proforma_replies_in_the_existing_thread(self):
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T-QUOTE",
                                     customer_email="buyer@acme.example", subject="Quotation Q-77 - Gaskets")
        self.gmail.send_reply.return_value = {"id": "r1", "threadId": "T-QUOTE"}
        response = self._post(document="proforma")
        self.assertContains(response, "Proforma invoice emailed to buyer@acme.example in the existing email thread")
        self.gmail.send_email.assert_not_called()
        self.gmail.get_thread_messages.assert_called_with("T-QUOTE", limit=50)
        kwargs = self.gmail.send_reply.call_args.kwargs
        self.assertEqual(kwargs["subject"], "Quotation Q-77 - Gaskets")  # thread subject kept
        self.assertEqual([a["filename"] for a in kwargs["attachments"]], ["q.pdf"])
        self.assertEqual(FollowUpStart.objects.filter(stage="payment_pending").count(), 1)  # the PI's own sequence

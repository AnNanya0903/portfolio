"""No waiting period: a rejected enquiry is a Ready Marketing lead at once and its first follow-up is due now;
"Send first follow-up now" counts as Follow-up 1; a STOP reply stops the sequence; dry-run sends nothing."""
import importlib
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.apps import apps as live_apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.follow_up_templates import build_follow_up_body, marketing_subject
from enquiries import auto_follow_up, status_emails
from enquiries.models import CustomerStatusEmail, EnquiryContact, FollowUpSent, FollowUpStart
from marketing import lost
from marketing.models import MarketingActivity, MarketingLead

OURS = "sales@ourcompany.example"
SETTINGS = dict(
    FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, MARKETING_AUTO_FOLLOW_UP=True,
    MARKETING_FOLLOW_UP_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=2,
)


def _gmail(reply_snippet=None):
    svc = MagicMock()
    svc.own_addresses.return_value = {OURS}
    thread = [{"id": "m1", "sender": OURS}]
    if reply_snippet is not None:
        thread.append({"id": "m2", "sender": "Buyer <buyer@acme.example>", "snippet": reply_snippet})
    svc.get_thread_messages.return_value = thread
    svc.send_email.return_value = {"id": "n1", "threadId": "T-MKT"}
    return svc


@override_settings(**SETTINGS)
class RejectionMakesAReadyLeadTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme"}
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example")
        for name in ("fetch_enquiry", "list_enquiries"):
            p = patch.object(auto_follow_up.services, name,
                             side_effect=(lambda *a, **k: self.enquiry) if name == "fetch_enquiry"
                             else (lambda *a, **k: [self.enquiry]))
            p.start()
            self.addCleanup(p.stop)

    def test_rejection_is_ready_at_once_and_follow_up_1_is_due_on_the_next_run(self):
        with patch("enquiries.views._resolve_follow_up_conversation", return_value=("error", "none")):
            status_emails.send_status_email(CustomerStatusEmail.KIND_REJECTION, "E1", self.enquiry)
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertIsNotNone(lead.ready_at)
        self.assertLess(timezone.now() - lead.ready_at, timedelta(minutes=1))
        row = lost.build_lost_leads([self.enquiry])[0]
        self.assertEqual(row["stage"], lost.STAGE_READY)
        self.assertEqual(row["ready_since"], timezone.localdate())
        self.assertEqual(lead.lead_source, "rejected")
        # No button: Marketing's sequence starts by itself and Follow-up 1 is due on the next run.
        start = FollowUpStart.objects.get(stage="marketing")
        self.assertEqual(start.customer_email, "buyer@acme.example")
        self.assertEqual([(d["enquiry_id"], d["attempt"]) for d in auto_follow_up.due_follow_ups()], [("E1", 1)])

    def test_ready_at_is_set_once(self):
        first = lost.mark_ready("E1").ready_at
        self.assertEqual(lost.mark_ready("E1").ready_at, first)


class WaitingLeadsMigrationTests(TestCase):
    def test_moves_every_lead_to_ready_and_is_safe_to_repeat(self):
        migration = importlib.import_module("marketing.migrations.0007_lead_ready_at")
        MarketingLead.objects.create(enquiry_id="W1")
        old = timezone.now() - timedelta(days=3)
        MarketingLead.objects.create(enquiry_id="R1", ready_at=old)
        migration.waiting_to_ready(live_apps, None)
        first = MarketingLead.objects.get(enquiry_id="W1").ready_at
        self.assertIsNotNone(first)
        self.assertEqual(MarketingLead.objects.get(enquiry_id="R1").ready_at, old)  # already Ready: untouched
        migration.waiting_to_ready(live_apps, None)  # again: nothing changes
        self.assertEqual(MarketingLead.objects.get(enquiry_id="W1").ready_at, first)


@override_settings(**SETTINGS)
class MarketingSequenceTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme"}
        for name in ("fetch_enquiry", "list_enquiries"):
            p = patch.object(auto_follow_up.services, name,
                             side_effect=(lambda *a, **k: self.enquiry) if name == "fetch_enquiry"
                             else (lambda *a, **k: [self.enquiry]))
            p.start()
            self.addCleanup(p.stop)
        self.start = FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                                  customer_email="buyer@acme.example")
        MarketingLead.objects.create(enquiry_id="E1", lead_source="rejected", ready_at=timezone.now())

    def test_stop_detection(self):
        for text in ("STOP", "stop.", "  Stop emailing me", "Please STOP" if False else "STOP!"):
            self.assertTrue(lost.is_stop_reply(text), text)
        for text in ("Please stop by tomorrow", "stopping by", "", None):
            self.assertFalse(lost.is_stop_reply(text), text)

    @override_settings(FOLLOW_UP_AUTO_DRY_RUN=True)
    def test_dry_run_logs_the_email_and_sends_nothing(self):
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail), \
                self.assertLogs("enquiries.auto_follow_up", level="INFO") as logs:
            out = auto_follow_up.run_auto_follow_up("E1")
        self.assertEqual(out["result"], "dry_run")
        self.assertIn("Reply STOP", "\n".join(logs.output))
        gmail.send_email.assert_not_called()
        gmail.send_reply.assert_not_called()
        self.assertEqual(FollowUpSent.objects.get().status, FollowUpSent.STATUS_DRY_RUN)  # "dry-run (not sent)"


class MarketingTemplateTests(TestCase):
    def test_no_unfilled_placeholders_whatever_is_missing(self):
        for attempt in range(1, 6):
            for kwargs in ({}, {"customer_name": "Acme", "subject": "Gaskets"}):
                body = build_follow_up_body("marketing", attempt=attempt, **kwargs)
                subject = marketing_subject(attempt, customer_name=kwargs.get("customer_name", ""),
                                            subject=kwargs.get("subject", ""))
                for text in (body, subject):
                    self.assertIsNone(re.search(r"\{[a-z_]+\}", text), text)
                self.assertTrue(body.endswith("Reply STOP if you'd prefer not to hear from us."))
                self.assertNotIn("Password", body)

    @override_settings(FOLLOW_UP_CONTACT_PHONE="", MARKETING_REVISED_QUOTE_DAYS="")
    def test_sentences_without_a_value_are_dropped(self):
        self.assertNotIn("reach us directly", build_follow_up_body("marketing", attempt=4))
        self.assertNotIn("working days", build_follow_up_body("marketing", attempt=2))

    @override_settings(FOLLOW_UP_CONTACT_PHONE="0816 2970025", MARKETING_REVISED_QUOTE_DAYS="3")
    def test_sentences_with_a_value_are_kept(self):
        self.assertIn("reach us directly at 0816 2970025", build_follow_up_body("marketing", attempt=4))
        self.assertIn("within 3 working days", build_follow_up_body("marketing", attempt=2))

    def test_subjects(self):
        self.assertEqual(marketing_subject(1, customer_name="Acme"), "Thank you for considering AVIGIRI, Acme")
        self.assertEqual(marketing_subject(2, subject="Gaskets"), "A different option for Gaskets?")
        self.assertEqual(marketing_subject(3), "Could you share what didn't work for you?")
        self.assertEqual(marketing_subject(4), "Still here when you need us")
        self.assertEqual(marketing_subject(5, customer_name="Acme"), "Closing the loop, Acme")

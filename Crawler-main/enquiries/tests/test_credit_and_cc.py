"""Credit period on accepted orders and CC on follow-up emails."""
import base64
from datetime import datetime, timedelta
from email import message_from_bytes
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.follow_up_templates import build_follow_up_body
from dashboard.gmail_service import GmailService
from enquiries import auto_follow_up, payment_terms
from enquiries.models import EnquiryContact, FollowUpSent

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
AUTO_ON = dict(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
               FOLLOW_UP_MAX_COUNT=4, **STATIC)


class ParseCcTests(TestCase):
    def test_splits_dedupes_and_skips_the_customer(self):
        cc, error = payment_terms.parse_cc("A@x.example; b@y.example, a@x.example buyer@acme.example",
                                           exclude="buyer@acme.example")
        self.assertEqual((cc, error), (["a@x.example", "b@y.example"], ""))

    def test_invalid_address_is_reported(self):
        cc, error = payment_terms.parse_cc("ok@x.example, not-an-email")
        self.assertEqual(cc, [])
        self.assertIn("not-an-email", error)

    def test_too_many(self):
        _, error = payment_terms.parse_cc(",".join(f"u{n}@x.example" for n in range(11)))
        self.assertIn("at most", error)

    @override_settings(FOLLOW_UP_DEFAULT_CC="customerservice@avigiri.com")
    def test_default_cc_then_what_the_enquiry_used_last(self):
        self.assertEqual(payment_terms.default_cc("E1"), "customerservice@avigiri.com")
        EnquiryContact.objects.create(enquiry_id="E1", follow_up_cc="boss@avigiri.com")
        self.assertEqual(payment_terms.default_cc("E1"), "boss@avigiri.com")


class CreditPeriodRuleTests(TestCase):
    def test_due_date_and_hold(self):
        today = timezone.localdate()
        contact = EnquiryContact.objects.create(enquiry_id="E1", payment_status="pending",
                                                credit_period_days=30, credit_starts_on=today - timedelta(days=10))
        self.assertEqual(payment_terms.credit_due_date(contact), today + timedelta(days=20))
        self.assertEqual(payment_terms.credit_hold_until("E1"), today + timedelta(days=20))
        self.assertIsNone(payment_terms.credit_hold_until("E1", today + timedelta(days=20)))  # due today: remind
        contact.payment_status = "paid"
        contact.save()
        self.assertIsNone(payment_terms.credit_hold_until("E1"))

    def test_no_credit_period_no_hold(self):
        EnquiryContact.objects.create(enquiry_id="E1", payment_due_on=timezone.localdate() + timedelta(days=5))
        self.assertIsNone(payment_terms.credit_hold_until("E1"))

    def test_payment_reminder_mentions_the_credit_period(self):
        due = datetime(2026, 12, 12).date()
        body = build_follow_up_body("payment_pending", reference="E1", subject="Gasket", due_on=due,
                                    credit_days=30, today=datetime(2026, 12, 20).date())
        self.assertIn("As per the agreed credit period of 30 days, the payment was due on 12 December 2026.", body)
        early = build_follow_up_body("payment_pending", reference="E1", subject="Gasket", due_on=due,
                                     credit_days=30, today=datetime(2026, 12, 1).date())
        self.assertIn("the payment is due on 12 December 2026", early)
        # other stages and orders without terms are unchanged
        self.assertNotIn("credit period", build_follow_up_body("general", reference="E1", subject="G", due_on=due,
                                                                credit_days=30))
        self.assertEqual(build_follow_up_body("payment_pending", reference="E1", subject="G"),
                         build_follow_up_body("payment_pending", reference="E1", subject="G", due_on=None))

    def test_automatic_payment_reminder_waits_for_the_due_date(self):
        sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        today = timezone.localdate()
        latest = FollowUpSent.objects.create(enquiry_id="E1", customer_email="b@acme.example", sent_by=sales,
                                             status="sent", gmail_thread_id="t1", stage="payment_pending",
                                             next_follow_up_on=today)
        EnquiryContact.objects.create(enquiry_id="E1", credit_period_days=30, credit_starts_on=today)
        enquiry = {"enquiry_id": "E1", "status": "accepted"}
        reason = auto_follow_up._skip_reason("E1", enquiry, latest, 1, today)
        self.assertIn("credit period", reason)
        self.assertEqual(auto_follow_up._skip_reason("E1", enquiry, latest, 1, today + timedelta(days=30)), "")


@override_settings(**STATIC)
class BoardCreditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("boss", "b@example.com", "x")
        self.client.force_login(self.user)
        self.rows = [{"enquiry_id": "C1", "status": "accepted", "subject": "S1", "customer_name": "A",
                      "received_at_dt": datetime(2026, 5, 1)}]
        patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in self.rows]).start()
        patch("enquiries.views.services.fetch_enquiry",
              side_effect=lambda root, eid, **k: {"enquiry_id": eid, "status": "accepted", "subject": "S1"}).start()
        patch("enquiries.views.services.update_enquiry", return_value={}).start()
        patch("enquiries.views.status_emails.send_status_email", return_value={"result": "sent", "note": ""}).start()
        self.addCleanup(patch.stopall)

    def _post(self, **data):
        return self.client.post(reverse("enquiries:board"),
                                {"action": "update_payment", "enquiry_id": "C1", **data}, follow=True)

    def test_credit_period_sets_the_due_date_and_board_shows_it(self):
        start = timezone.localdate()
        response = self._post(payment_status="pending", credit_period_days="45", credit_starts_on=start.isoformat())
        contact = EnquiryContact.objects.get(enquiry_id="C1")
        self.assertEqual((contact.credit_period_days, contact.credit_starts_on), (45, start))
        self.assertEqual(contact.payment_due_on, start + timedelta(days=45))
        self.assertContains(response, "payment reminders wait until then")

        FollowUpSent.objects.create(enquiry_id="C1", sent_by=self.user, status="sent", stage="payment_pending",
                                    next_follow_up_on=start + timedelta(days=3), cc="boss@avigiri.com")
        board = self.client.get(reverse("enquiries:board"))
        row = board.context["enquiries"][0]
        self.assertTrue(row["payment"]["in_credit"])
        self.assertEqual(row["follow_up"]["due_state"], "credit")
        self.assertContains(board, "45-day credit")
        self.assertContains(board, "(in credit period)")
        self.assertContains(board, 'data-follow-up-state="credit"')
        self.assertContains(board, "CC: boss@avigiri.com")

    def test_credit_start_defaults_to_today_and_empty_clears_it(self):
        self._post(payment_status="pending", credit_period_days="30")
        contact = EnquiryContact.objects.get(enquiry_id="C1")
        self.assertEqual(contact.credit_starts_on, timezone.localdate())
        self._post(payment_status="partial", credit_period_days="")
        contact.refresh_from_db()
        self.assertIsNone(contact.credit_period_days)
        self.assertIsNone(contact.credit_starts_on)

    def test_bad_credit_period_is_rejected(self):
        for value in ("-5", "abc", "400"):
            response = self._post(payment_status="pending", credit_period_days=value)
            self.assertContains(response, "Credit period must be")
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="C1").exists())


class SendReplyCcTests(TestCase):
    def test_cc_header(self):
        svc = GmailService.__new__(GmailService)
        svc.service = MagicMock()
        svc.send_reply(original_message={"sender": "b@acme.example", "subject": "Q", "thread_id": "t"},
                       body="Hello", to="b@acme.example", cc=["x@avigiri.com", "y@avigiri.com"])
        raw = svc.service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
        msg = message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(msg["Cc"], "x@avigiri.com, y@avigiri.com")

    def test_no_cc_no_header(self):
        svc = GmailService.__new__(GmailService)
        svc.service = MagicMock()
        svc.send_reply(original_message={"sender": "b@acme.example", "subject": "Q", "thread_id": "t"}, body="Hi")
        raw = svc.service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
        self.assertIsNone(message_from_bytes(base64.urlsafe_b64decode(raw))["Cc"])


@override_settings(**STATIC)
class ComposeCcTests(TestCase):
    OURS = "sales@ourcompany.example"
    ENQUIRY = {"enquiry_id": "ENQ-7", "subject": "Gasket quote", "customer_name": "Acme",
               "customer_reference": "EMAIL-orig-1", "status": "accepted"}
    CONVO = {"thread_id": "th-1", "subject": "Gasket quote", "customer_email": "buyer@acme.example",
             "latest_message_id": "gm-1", "message_count": 2}

    def setUp(self):
        self.user = get_user_model().objects.create_user("stepper", "s@example.com", "x")
        self.client.force_login(self.user)
        patch("dashboard.views._ensure_local_demo_emails").start()
        self.addCleanup(patch.stopall)
        gs = MagicMock()
        gs.get_message.return_value = {"id": "orig-1", "thread_id": "th-1"}
        gs.describe_conversation.return_value = dict(self.CONVO)
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(self.ENQUIRY)), \
             patch("enquiries.views.GmailService", return_value=gs):
            self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]))
        msg = {"id": "gm-1", "thread_id": "th-1", "sender": f"Sales <{self.OURS}>", "subject": "Re: Gasket quote",
               "attachments": []}
        self.svc = MagicMock()
        self.svc.get_message.return_value = msg
        self.svc.get_thread_messages.return_value = [msg]
        self.svc.own_addresses.return_value = {self.OURS}
        self.svc.send_reply.return_value = {"id": "sent-1", "threadId": "th-1"}

    def _compose(self, data=None):
        with patch("dashboard.views.GmailService", return_value=self.svc):
            url = reverse("dashboard:compose_email", args=["gm-1"])
            return self.client.post(url, data) if data is not None else self.client.get(url)

    def _send(self, cc):
        return self._compose({"subject": "Re: Gasket quote", "body": "Hi", "follow_up_stage": "payment_pending",
                              "follow_up_token": self.client.session["follow_up_context"]["token"],
                              "follow_up_cc": cc})

    @override_settings(FOLLOW_UP_DEFAULT_CC="customerservice@avigiri.com")
    def test_cc_prefilled_sent_saved_and_remembered(self):
        page = self._compose()
        self.assertContains(page, 'name="follow_up_cc"')
        self.assertContains(page, 'value="customerservice@avigiri.com"')
        response = self._send("customerservice@avigiri.com, Boss@Avigiri.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.svc.send_reply.call_args.kwargs["cc"], ["customerservice@avigiri.com", "boss@avigiri.com"])
        record = FollowUpSent.objects.get()
        self.assertEqual(record.cc, "customerservice@avigiri.com, boss@avigiri.com")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-7").follow_up_cc, record.cc)

    def test_invalid_cc_blocks_sending(self):
        response = self._send("oops")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not a valid CC email address")
        self.svc.send_reply.assert_not_called()
        self.assertFalse(FollowUpSent.objects.exists())

    def test_empty_cc_sends_without_cc(self):
        self._send("")
        self.assertIsNone(self.svc.send_reply.call_args.kwargs["cc"])

    def test_credit_period_notice_and_due_date_default(self):
        today = timezone.localdate()
        EnquiryContact.objects.create(enquiry_id="ENQ-7", payment_status="pending", credit_period_days=30,
                                      credit_starts_on=today)
        page = self._compose()
        due = today + timedelta(days=30)
        self.assertContains(page, "data-credit-period")
        self.assertEqual(page.context["next_follow_up_default"], due.isoformat())
        self.assertIn("agreed credit period of 30 days", page.context["form"].initial["body"])


@override_settings(**AUTO_ON)
class AutoFollowUpCcTests(TestCase):
    def test_automatic_follow_up_reuses_the_last_cc(self):
        sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        FollowUpSent.objects.create(enquiry_id="E1", customer_email="b@acme.example", sent_by=sales, status="sent",
                                    gmail_thread_id="t1", stage="waiting_po", next_follow_up_on=timezone.localdate(),
                                    cc="boss@avigiri.com, sales@ourcompany.example")
        gmail = MagicMock()
        gmail.own_addresses.return_value = {"sales@ourcompany.example"}
        gmail.get_thread_messages.return_value = [{"id": "m2", "sender": "sales@ourcompany.example"}]
        gmail.get_message.return_value = {"id": "m2", "subject": "Re: Q", "thread_id": "t1"}
        gmail.send_reply.return_value = {"id": "s2", "threadId": "t1"}
        with patch("enquiries.auto_follow_up.services.fetch_enquiry",
                   return_value={"enquiry_id": "E1", "status": "open", "subject": "Q"}), \
             patch("enquiries.auto_follow_up.GmailService", return_value=gmail):
            result = auto_follow_up.run_auto_follow_up("E1")
        self.assertEqual(result["result"], "sent", result)
        self.assertEqual(gmail.send_reply.call_args.kwargs["cc"], ["boss@avigiri.com"])  # own address dropped
        self.assertEqual(FollowUpSent.objects.filter(source="auto").get().cc, "boss@avigiri.com")

"""Tests for the order process: PI status, payment tracking, and automatic rejection / PI-disagree emails."""
import re
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.follow_up_templates import STAGE_CHOICES, STAGE_KEYS, build_follow_up_body
from enquiries import auto_follow_up, status_emails
from enquiries.models import CustomerStatusEmail, EnquiryContact, FollowUpSent
from enquiries.views import _follow_up_stopped_reason

AUTO_ON = dict(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
               FOLLOW_UP_MAX_COUNT=4, STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")

ENQUIRY = {"enquiry_id": "E1", "status": "rejected", "subject": "Gasket quote", "customer_name": "Acme",
           "customer_reference": "", "remarks": ""}


def _gmail():
    svc = MagicMock()
    svc.own_addresses.return_value = {"sales@ourcompany.example"}
    svc.get_message.return_value = {"id": "m2", "subject": "Re: Gasket quote", "thread_id": "t1"}
    svc.send_reply.return_value = {"id": "sent-1", "threadId": "t1"}
    return svc


def _conversation(email="buyer@acme.example"):
    return ("ok", {"thread_id": "t1", "customer_email": email, "latest_message_id": "m2"})


class StageTests(TestCase):
    def test_stages_match_the_process(self):
        labels = dict(STAGE_CHOICES)
        self.assertNotIn("sample_sent", STAGE_KEYS)
        self.assertEqual(labels["waiting_po"], "Quotation sent")
        self.assertEqual([key for key, _ in STAGE_CHOICES], ["general", "waiting_po", "payment_pending", "marketing"])
        self.assertNotIn("pi_sent", STAGE_KEYS)
        self.assertEqual(labels["payment_pending"], "Proforma invoice sent")

    def test_every_stage_has_four_emails(self):
        for key in STAGE_KEYS:
            bodies = {build_follow_up_body(key, reference="E1", subject="Gasket", attempt=n) for n in (1, 2, 3, 4)}
            self.assertEqual(len(bodies), 4, key)
            for body in bodies:
                self.assertTrue(body.startswith("Dear Sir/Madam,"))
                self.assertIn("AVIGIRI team", body)

    def test_old_sample_stage_falls_back_to_general(self):
        self.assertEqual(build_follow_up_body("sample_sent", reference="E1", subject="G"),
                         build_follow_up_body("general", reference="E1", subject="G"))


class StopRuleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("u", "u@example.com", "x")

    def _sent(self, n, stage="waiting_po"):
        for _ in range(n):
            FollowUpSent.objects.create(enquiry_id="E1", sent_by=self.user, status="sent", stage=stage)

    def test_converted_order_is_paid_so_no_follow_up(self):
        self.assertIn("converted", _follow_up_stopped_reason({"enquiry_id": "E1", "status": "converted"}, 0))

    @override_settings(FOLLOW_UP_MAX_COUNT=4)
    def test_accepted_unpaid_order_gets_payment_follow_ups_with_their_own_limit(self):
        self._sent(4)  # sales follow-ups do not use up the payment ones
        enquiry = {"enquiry_id": "E1", "status": "accepted"}
        self.assertEqual(_follow_up_stopped_reason(enquiry, 4), "")
        self._sent(4, stage="payment_pending")
        self.assertIn("followed up 4 times", _follow_up_stopped_reason(enquiry, 8))

    def test_paid_order_gets_no_follow_up(self):
        EnquiryContact.objects.create(enquiry_id="E1", payment_status=EnquiryContact.PAYMENT_PAID)
        self.assertIn("has been received", _follow_up_stopped_reason({"enquiry_id": "E1", "status": "accepted"}, 0))


@override_settings(**AUTO_ON)
class StatusEmailTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        self.gmail = _gmail()
        for target, value in (("enquiries.status_emails.GmailService", MagicMock(return_value=self.gmail)),):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("enquiries.views._resolve_follow_up_conversation", return_value=_conversation())
        self.resolve = p.start()
        self.addCleanup(p.stop)

    def test_rejection_email_asks_for_the_reason_once(self):
        outcome = status_emails.send_status_email("rejection", "E1", ENQUIRY)
        self.assertEqual(outcome["result"], "sent")
        kwargs = self.gmail.send_reply.call_args.kwargs
        self.assertEqual(kwargs["to"], "buyer@acme.example")
        self.assertIn("could you please let us know the main reason", kwargs["body"])
        self.assertTrue(kwargs["body"].startswith("Dear Sir/Madam,"))
        row = CustomerStatusEmail.objects.get()
        self.assertEqual((row.status, row.customer_email, row.gmail_message_id), ("sent", "buyer@acme.example", "sent-1"))
        # Changing the status again never emails twice.
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "already_sent")
        self.assertEqual(self.gmail.send_reply.call_count, 1)

    def test_reason_already_given_sends_a_thank_you(self):
        status_emails.send_status_email("rejection", "E1", ENQUIRY, reason="Price too high")
        body = self.gmail.send_reply.call_args.kwargs["body"]
        self.assertIn("for sharing your reason", body)
        self.assertNotIn("main reason?", body)

    @override_settings(FOLLOW_UP_AUTO_ENABLED=False)
    def test_turned_off_sends_nothing(self):
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "skipped")
        self.gmail.send_reply.assert_not_called()

    @override_settings(FOLLOW_UP_AUTO_DRY_RUN=True)
    def test_test_mode_sends_nothing_and_can_send_later(self):
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "dry_run")
        self.gmail.send_reply.assert_not_called()
        with self.settings(FOLLOW_UP_AUTO_DRY_RUN=False):
            self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "sent")

    def test_unconfirmed_conversation_is_not_guessed(self):
        self.resolve.return_value = ("choose", [{"thread_id": "a"}, {"thread_id": "b"}])
        outcome = status_emails.send_status_email("rejection", "E1", ENQUIRY)
        self.assertEqual(outcome["result"], "skipped")
        self.assertIn("email the customer manually", outcome["note"])
        self.gmail.send_reply.assert_not_called()

    def test_never_emails_our_own_address(self):
        self.resolve.return_value = _conversation("sales@ourcompany.example")
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "skipped")
        self.gmail.send_reply.assert_not_called()

    def test_gmail_failure_is_recorded_not_raised(self):
        self.gmail.send_reply.side_effect = RuntimeError("boom")
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "failed")
        self.assertIn("boom", CustomerStatusEmail.objects.get().note)


@override_settings(**AUTO_ON)
class ShareLinkTests(TestCase):
    def setUp(self):
        self.token = signing.dumps({"enquiry_id": "E1"}, salt="enquiry-share")
        self.update = patch("enquiries.views.services.update_enquiry").start()
        patch("enquiries.views.services.fetch_enquiry", return_value={**ENQUIRY, "status": "open", "remarks": "note"}).start()
        self.send = patch("enquiries.views.status_emails.send_status_email", return_value={"result": "sent"}).start()
        self.addCleanup(patch.stopall)

    def _post(self, action, reason=""):
        return self.client.post(reverse("enquiries:share_action"), {"token": self.token, "action": action, "reason": reason})

    def test_reject_sends_rejection_email_with_the_reason(self):
        self.assertEqual(self._post("reject", "Price too high").status_code, 200)
        self.assertEqual(self.update.call_args.args[2]["status"], "rejected")
        self.assertEqual(self.send.call_args.args[0], "rejection")
        self.assertEqual(self.send.call_args.kwargs["reason"], "Price too high")

    def test_request_changes_is_no_longer_accepted(self):
        self.assertEqual(self._post("request_changes", "30 days").status_code, 400)
        self.update.assert_not_called()
        self.send.assert_not_called()

    def test_accept_sets_accepted_and_sends_no_email(self):
        self._post("accept")
        self.assertEqual(self.update.call_args.args[2]["status"], "accepted")
        self.send.assert_not_called()


@override_settings(**AUTO_ON)
class BoardTrackingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("boss", "b@example.com", "x")
        self.client.force_login(self.user)
        self.rows = [
            {"enquiry_id": "C1", "status": "accepted", "subject": "S1", "customer_name": "A", "received_at_dt": datetime(2026, 5, 1)},
            {"enquiry_id": "C2", "status": "accepted", "subject": "S2", "customer_name": "B", "received_at_dt": datetime(2026, 5, 2)},
            {"enquiry_id": "O1", "status": "open", "subject": "S3", "customer_name": "C", "received_at_dt": datetime(2026, 5, 3)},
        ]
        patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in self.rows]).start()
        self.statuses = {r["enquiry_id"]: r["status"] for r in self.rows}
        patch("enquiries.views.services.fetch_enquiry",
              side_effect=lambda root, eid, **k: {**ENQUIRY, "enquiry_id": eid, "status": self.statuses[eid]}).start()

        def update(root, eid, payload, **k):
            if "status" in payload:
                self.statuses[eid] = payload["status"]
                for row in self.rows:
                    if row["enquiry_id"] == eid:
                        row["status"] = payload["status"]
            return {}
        self.update = patch("enquiries.views.services.update_enquiry", side_effect=update).start()
        self.send = patch("enquiries.views.status_emails.send_status_email", return_value={"result": "sent", "note": ""}).start()
        self.addCleanup(patch.stopall)

    def _post(self, **data):
        return self.client.post(reverse("enquiries:board"), data)

    def test_paid_converts_the_order_and_filters_work(self):
        self._post(action="update_payment", enquiry_id="C1", payment_status="paid")
        self._post(action="update_payment", enquiry_id="C2", payment_status="partial")
        contact = EnquiryContact.objects.get(enquiry_id="C1")
        self.assertEqual(contact.payment_status, "paid")
        self.assertEqual(contact.payment_updated_by, self.user)
        self.assertEqual(self.statuses["C1"], "converted")  # paid -> converted
        paid_calls = [c for c in self.send.call_args_list if c.args[0] == "payment_received"]
        self.assertEqual([c.args[1] for c in paid_calls], ["C1"])  # thank-you email for the paid order only
        self.assertEqual(self.statuses["C2"], "accepted")   # partly paid stays accepted

        unpaid = self.client.get(reverse("enquiries:board"), {"payment": "unpaid"})
        self.assertEqual([e["enquiry_id"] for e in unpaid.context["enquiries"]], ["C2"])
        paid = self.client.get(reverse("enquiries:board"), {"payment": "paid"})
        self.assertEqual([e["enquiry_id"] for e in paid.context["enquiries"]], ["C1"])

    def test_accepted_order_without_payment_shows_pending(self):
        response = self.client.get(reverse("enquiries:board"))
        rows = {e["enquiry_id"]: e for e in response.context["enquiries"]}
        self.assertEqual(rows["C1"]["payment"]["status"], "pending")
        self.assertIsNone(rows["O1"]["payment"])

    def test_payment_only_for_accepted_orders(self):
        self._post(action="update_payment", enquiry_id="O1", payment_status="paid")
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="O1").exists())
        self.assertEqual(self.statuses["O1"], "open")

    def test_switch_cannot_convert_an_unpaid_order(self):
        url = reverse("enquiries:toggle_converted", args=["C1"])
        response = self.client.post(url, {"converted": "true", "previous_status": "accepted"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Mark as paid", response.json()["error"])
        self.assertEqual(self.statuses["C1"], "accepted")

    def test_undoing_a_conversion_sets_payment_back_to_pending(self):
        self._post(action="update_payment", enquiry_id="C1", payment_status="paid")
        url = reverse("enquiries:toggle_converted", args=["C1"])
        self.client.post(url, {"converted": "false", "previous_status": "accepted"})
        self.assertEqual(self.statuses["C1"], "accepted")
        contact = EnquiryContact.objects.get(enquiry_id="C1")
        self.assertEqual(contact.payment_status, "pending")
        self.assertIsNone(contact.payment_paid_on)

    def _store_confidential(self):
        return EnquiryContact.objects.create(
            enquiry_id="C1", payment_status="partial", payment_amount_received="1500.50",
            payment_due_on=timezone.localdate() - timedelta(days=2), payment_paid_on=date(2026, 11, 1),
            payment_reference="UTR987654")

    def test_the_payment_form_has_only_the_status_dropdown(self):
        self._store_confidential()
        page = self.client.get(reverse("enquiries:board"))
        html = page.content.decode()
        for gone in ("Amount received", "Payment due on", "Paid on (when Paid)", "Payment reference", "UTR / cheque",
                     "This is the only way to convert an order", "payment_amount_received", "payment_due_on",
                     "payment_paid_on", "payment_reference"):
            self.assertNotIn(gone, html)
        self.assertContains(page, ">Payment Status</label>")
        labels = re.findall(r'<option value="(pending|partial|credit|paid)"[^>]*>([^<]+)</option>',
                            html.split('name="payment_status"', 1)[1])[:4]
        self.assertEqual(labels, [("pending", "Payment Pending"), ("partial", "Partially Paid"),
                                  ("credit", "Credit Period"), ("paid", "Paid")])
        self.assertContains(page, "Mark this order as Paid and Converted?")

    def test_the_confidential_values_are_never_shown_or_sent_to_the_page(self):
        self._store_confidential()
        page = self.client.get(reverse("enquiries:board"))
        html = page.content.decode()
        for secret in ("1500.50", "UTR987654", "01 Nov 2026"):
            self.assertNotIn(secret, html)
        payment = {e["enquiry_id"]: e for e in page.context["enquiries"]}["C1"]["payment"]
        for key in ("amount", "paid_on", "reference"):
            self.assertNotIn(key, payment)
        self.assertIsNone(payment["due"])  # the entered due date is not sent (only a credit period's end is)
        self.assertEqual(payment["label"], "Partially Paid")

    def test_saving_the_status_keeps_the_stored_confidential_data(self):
        self._store_confidential()
        self._post(action="update_payment", enquiry_id="C1", payment_status="paid",
                   payment_amount_received="1", payment_reference="ignored", payment_paid_on="2020-01-01")
        contact = EnquiryContact.objects.get(enquiry_id="C1")
        self.assertEqual(contact.payment_status, "paid")
        self.assertEqual((str(contact.payment_amount_received), contact.payment_reference, contact.payment_paid_on),
                         ("1500.50", "UTR987654", date(2026, 11, 1)))  # posted values are ignored, nothing wiped
        self.assertEqual(self.statuses["C1"], "converted")

    def test_paid_needs_nothing_but_the_status_and_the_email_carries_no_payment_detail(self):
        self._store_confidential()
        self._post(action="update_payment", enquiry_id="C1", payment_status="paid")
        self.assertEqual(self.statuses["C1"], "converted")
        call = [c for c in self.send.call_args_list if c.args[0] == "payment_received"][0]
        self.assertNotIn("payment_detail", call.kwargs)

    def test_partial_pending_and_credit_leave_the_order_unconverted(self):
        for value, extra in (("pending", {}), ("partial", {}), ("credit", {"credit_period_days": "30"})):
            self._post(action="update_payment", enquiry_id="C1", payment_status=value, **extra)
            self.assertEqual(self.statuses["C1"], "accepted")
            self.assertEqual(EnquiryContact.objects.get(enquiry_id="C1").payment_status, value)

    def test_conversion_rule(self):
        from enquiries.views import _conversion_block_reason

        self.assertIn("payment is received", _conversion_block_reason("C1", "accepted", "converted"))
        self.assertIn("payment is received", _conversion_block_reason("O1", "open", "converted"))
        self.assertEqual(_conversion_block_reason("C1", "converted", "converted"), "")
        self.assertEqual(_conversion_block_reason("C1", "open", "accepted"), "")
        EnquiryContact.objects.create(enquiry_id="C1", payment_status="paid")
        self.assertEqual(_conversion_block_reason("C1", "accepted", "converted"), "")

    def test_bad_payment_values_are_rejected(self):
        self._post(action="update_payment", enquiry_id="C1", payment_status="lots")
        for bad in ("PAID", "Paid ", "payment_pending", "", "converted"):
            self._post(action="update_payment", enquiry_id="C1", payment_status=bad)
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="C1").exists())
        self.assertEqual(self.statuses["C1"], "accepted")

    def test_skipped_email_is_shown_with_retry(self):
        self.rows[2]["status"] = "rejected"
        CustomerStatusEmail.objects.create(enquiry_id="O1", kind="rejection", event_key="rejection",
                                           status="skipped", note="no confirmed customer email address")
        response = self.client.get(reverse("enquiries:board"))
        self.assertContains(response, 'data-status-email-state="skipped"')
        self.assertContains(response, "no confirmed customer email address")
        self.assertContains(response, 'value="retry_status_email"')


@override_settings(**AUTO_ON)
class AfterStatusChangeTests(TestCase):
    def test_staff_rejecting_sends_email_only_on_the_change(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from enquiries.views import _after_status_change

        request = RequestFactory().post("/")
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch("enquiries.views.status_emails.send_status_email", return_value={"result": "sent"}) as send:
            _after_status_change(request, "E1", {**ENQUIRY, "status": "open", "remarks": "internal note"}, "rejected")
            self.assertEqual(send.call_args.kwargs["reason"], "")  # internal remarks are not the customer's reason
            _after_status_change(request, "E1", {**ENQUIRY, "status": "rejected"}, "rejected")
            _after_status_change(request, "E1", {**ENQUIRY, "status": "open"}, "accepted")
        self.assertEqual(send.call_count, 1)


@override_settings(**AUTO_ON, FOLLOW_UP_MIN_GAP_HOURS=0)  # the rows below were "sent" moments ago
class AutoFollowUpOnConvertedTests(TestCase):
    def test_quote_follow_ups_stop_when_order_is_accepted(self):
        sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        latest = FollowUpSent.objects.create(enquiry_id="E1", customer_email="b@acme.example", sent_by=sales,
                                             status="sent", gmail_thread_id="t1", stage="waiting_po",
                                             next_follow_up_on=timezone.localdate())
        accepted = {"enquiry_id": "E1", "status": "accepted"}
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        reason = auto_follow_up._skip_reason("E1", accepted, cycle, timezone.localdate())
        self.assertIn("only while it is Open", reason)  # accepted enquiries get no automatic follow-ups
        latest.stage = "payment_pending"
        latest.save()
        cycle = auto_follow_up.cycles(["E1"])["E1"]
        self.assertIn("only while it is Open", auto_follow_up._skip_reason("E1", accepted, cycle, timezone.localdate()))
        open_enquiry = {"enquiry_id": "E1", "status": "open"}
        self.assertEqual(auto_follow_up._skip_reason("E1", open_enquiry, cycle, timezone.localdate()), "")


@override_settings(**AUTO_ON, FOLLOW_UP_CLOSE_AFTER_DAYS=7)
class CustomerEmailTests(TestCase):
    """Payment received and closing emails (there is no acknowledgement email any more)."""

    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        self.gmail = _gmail()
        self.gmail.get_thread_messages.return_value = [{"id": "m1", "sender": "Buyer <buyer@acme.example>"}]
        patch("enquiries.status_emails.GmailService", return_value=self.gmail).start()
        self.resolve = patch("enquiries.views._resolve_follow_up_conversation", return_value=_conversation()).start()
        self.update = patch("enquiries.services.update_enquiry").start()
        self.addCleanup(patch.stopall)
        self.today = timezone.localdate()

    def _enq(self, eid, status="open", days_ago=0):
        return {"enquiry_id": eid, "status": status, "subject": "Gasket", "customer_name": "Acme",
                "received_at_dt": datetime.combine(self.today - timedelta(days=days_ago), datetime.min.time())}

    def _followed_up(self, eid, n=4, days_ago=8, stage="waiting_po"):
        for i in range(n):
            FollowUpSent.objects.create(enquiry_id=eid, sent_by=self.sales, status="sent", stage=stage,
                                        sent_at=timezone.now() - timedelta(days=days_ago + (n - i)))

    # --- no acknowledgement ----------------------------------------------------------------
    def test_there_is_no_acknowledgement_email(self):
        self.assertFalse(hasattr(status_emails, "acknowledgement_due"))
        outcome = status_emails.send_status_email("acknowledgement", "NEW", self._enq("NEW"))
        self.assertEqual(outcome["result"], "skipped")
        self.gmail.send_reply.assert_not_called()

    # --- closing ---------------------------------------------------------------------------
    def test_customer_reply_stops_the_closing_email(self):
        outcome = status_emails.send_closing_email(self._enq("DONE"), "http://api")  # customer wrote last
        self.assertEqual(outcome["result"], "skipped")
        self.gmail.send_reply.assert_not_called()
        self.update.assert_not_called()

    # --- payment received ------------------------------------------------------------------
    def test_payment_received_mentions_reference_and_date(self):
        from datetime import date

        detail = status_emails.payment_detail_text(date(2026, 11, 1), "UTR123")
        status_emails.send_status_email("payment_received", "C1", self._enq("C1", status="converted"), payment_detail=detail)
        body = self.gmail.send_reply.call_args.kwargs["body"]
        self.assertIn("(payment reference: UTR123, received on 01 Nov 2026)", body)
        self.assertIn("confirmed for production", body)

    # --- scheduled step --------------------------------------------------------------------
class EmailWordingTests(TestCase):
    """Every automatic email is signed by the team and ends with a line that fits its purpose."""

    def test_no_personal_name_in_any_email(self):
        user = get_user_model().objects.create_user("ananya", "a@example.com", "x", first_name="Ananya", last_name="Y A")
        bodies = [build_follow_up_body(k, reference="E1", subject="Gasket", user=user, attempt=n)
                  for k in STAGE_KEYS for n in (1, 2, 3, 4)]
        bodies += [status_emails.build_status_email_body(t, reference="E1", subject="Gasket", user=user)
                   for t in ("rejection_ask", "rejection_thanks", "payment_received")]
        bodies.append(status_emails.enquiry_closing_body())
        for body in bodies:
            self.assertNotIn("Ananya", body)
            self.assertRegex(body, r"AVIGIRI [Tt]eam")

    def test_enquiry_follow_ups_are_signed_avigiri_team_and_have_no_unfilled_placeholder(self):
        for n in (1, 2, 3, 4):
            body = build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=n,
                                        share_url="https://x.example/s/1/", share_password="AG-pw",
                                        customer_name="Acme")
            self.assertTrue(body.startswith("Dear Acme,"))
            self.assertTrue(body.rstrip().endswith("AVIGIRI team"), body[-40:])
            self.assertNotRegex(body, r"[{}]")
            self.assertIn("https://x.example/s/1/", body)
            self.assertIn("Password: AG-pw", body)

    def test_a_sentence_without_its_value_is_left_out(self):
        body = build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=3,
                                    share_url="https://x/", share_password="P")
        self.assertNotIn("timeline", body)
        self.assertNotIn("reach us at", body)
        with override_settings(FOLLOW_UP_CONTACT_PHONE="0816 2970025"):
            body = build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=3,
                                        share_url="https://x/", share_password="P",
                                        delivery_timeline="3 weeks", confirm_by_date="30 Nov 2026")
        self.assertIn("timeline of 3 weeks", body)
        self.assertIn("confirmation by 30 Nov 2026", body)
        self.assertIn("reach us at 0816 2970025", body)


class PaymentStatusMigrationTests(TestCase):
    def test_existing_records_become_pending_or_paid(self):
        import importlib
        from django.apps import apps as live_apps

        migration = importlib.import_module("enquiries.migrations.0016_payment_status_only")
        blank = EnquiryContact.objects.create(enquiry_id="M1", payment_status="")
        paid = EnquiryContact.objects.create(enquiry_id="M2", payment_status="", payment_paid_on=date(2026, 11, 1),
                                             payment_reference="UTR1")
        credit = EnquiryContact.objects.create(enquiry_id="M3", payment_status="credit", credit_period_days=30)
        migration.set_payment_status(live_apps, None)
        statuses = {c.enquiry_id: c.payment_status for c in EnquiryContact.objects.all()}
        self.assertEqual(statuses, {"M1": "pending", "M2": "paid", "M3": "credit"})
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="M2").payment_reference, "UTR1")  # nothing dropped

    def test_the_default_is_payment_pending(self):
        self.assertEqual(EnquiryContact.objects.create(enquiry_id="M4").payment_status, "pending")

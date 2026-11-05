"""Tests for the enquiries board FY / Month / Status filters and the manual follow-up flow."""
import base64
from datetime import datetime
from email import message_from_bytes
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.gmail_service import GmailAPIError, GmailService
from enquiries.models import FollowUpSent


def _enq(enquiry_id, status, received):
    return {
        "enquiry_id": enquiry_id,
        "status": status,
        "subject": f"Subject {enquiry_id}",
        "customer_name": f"Customer {enquiry_id}",
        "received_at_dt": received,
    }


def _dataset():
    return [
        _enq("A", "open", datetime(2024, 3, 31, 12, 0)),      # FY 2023-24, March
        _enq("B", "closed", datetime(2024, 4, 1, 0, 0)),      # FY 2024-25, April (first day)
        _enq("C", "open", datetime(2025, 3, 31, 23, 59)),     # FY 2024-25, March (last day)
        _enq("D", "accepted", datetime(2025, 4, 1, 9, 0)),    # FY 2025-26, April
        _enq("E", "rejected", datetime(2024, 11, 15, 10, 0)), # FY 2024-25, November
        _enq("F", "open", None),                              # no date
        _enq("G", "OPEN ", datetime(2024, 6, 10, 10, 0)),     # FY 2024-25, June, odd casing
    ]


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class BoardFilterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("filtertester", "f@example.com", "secret123")
        self.client.force_login(self.user)
        patcher = patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: _dataset())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ids(self, **params):
        response = self.client.get(reverse("enquiries:board"), params)
        self.assertEqual(response.status_code, 200)
        return sorted(e["enquiry_id"] for e in response.context["enquiries"]), response

    # --- each filter on its own ---
    def test_no_filters_returns_everything(self):
        ids, _ = self._ids()
        self.assertEqual(ids, ["A", "B", "C", "D", "E", "F", "G"])

    def test_empty_values_mean_all(self):
        ids, _ = self._ids(fin_year="", month="", status="")
        self.assertEqual(ids, ["A", "B", "C", "D", "E", "F", "G"])

    def test_fy_filter_uses_april_to_march_boundaries(self):
        ids, _ = self._ids(fin_year="2024-25")
        self.assertEqual(ids, ["B", "C", "E", "G"])

    def test_fy_options_cover_dataset(self):
        _, response = self._ids()
        self.assertEqual(response.context["fin_years"], ["2023-24", "2024-25", "2025-26"])

    def test_month_filter_alone_spans_years(self):
        ids, _ = self._ids(month="3")
        self.assertEqual(ids, ["A", "C"])

    def test_status_filter(self):
        ids, _ = self._ids(status="open")
        # G has status "OPEN " which is normalised by strip().lower()
        self.assertEqual(ids, ["A", "C", "F", "G"])

    def test_invalid_values_are_ignored(self):
        ids, _ = self._ids(status="bogus", month="13", fin_year="garbage")
        # month/fin_year invalid -> no date restriction, but undated rows are dropped
        self.assertEqual(ids, ["A", "B", "C", "D", "E", "G"])

    # --- combinations ---
    def test_fy_and_month(self):
        ids, _ = self._ids(fin_year="2024-25", month="3")
        self.assertEqual(ids, ["C"])  # March 2025, not March 2024
        ids, _ = self._ids(fin_year="2024-25", month="4")
        self.assertEqual(ids, ["B"])

    def test_fy_and_status(self):
        ids, _ = self._ids(fin_year="2024-25", status="open")
        self.assertEqual(ids, ["C", "G"])

    def test_all_three(self):
        ids, _ = self._ids(fin_year="2024-25", month="11", status="rejected")
        self.assertEqual(ids, ["E"])
        ids, _ = self._ids(fin_year="2024-25", month="11", status="open")
        self.assertEqual(ids, [])

    # --- persistence ---
    def test_selected_values_rendered_back_into_selects(self):
        _, response = self._ids(fin_year="2024-25", month="3", status="open")
        self.assertContains(response, '<option value="2024-25" selected>2024-25</option>', html=True)
        self.assertContains(response, '<option value="3" selected>March</option>', html=True)
        self.assertContains(response, '<option value="open" selected>Open</option>', html=True)

    def test_pagination_links_keep_filters(self):
        many = [_enq(f"X{i:02d}", "open", datetime(2024, 5, 1 + i % 28)) for i in range(30)]
        with patch("enquiries.views.services.list_enquiries", return_value=many):
            response = self.client.get(
                reverse("enquiries:board"),
                {"fin_year": "2024-25", "month": "5", "status": "open", "per_page": 25},
            )
        self.assertEqual(response.context["enquiries_total_count"], 30)
        next_qs = parse_qs(urlparse(response.context["enquiries_next_url"]).query)
        self.assertEqual(next_qs["fin_year"], ["2024-25"])
        self.assertEqual(next_qs["month"], ["5"])
        self.assertEqual(next_qs["status"], ["open"])
        self.assertEqual(next_qs["page"], ["2"])


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class FollowUpFlowTests(TestCase):
    ENQUIRY = {
        "enquiry_id": "ENQ-7",
        "subject": "Gasket quote",
        "customer_name": "Acme",
        "customer_email": "buyer@acme.example",
    }
    MESSAGE = {
        "id": "gm-1",
        "thread_id": "th-1",
        "subject": "Gasket quote",
        "sender": "buyer@acme.example",
        "message_id_header": "<abc@acme.example>",
    }

    def setUp(self):
        self.user = get_user_model().objects.create_user("futester", "fu@example.com", "secret123")
        self.client.force_login(self.user)
        for target in ("dashboard.views._ensure_local_demo_emails",):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)

    def _start_follow_up(self):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(self.ENQUIRY)), \
             patch("enquiries.views.GmailService") as gs:
            gs.return_value.search_messages.return_value = [dict(self.MESSAGE)]
            return self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]))

    def _compose_service(self, message=None):
        svc = MagicMock()
        msg = dict(message or self.MESSAGE)
        svc.get_message.return_value = msg
        svc.get_thread_messages.return_value = [msg]
        return svc

    def test_follow_up_stores_context_and_redirects_without_sending(self):
        response = self._start_follow_up()
        self.assertRedirects(response, reverse("dashboard:compose_email", args=["gm-1"]),
                             fetch_redirect_response=False)
        ctx = self.client.session["follow_up_context"]
        self.assertEqual(ctx["enquiry_id"], "ENQ-7")
        self.assertEqual(ctx["customer_email"], "buyer@acme.example")
        self.assertEqual(FollowUpSent.objects.count(), 0)

    def test_compose_get_shows_banner_and_does_not_send(self):
        self._start_follow_up()
        svc = self._compose_service()
        with patch("dashboard.views.GmailService", return_value=svc):
            response = self.client.get(reverse("dashboard:compose_email", args=["gm-1"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual follow-up")
        self.assertContains(response, "buyer@acme.example")
        svc.send_reply.assert_not_called()
        self.assertEqual(FollowUpSent.objects.count(), 0)

    def test_compose_post_sends_and_records(self):
        self._start_follow_up()
        svc = self._compose_service()
        with patch("dashboard.views.GmailService", return_value=svc):
            response = self.client.post(reverse("dashboard:compose_email", args=["gm-1"]),
                                        {"subject": "Re: Gasket quote", "body": "Following up"})
        self.assertEqual(response.status_code, 302)
        svc.send_reply.assert_called_once()
        kwargs = svc.send_reply.call_args.kwargs
        self.assertEqual(kwargs["body"], "Following up")
        self.assertEqual(kwargs["subject"], "Re: Gasket quote")
        self.assertEqual(kwargs["to"], "buyer@acme.example")
        record = FollowUpSent.objects.get()
        self.assertEqual(record.enquiry_id, "ENQ-7")
        self.assertEqual(record.customer_email, "buyer@acme.example")
        self.assertEqual(record.body, "Following up")
        self.assertEqual(record.sent_by, self.user)
        self.assertNotIn("follow_up_context", self.client.session)

    def _start_follow_up_with(self, enquiry_overrides, message_overrides):
        enquiry = {**self.ENQUIRY, **enquiry_overrides}
        message = {**self.MESSAGE, **message_overrides}
        with patch("enquiries.views.services.fetch_enquiry", return_value=enquiry), \
             patch("enquiries.views.GmailService") as gs:
            gs.return_value.search_messages.return_value = [message]
            self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]))
        return message

    def test_customer_email_used_even_when_matched_message_is_our_own(self):
        """The newest matching Gmail message can be our own sent reply; the customer must still get it."""
        message = self._start_follow_up_with({}, {"sender": "sales@ourcompany.example"})
        svc = self._compose_service(message)
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-1"]))
            self.assertContains(get_resp, "buyer@acme.example")
            self.assertNotContains(get_resp, "data-follow-up-fallback-warning")
            self.client.post(reverse("dashboard:compose_email", args=["gm-1"]),
                             {"subject": "Re: Gasket quote", "body": "Following up"})
        self.assertEqual(svc.send_reply.call_args.kwargs["to"], "buyer@acme.example")
        self.assertEqual(FollowUpSent.objects.get().customer_email, "buyer@acme.example")

    def test_missing_customer_email_warns_and_falls_back_to_gmail_sender(self):
        message = self._start_follow_up_with({"customer_email": ""}, {"sender": "contact@acme.example"})
        svc = self._compose_service(message)
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-1"]))
            self.assertContains(get_resp, "data-follow-up-fallback-warning")
            self.assertContains(get_resp, "contact@acme.example")
            svc.send_reply.assert_not_called()
            self.client.post(reverse("dashboard:compose_email", args=["gm-1"]),
                             {"subject": "Re: Gasket quote", "body": "Following up"})
        self.assertEqual(svc.send_reply.call_args.kwargs["to"], "contact@acme.example")
        self.assertEqual(FollowUpSent.objects.get().customer_email, "contact@acme.example")

    def test_invalid_customer_email_treated_as_missing(self):
        message = self._start_follow_up_with({"customer_email": "not-an-email"}, {"sender": "contact@acme.example"})
        svc = self._compose_service(message)
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-1"]))
        self.assertContains(get_resp, "data-follow-up-fallback-warning")

    def test_plain_reply_without_follow_up_keeps_default_recipient(self):
        svc = self._compose_service()
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-1"]))
            self.assertNotContains(get_resp, "Manual follow-up")
            self.client.post(reverse("dashboard:compose_email", args=["gm-1"]),
                             {"subject": "Re: Gasket quote", "body": "Hi"})
        self.assertIsNone(svc.send_reply.call_args.kwargs["to"])
        self.assertEqual(FollowUpSent.objects.count(), 0)

    def test_send_failure_records_nothing_and_keeps_context(self):
        self._start_follow_up()
        svc = self._compose_service()
        svc.send_reply.side_effect = GmailAPIError("boom")
        with patch("dashboard.views.GmailService", return_value=svc):
            response = self.client.post(reverse("dashboard:compose_email", args=["gm-1"]),
                                        {"subject": "Re: Gasket quote", "body": "Following up"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FollowUpSent.objects.count(), 0)
        self.assertIn("follow_up_context", self.client.session)

    def test_follow_up_gmail_error_redirects_to_board(self):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(self.ENQUIRY)), \
             patch("enquiries.views.GmailService") as gs:
            gs.return_value.search_messages.side_effect = GmailAPIError("auth failed")
            response = self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]))
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)

    def test_stale_context_not_applied_to_unrelated_reply(self):
        """Abandoned follow-up must not tag a later, unrelated reply as a follow-up."""
        self._start_follow_up()  # context for gm-1 is now in the session
        other = {"id": "gm-OTHER", "thread_id": "th-9", "subject": "Unrelated",
                 "sender": "someone@else.example"}
        svc = self._compose_service(other)
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-OTHER"]))
            self.assertNotContains(get_resp, "Manual follow-up")
            self.client.post(reverse("dashboard:compose_email", args=["gm-OTHER"]),
                             {"subject": "Re: Unrelated", "body": "Hi"})
        self.assertEqual(FollowUpSent.objects.filter(enquiry_id="ENQ-7").count(), 0)


class SendReplyRecipientTests(TestCase):
    def _send(self, original, to=None):
        svc = GmailService.__new__(GmailService)
        svc.service = MagicMock()
        svc.send_reply(original_message=original, body="Hello", subject="Re: Gasket quote", to=to)
        send_call = svc.service.users.return_value.messages.return_value.send.call_args
        raw = send_call.kwargs["body"]["raw"]
        return message_from_bytes(base64.urlsafe_b64decode(raw))

    def test_reply_goes_to_original_sender(self):
        msg = self._send({"sender": "buyer@acme.example", "subject": "Gasket quote", "thread_id": "t"})
        self.assertEqual(msg["To"], "buyer@acme.example")
        self.assertEqual(msg["Subject"], "Re: Gasket quote")
        self.assertIn("Hello", msg.get_payload())

    def test_explicit_to_overrides_original_sender(self):
        msg = self._send({"sender": "sales@ourcompany.example", "subject": "Gasket quote", "thread_id": "t"},
                         to="buyer@acme.example")
        self.assertEqual(msg["To"], "buyer@acme.example")

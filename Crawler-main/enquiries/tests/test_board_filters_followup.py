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
    """Follow-up from the enquiries board through to sending, all with a mocked Gmail service."""

    ENQUIRY = {
        "enquiry_id": "ENQ-7",
        "subject": "Gasket quote",
        "customer_name": "Acme",
        "customer_reference": "EMAIL-orig-1",
    }
    CONVERSATION = {
        "thread_id": "th-1",
        "subject": "Gasket quote",
        "customer_email": "buyer@acme.example",
        "latest_message_id": "gm-1",
        "last_date": None,
        "message_count": 2,
        "snippet": "Please quote",
    }
    MESSAGE = {
        "id": "gm-1",
        "thread_id": "th-1",
        "subject": "Gasket quote",
        "sender": "sales@ourcompany.example",  # latest message in the thread is our own
        "message_id_header": "<abc@ourcompany.example>",
    }

    def setUp(self):
        self.user = get_user_model().objects.create_user("futester", "fu@example.com", "secret123")
        self.client.force_login(self.user)
        p = patch("dashboard.views._ensure_local_demo_emails")
        p.start()
        self.addCleanup(p.stop)

    # --- helpers ---
    def _gmail_for_follow_up(self, conversation=None, *, original_thread="th-1", candidates=None):
        gs = MagicMock()
        gs.get_message.return_value = {"id": "orig-1", "thread_id": original_thread} if original_thread else {}
        gs.describe_conversation.return_value = dict(conversation or self.CONVERSATION)
        gs.find_enquiry_conversations.return_value = candidates if candidates is not None else []
        return gs

    def _follow_up(self, enquiry=None, gs=None, **params):
        enquiry = dict(self.ENQUIRY if enquiry is None else enquiry)
        gs = gs or self._gmail_for_follow_up()
        with patch("enquiries.views.services.fetch_enquiry", return_value=enquiry), \
             patch("enquiries.views.GmailService", return_value=gs):
            return self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]), params)

    def _compose_service(self, message=None):
        svc = MagicMock()
        msg = dict(message or self.MESSAGE)
        svc.get_message.return_value = msg
        svc.get_thread_messages.return_value = [msg]
        svc.send_reply.return_value = {"id": "sent-1", "threadId": "th-1"}
        return svc

    def _compose(self, method="get", data=None, svc=None):
        svc = svc or self._compose_service()
        url = reverse("dashboard:compose_email", args=["gm-1"])
        with patch("dashboard.views.GmailService", return_value=svc):
            response = self.client.post(url, data) if method == "post" else self.client.get(url)
        return response, svc

    def _token(self):
        return self.client.session["follow_up_context"]["token"]

    # 1. valid enquiry with a matching conversation (original Gmail message saved on the enquiry)
    def test_email_created_enquiry_uses_its_original_conversation(self):
        gs = self._gmail_for_follow_up()
        response = self._follow_up(gs=gs)
        self.assertRedirects(response, reverse("dashboard:compose_email", args=["gm-1"]),
                             fetch_redirect_response=False)
        gs.get_message.assert_called_once_with("orig-1")
        gs.describe_conversation.assert_called_once_with("th-1")
        gs.find_enquiry_conversations.assert_not_called()  # no subject search needed
        ctx = self.client.session["follow_up_context"]
        self.assertEqual(ctx["customer_email"], "buyer@acme.example")
        self.assertEqual(ctx["gmail_thread_id"], "th-1")
        self.assertEqual(FollowUpSent.objects.count(), 0)

    def test_previous_sent_follow_up_thread_is_reused(self):
        FollowUpSent.objects.create(enquiry_id="ENQ-7", customer_email="buyer@acme.example",
                                    gmail_thread_id="th-1", sent_by=self.user, status="sent")
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        gs = self._gmail_for_follow_up()
        response = self._follow_up(enquiry=enquiry, gs=gs)
        self.assertEqual(response.status_code, 302)
        gs.describe_conversation.assert_called_once_with("th-1")
        gs.find_enquiry_conversations.assert_not_called()

    # 2. no customer email can be identified
    def test_conversation_without_customer_address_is_refused(self):
        gs = self._gmail_for_follow_up({**self.CONVERSATION, "customer_email": ""})
        response = self._follow_up(gs=gs)
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        self.assertNotIn("follow_up_context", self.client.session)

    # 3. no matching conversation
    def test_no_matching_conversation_shows_error(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        gs = self._gmail_for_follow_up(candidates=[])
        response = self._follow_up(enquiry=enquiry, gs=gs)
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        self.assertNotIn("follow_up_context", self.client.session)

    # 4. multiple / subject-only matches need the employee to choose
    def test_multiple_conversations_show_selection_page(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        other = {**self.CONVERSATION, "thread_id": "th-2", "customer_email": "other@else.example",
                 "latest_message_id": "gm-2"}
        gs = self._gmail_for_follow_up(candidates=[dict(self.CONVERSATION), other])
        response = self._follow_up(enquiry=enquiry, gs=gs)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/follow_up_select.html")
        self.assertContains(response, 'data-follow-up-candidate="th-1"')
        self.assertContains(response, 'data-follow-up-candidate="th-2"')
        self.assertNotIn("follow_up_context", self.client.session)

    def test_single_subject_only_match_still_needs_confirmation(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        gs = self._gmail_for_follow_up(candidates=[dict(self.CONVERSATION)])
        response = self._follow_up(enquiry=enquiry, gs=gs)
        self.assertTemplateUsed(response, "enquiries/follow_up_select.html")

    def test_choosing_a_conversation_continues_to_compose(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        other = {**self.CONVERSATION, "thread_id": "th-2", "latest_message_id": "gm-2"}
        gs = self._gmail_for_follow_up(candidates=[dict(self.CONVERSATION), other])
        response = self._follow_up(enquiry=enquiry, gs=gs, thread="th-2")
        self.assertRedirects(response, reverse("dashboard:compose_email", args=["gm-2"]),
                             fetch_redirect_response=False)
        self.assertEqual(self.client.session["follow_up_context"]["gmail_thread_id"], "th-2")

    def test_choosing_a_conversation_not_in_results_is_refused(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        gs = self._gmail_for_follow_up(candidates=[dict(self.CONVERSATION)])
        response = self._follow_up(enquiry=enquiry, gs=gs, thread="th-evil")
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        self.assertNotIn("follow_up_context", self.client.session)

    def test_invalid_thread_parameter_rejected(self):
        response = self._follow_up(thread="bad id!")
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)

    # compose page: prefilled, editable, never sends on load
    def test_compose_prefills_and_does_not_send_on_load(self):
        self._follow_up()
        response, svc = self._compose()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manual follow-up")
        self.assertContains(response, "buyer@acme.example")
        self.assertContains(response, "Dear Acme,")
        self.assertContains(response, "Just following up on the quotation")
        self.assertContains(response, "AVIGIRI team")
        self.assertNotContains(response, "MagicWand Team")
        self.assertContains(response, "we shared for Gasket quote")
        self.assertContains(response, "Re: Gasket quote")
        svc.send_reply.assert_not_called()

    # 5 + 9. successful send goes to the customer in the original thread and is recorded
    def test_send_goes_to_customer_and_records_history(self):
        self._follow_up()
        response, svc = self._compose("post", {"subject": "Re: Gasket quote", "body": "Edited text",
                                               "follow_up_token": self._token()})
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        kwargs = svc.send_reply.call_args.kwargs
        self.assertEqual(kwargs["to"], "buyer@acme.example")  # not the latest sender (ourselves)
        self.assertEqual(kwargs["body"], "Edited text")
        self.assertEqual(kwargs["original_message"]["thread_id"], "th-1")
        record = FollowUpSent.objects.get()
        self.assertEqual(record.status, "sent")
        self.assertEqual(record.enquiry_id, "ENQ-7")
        self.assertEqual(record.customer_email, "buyer@acme.example")
        self.assertEqual(record.gmail_thread_id, "th-1")
        self.assertEqual(record.gmail_message_id, "sent-1")
        self.assertEqual(record.body, "Edited text")
        self.assertEqual(record.sent_by, self.user)
        self.assertNotIn("follow_up_context", self.client.session)

    # 6. Gmail failure is recorded as Failed and can be retried
    def test_gmail_failure_recorded_and_retry_allowed(self):
        self._follow_up()
        svc = self._compose_service()
        svc.send_reply.side_effect = GmailAPIError("Unable to send reply")
        response, _ = self._compose("post", {"subject": "Re: Gasket quote", "body": "Hi",
                                             "follow_up_token": self._token()}, svc=svc)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Follow-up was not sent")
        record = FollowUpSent.objects.get()
        self.assertEqual(record.status, "failed")
        self.assertIn("Unable to send reply", record.error_message)
        # retry with the new token succeeds
        response, svc2 = self._compose("post", {"subject": "Re: Gasket quote", "body": "Hi",
                                                "follow_up_token": self._token()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FollowUpSent.objects.filter(status="sent").count(), 1)

    def test_network_failure_recorded_as_failed(self):
        self._follow_up()
        svc = self._compose_service()
        svc.send_reply.side_effect = ConnectionError("timeout")
        response, _ = self._compose("post", {"subject": "Re: x", "body": "Hi",
                                             "follow_up_token": self._token()}, svc=svc)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FollowUpSent.objects.get().status, "failed")

    def test_gmail_error_while_matching_redirects_with_message(self):
        enquiry = {k: v for k, v in self.ENQUIRY.items() if k != "customer_reference"}
        gs = self._gmail_for_follow_up()
        gs.find_enquiry_conversations.side_effect = GmailAPIError("Unable to search Gmail right now.")
        response = self._follow_up(enquiry=enquiry, gs=gs)
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)

    # 7. unauthorised access
    def test_anonymous_user_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"]))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(reverse("dashboard:compose_email", args=["gm-1"]), response["Location"])

    def test_user_without_enquiries_access_is_blocked(self):
        from dashboard.models import UserServiceAccess
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["mrp"])
        gs = self._gmail_for_follow_up()
        response = self._follow_up(gs=gs)
        self.assertEqual(response.status_code, 302)
        gs.describe_conversation.assert_not_called()
        self.assertNotIn("follow_up_context", self.client.session)

    # 8. duplicate submission
    def test_duplicate_submission_does_not_send_twice(self):
        self._follow_up()
        token = self._token()
        data = {"subject": "Re: Gasket quote", "body": "Hi", "follow_up_token": token}
        self._compose("post", data)
        response, svc = self._compose("post", data)  # same form submitted again
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        svc.send_reply.assert_not_called()
        self.assertEqual(FollowUpSent.objects.count(), 1)

    def test_same_token_already_in_history_is_refused(self):
        """Covers two requests racing with the same session token."""
        self._follow_up()
        token = self._token()
        FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, submission_token=token)
        response, svc = self._compose("post", {"subject": "Re: x", "body": "Hi", "follow_up_token": token})
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        svc.send_reply.assert_not_called()

    def test_stale_context_not_applied_to_unrelated_reply(self):
        self._follow_up()
        other = {"id": "gm-OTHER", "thread_id": "th-9", "subject": "Unrelated", "sender": "someone@else.example"}
        svc = self._compose_service(other)
        with patch("dashboard.views.GmailService", return_value=svc):
            get_resp = self.client.get(reverse("dashboard:compose_email", args=["gm-OTHER"]))
            self.assertNotContains(get_resp, "Manual follow-up")
            self.client.post(reverse("dashboard:compose_email", args=["gm-OTHER"]),
                             {"subject": "Re: Unrelated", "body": "Hi"})
        self.assertIsNone(svc.send_reply.call_args.kwargs.get("to"))
        self.assertEqual(FollowUpSent.objects.count(), 0)

    def test_plain_reply_without_follow_up_unchanged(self):
        response, svc = self._compose("post", {"subject": "Re: Gasket quote", "body": "Hi"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("gmail_ai"))
        self.assertNotIn("to", svc.send_reply.call_args.kwargs)
        self.assertEqual(FollowUpSent.objects.count(), 0)

    # board indicator
    def test_board_shows_last_follow_up(self):
        FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status="sent")
        FollowUpSent.objects.create(enquiry_id="ENQ-8", sent_by=self.user, status="failed")
        rows = [_enq("ENQ-7", "open", datetime(2025, 1, 1)), _enq("ENQ-8", "open", datetime(2025, 1, 2))]
        with patch("enquiries.views.services.list_enquiries", return_value=rows):
            response = self.client.get(reverse("enquiries:board"))
        self.assertContains(response, "data-last-follow-up", count=1)


class ConversationMatchingTests(TestCase):
    """GmailService helpers that turn search results into conversations."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("gm", "sales@ourcompany.example", "x")
        self.svc = GmailService(self.user)
        self.svc.own_addresses = lambda: {"sales@ourcompany.example"}

    def test_customer_is_first_participant_who_is_not_us(self):
        self.svc.get_thread_messages = lambda *a, **k: [
            {"id": "m1", "sender": "Buyer <buyer@acme.example>", "subject": "Quote", "to": "sales@ourcompany.example"},
            {"id": "m2", "sender": "sales@ourcompany.example", "subject": "Re: Quote", "to": "buyer@acme.example"},
        ]
        convo = self.svc.describe_conversation("th-1")
        self.assertEqual(convo["customer_email"], "buyer@acme.example")
        self.assertEqual(convo["latest_message_id"], "m2")
        self.assertEqual(convo["message_count"], 2)

    def test_customer_taken_from_recipient_when_we_wrote_every_message(self):
        self.svc.get_thread_messages = lambda *a, **k: [
            {"id": "m1", "sender": "sales@ourcompany.example", "to": "Buyer <buyer@acme.example>"},
        ]
        self.assertEqual(self.svc.describe_conversation("th-1")["customer_email"], "buyer@acme.example")

    def test_search_groups_by_thread_and_never_picks_one(self):
        captured = {}

        def fake_search(query, limit=20):
            captured["query"] = query
            return [{"id": "a", "thread_id": "t1"}, {"id": "b", "thread_id": "t1"}, {"id": "c", "thread_id": "t2"}]

        self.svc.search_messages = fake_search
        self.svc.describe_conversation = lambda tid, own=None: {"thread_id": tid, "customer_email": "x@y.example"}
        result = self.svc.find_enquiry_conversations(subject='Gasket "quote"', customer_name="Acme")
        self.assertEqual([c["thread_id"] for c in result], ["t1", "t2"])
        self.assertIn('subject:"Gasket quote"', captured["query"])
        self.assertIn('"Acme"', captured["query"])

    def test_search_by_customer_email_when_known(self):
        captured = {}
        self.svc.search_messages = lambda q, limit=20: captured.setdefault("q", q) and []
        self.svc.find_enquiry_conversations(subject="Quote", customer_email="buyer@acme.example")
        self.assertIn("(from:buyer@acme.example OR to:buyer@acme.example)", captured["q"])

    def test_search_http_error_becomes_friendly_error(self):
        from googleapiclient.errors import HttpError
        self.svc.service = MagicMock()
        resp = MagicMock(status=500)
        self.svc.service.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
            HttpError(resp, b"{}")
        )
        with self.assertRaises(GmailAPIError):
            self.svc.search_messages("subject:x")


class LocalDemoConversationTests(TestCase):
    """Locally, follow-ups find the built-in demo-* emails without calling Gmail."""

    def setUp(self):
        from dashboard.models import EmailResult
        from django.utils import timezone

        self.user = get_user_model().objects.create_user("demo", "sales@ourcompany.example", "x")
        for email_id, subject in [("demo-1", "Fwd: CAD files from past inquiries"), ("real-1", "CAD files")]:
            EmailResult.objects.create(
                user=self.user, email_id=email_id, sender="buyer@acme.example", subject=subject,
                thread_id=f"{email_id}-thread", parsed_date=timezone.now(),
            )
        self.svc = GmailService(self.user)
        self.svc.search_messages = MagicMock(side_effect=AssertionError("Gmail must not be searched"))

    @override_settings(DEBUG=True)
    def test_matches_demo_email_by_subject_without_gmail(self):
        result = self.svc.find_enquiry_conversations(subject="CAD files")
        self.assertEqual([c["thread_id"] for c in result], ["demo-1-thread"])  # never the real cached email
        self.assertEqual(result[0]["customer_email"], "buyer@acme.example")

    @override_settings(DEBUG=False, DEPLOY_ENV="production")
    def test_not_used_outside_local(self):
        self.svc.search_messages = MagicMock(return_value=[])
        self.assertEqual(self.svc.find_enquiry_conversations(subject="CAD files"), [])
        self.svc.search_messages.assert_called_once()


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


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
                   AI_REPLY_ALLOW_STUB=True, DEBUG=True)
class FollowUpAIDraftTests(FollowUpFlowTests.__mro__[0]):
    """'Draft with AI' on a follow-up writes a follow-up in the chosen tone."""

    def _ai(self, tone, message_id="gm-1", svc=None):
        svc = svc or self._compose_service()
        with patch("dashboard.views.GmailService", return_value=svc), \
             patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            response = self.client.post(reverse("dashboard:generate_ai_reply", args=[message_id]),
                                        data=f'{{"tone": "{tone}"}}', content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_follow_up_ai_draft_uses_sir_madam_and_differs_by_tone(self):
        self._follow_up()
        drafts = [self._ai(t) for t in ("professional and concise", "friendly and warm",
                                         "brief and direct", "empathetic")]
        self.assertEqual(len({d["reply"] for d in drafts}), 4)
        for d in drafts:
            self.assertTrue(d["is_sample"])
            self.assertTrue(d["reply"].startswith("Dear Sir/Madam,"))
            self.assertNotIn("Acme", d["reply"])
            self.assertTrue(d["reply"].endswith("AVIGIRI Team"))
            self.assertIn("Gasket quote", d["reply"])

    def test_plain_reply_ai_draft_is_not_a_follow_up(self):
        data = self._ai("friendly and warm")
        self.assertIn("Hey there", data["reply"])
        self.assertNotIn("following up", data["reply"].lower())


class CustomerFieldsOnCreateTests(TestCase):
    """Optional Customer name / email on the PDF upload and Google Sheet forms."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.SimpleUploadedFile = SimpleUploadedFile
        self.user = get_user_model().objects.create_user("custfields", "cf@example.com", "secret123")
        self.client.force_login(self.user)

    def _pdf(self, **extra):
        data = {"title": "PO-6303", "pdf": self.SimpleUploadedFile("po.pdf", b"%PDF-1.4", content_type="application/pdf")}
        data.update(extra)
        with patch("enquiries.views.services.create_enquiry", return_value={"enquiry_id": "ENQ-9"}) as create:
            response = self.client.post(reverse("enquiries:upload_pdf_enquiry"), data)
        return response, create

    def _sheet(self, **extra):
        data = {"title": "Project X"}
        data.update(extra)
        with patch("enquiries.views.build") as build, patch("enquiries.views.GmailService"), \
             patch("enquiries.views.services.create_enquiry", return_value={"enquiry_id": "ENQ-10"}) as create:
            build.return_value.spreadsheets.return_value.create.return_value.execute.return_value = {
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/x/edit", "spreadsheetId": "x"}
            response = self.client.post(reverse("enquiries:create_google_sheet"), data)
        return response, create

    def test_pdf_saves_customer_name_and_email(self):
        from enquiries.models import EnquiryContact
        response, create = self._pdf(customer_name="Kiran Rubber Industries", customer_email="Buyer@KiranRubber.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(create.call_args.args[1]["customer_name"], "Kiran Rubber Industries")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-9").customer_email, "buyer@kiranrubber.com")

    def test_pdf_without_customer_keeps_old_behaviour(self):
        from enquiries.models import EnquiryContact
        response, create = self._pdf()
        self.assertEqual(create.call_args.args[1]["customer_name"], "PO-6303")
        self.assertFalse(EnquiryContact.objects.exists())

    def test_pdf_invalid_email_is_rejected(self):
        response, create = self._pdf(customer_email="not-an-email")
        self.assertEqual(response.status_code, 302)
        create.assert_not_called()

    def test_sheet_saves_customer_name_and_email(self):
        from enquiries.models import EnquiryContact
        response, create = self._sheet(customer_name="Acme Ltd", customer_email="ops@acme.example")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(create.call_args.args[1]["customer_name"], "Acme Ltd")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-10").customer_email, "ops@acme.example")

    def test_sheet_invalid_email_is_rejected(self):
        response, create = self._sheet(customer_email="nope")
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid customer email", response.json()["error"])
        create.assert_not_called()

    def test_follow_up_searches_by_saved_email(self):
        from enquiries.models import EnquiryContact
        EnquiryContact.objects.create(enquiry_id="ENQ-9", customer_email="buyer@kiranrubber.com")
        enquiry = {"enquiry_id": "ENQ-9", "subject": "PO-6303", "customer_name": "Kiran Rubber Industries"}
        convo = {"thread_id": "t1", "subject": "PO-6303", "customer_email": "buyer@kiranrubber.com",
                 "latest_message_id": "m1", "message_count": 1}
        with patch("enquiries.views.services.fetch_enquiry", return_value=enquiry), \
             patch("enquiries.views.GmailService") as gs:
            gs.return_value.find_enquiry_conversations.return_value = [convo]
            response = self.client.get(reverse("enquiries:follow_up", args=["ENQ-9"]))
        gs.return_value.find_enquiry_conversations.assert_called_once_with(
            subject="PO-6303", customer_email="buyer@kiranrubber.com", customer_name="Kiran Rubber Industries")
        # one conversation found with the customer's email is confident enough to open the reply page
        self.assertRedirects(response, reverse("dashboard:compose_email", args=["m1"]), fetch_redirect_response=False)

    def test_board_shows_the_new_fields(self):
        with patch("enquiries.views.services.list_enquiries", return_value=[]):
            response = self.client.get(reverse("enquiries:board"))
        self.assertContains(response, 'name="customer_email"', count=2)
        self.assertContains(response, 'name="customer_name"', count=2)


class FollowUpTemplateTests(TestCase):
    """Steps 4-5: stage templates."""

    def test_each_stage_is_specific_and_signed(self):
        from dashboard.follow_up_templates import STAGE_CHOICES, build_follow_up_body
        user = get_user_model().objects.create_user("ananya", "a@example.com", "x", first_name="Ananya")
        bodies = {key: build_follow_up_body(key, reference="ENQ-7", subject="EPDM gaskets", user=user,
                                            share_url="https://x.example/s/", share_password="AG-pw")
                  for key, _ in STAGE_CHOICES}
        for key, body in bodies.items():
            self.assertTrue(body.startswith("Dear Sir/Madam,"))
            self.assertIn("AVIGIRI team", body)
            self.assertNotIn("Ananya", body)  # signed by the team, never a person
        # Enquiry stages have the link; the document named follows the stage; Marketing never has a link.
        for key in ("general", "waiting_po", "payment_pending"):
            self.assertIn("https://x.example/s/", bodies[key])
            self.assertIn("EPDM gaskets", bodies[key])
        self.assertIn("quotation we shared", bodies["waiting_po"])
        self.assertIn("proforma invoice we shared", bodies["payment_pending"])
        self.assertNotIn("https://", bodies["marketing"])
        self.assertIn("wasn't the right fit", bodies["marketing"])

    @override_settings(FOLLOW_UP_CONTACT_PHONE="+91 80 1234 5678")
    def test_phone_added_when_configured(self):
        from dashboard.follow_up_templates import build_follow_up_body
        body = build_follow_up_body("general", reference="ENQ-1", subject="x", attempt=3,
                                    share_url="https://x/", share_password="P")
        self.assertIn("reach us at +91 80 1234 5678", body)

    def test_unknown_stage_falls_back_to_general(self):
        from dashboard.follow_up_templates import build_follow_up_body
        self.assertEqual(build_follow_up_body("nope", reference="R", subject="S"),
                         build_follow_up_body("general", reference="R", subject="S"))


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class FollowUpStepsTests(TestCase):
    """Steps 6-9 on the compose page, the board and the Follow-up button."""

    OURS = "sales@ourcompany.example"
    ENQUIRY = {"enquiry_id": "ENQ-7", "subject": "Gasket quote", "customer_name": "Acme",
               "customer_reference": "EMAIL-orig-1", "status": "open"}
    CONVO = {"thread_id": "th-1", "subject": "Gasket quote", "customer_email": "buyer@acme.example",
             "latest_message_id": "gm-1", "message_count": 2}

    def setUp(self):
        self.user = get_user_model().objects.create_user("stepper", "s@example.com", "x", first_name="Ananya")
        self.client.force_login(self.user)
        p = patch("dashboard.views._ensure_local_demo_emails")
        p.start()
        self.addCleanup(p.stop)

    def _follow_up(self, enquiry=None):
        gs = MagicMock()
        gs.get_message.return_value = {"id": "orig-1", "thread_id": "th-1"}
        gs.describe_conversation.return_value = dict(self.CONVO)
        with patch("enquiries.views.services.fetch_enquiry", return_value={**self.ENQUIRY, **(enquiry or {})}), \
             patch("enquiries.views.GmailService", return_value=gs):
            return self.client.get(reverse("enquiries:follow_up", args=["ENQ-7"])), gs

    def _svc(self, thread):
        svc = MagicMock()
        svc.get_message.return_value = thread[-1]
        svc.get_thread_messages.return_value = thread
        svc.own_addresses.return_value = {self.OURS}
        svc.send_reply.return_value = {"id": "sent-1", "threadId": "th-1"}
        return svc

    def _thread(self, last_from_customer=False):
        customer_msg = {"id": "gm-0", "thread_id": "th-1", "sender": "Buyer <buyer@acme.example>",
                        "subject": "Gasket quote",
                        "attachments": [{"message_id": "gm-0", "attachment_id": "a0", "filename": "drawing.pdf",
                                         "mimeType": "application/pdf", "size": 2048}]}
        our_msg = {"id": "gm-1", "thread_id": "th-1", "sender": "Sales <%s>" % self.OURS,
                   "subject": "Re: Gasket quote",
                   "attachments": [{"message_id": "gm-1", "attachment_id": "a1", "filename": "AVIGIRI-quote.pdf",
                                    "mimeType": "application/pdf", "size": 4096}]}
        thread = [customer_msg, our_msg]
        if last_from_customer:
            thread.append({"id": "gm-2", "thread_id": "th-1", "sender": "buyer@acme.example",
                           "subject": "Re: Gasket quote", "attachments": []})
        return thread

    def _compose(self, svc, method="get", data=None):
        url = reverse("dashboard:compose_email", args=["gm-1"])
        with patch("dashboard.views.GmailService", return_value=svc):
            return self.client.post(url, data) if method == "post" else self.client.get(url)

    # Step 5: stage picker and reference on the page
    def test_compose_shows_stage_picker_reference_and_next_date(self):
        self._follow_up()
        response = self._compose(self._svc(self._thread()))
        self.assertContains(response, 'name="follow_up_stage"')
        self.assertContains(response, "Quotation sent")
        self.assertContains(response, "Proforma invoice sent")
        self.assertContains(response, 'name="next_follow_up_on"')
        self.assertContains(response, 'id="followUpStageBodies"')

    # Step 6: only our quote PDF is offered and ticked
    def test_our_quote_pdf_is_ticked_not_the_customers_files(self):
        self._follow_up()
        response = self._compose(self._svc(self._thread()))
        entries = response.context["attachment_entries"]
        self.assertEqual([e["filename"] for e in entries], ["AVIGIRI-quote.pdf"])
        self.assertEqual(len(response.context["form"].initial["include_attachments"]), 1)
        self.assertContains(response, "data-quote-attachment-note")

    # Step 8: stage and next date are saved with the follow-up
    def test_stage_and_next_date_saved(self):
        from datetime import timedelta
        from django.utils import timezone
        self._follow_up()
        token = self.client.session["follow_up_context"]["token"]
        when = (timezone.localdate() + timedelta(days=5)).isoformat()
        response = self._compose(self._svc(self._thread()), "post", {
            "subject": "Re: Gasket quote", "body": "Hi", "follow_up_token": token,
            "follow_up_stage": "waiting_po", "next_follow_up_on": when})
        self.assertEqual(response.status_code, 302)
        record = FollowUpSent.objects.get()
        self.assertEqual(record.stage, "waiting_po")
        self.assertEqual(record.next_follow_up_on.isoformat(), when)

    def test_past_next_date_is_rejected_and_nothing_sent(self):
        self._follow_up()
        token = self.client.session["follow_up_context"]["token"]
        svc = self._svc(self._thread())
        response = self._compose(svc, "post", {"subject": "Re: x", "body": "Hi", "follow_up_token": token,
                                               "next_follow_up_on": "2020-01-01"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be in the past")
        svc.send_reply.assert_not_called()
        self.assertFalse(FollowUpSent.objects.exists())

    # Step 7 + 8: board history and due badges
    def _board(self, rows):
        with patch("enquiries.views.services.list_enquiries", return_value=rows):
            return self.client.get(reverse("enquiries:board"))

    def test_board_shows_count_last_sender_and_due_state(self):
        from datetime import timedelta
        from django.utils import timezone
        today = timezone.localdate()
        for days_ago in (6, 2):
            FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status="sent",
                                        sent_at=timezone.now() - timedelta(days=days_ago),
                                        next_follow_up_on=today - timedelta(days=1))
        FollowUpSent.objects.create(enquiry_id="ENQ-8", sent_by=self.user, status="sent", next_follow_up_on=today)
        FollowUpSent.objects.create(enquiry_id="ENQ-9", sent_by=self.user, status="sent",
                                    next_follow_up_on=today + timedelta(days=3))
        FollowUpSent.objects.create(enquiry_id="ENQ-9", sent_by=self.user, status="failed")
        rows = [_enq(i, "open", datetime(2026, 11, 1)) for i in ("ENQ-7", "ENQ-8", "ENQ-9")]
        response = self._board(rows)
        self.assertContains(response, "Followed up 2 times, last on")
        self.assertContains(response, "by Ananya")
        self.assertContains(response, 'data-follow-up-due="overdue"')
        self.assertContains(response, 'data-follow-up-due="today"')
        self.assertContains(response, 'data-follow-up-due="later"')
        self.assertContains(response, "Followed up 1 time,", count=2)  # failed send not counted

    def test_closed_enquiry_has_no_due_badge(self):
        from django.utils import timezone
        FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status="sent",
                                    next_follow_up_on=timezone.localdate())
        response = self._board([_enq("ENQ-7", "converted", datetime(2026, 11, 1))])
        self.assertContains(response, "data-last-follow-up")
        self.assertNotContains(response, "data-follow-up-due")

    # Step 9: stop rules
    def test_closed_converted_rejected_enquiries_are_not_followed_up(self):
        for status in ("closed", "converted", "rejected"):
            response, gs = self._follow_up({"status": status})
            self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
            gs.describe_conversation.assert_not_called()
            self.assertNotIn("follow_up_context", self.client.session)

    @override_settings(FOLLOW_UP_MAX_COUNT=4)
    def test_limit_of_follow_ups(self):
        for _ in range(3):
            FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status="sent")
        response, _ = self._follow_up()
        self.assertIn("follow_up_context", self.client.session)  # 3 sent: a 4th is still allowed
        FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status="sent")
        response, gs = self._follow_up()
        self.assertRedirects(response, reverse("enquiries:board"), fetch_redirect_response=False)
        gs.describe_conversation.assert_not_called()

    def test_warns_when_customer_message_is_unanswered(self):
        self._follow_up()
        response = self._compose(self._svc(self._thread(last_from_customer=True)))
        self.assertContains(response, "data-customer-waiting")
        self._follow_up()
        response = self._compose(self._svc(self._thread()))
        self.assertNotContains(response, "data-customer-waiting")

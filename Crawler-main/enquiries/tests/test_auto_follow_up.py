"""Tests for Stage 2 automatic follow-ups (the rules, and what the hourly scheduler does with them)."""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.follow_up_templates import build_follow_up_body
from enquiries import auto_follow_up, scheduler
from enquiries.models import EnquiryContact, FollowUpSent
from enquiries.tests.share_helpers import make_share

TODAY = timezone.localdate()

AUTO_SETTINGS = dict(
    FOLLOW_UP_AUTO_ENABLED=True,
    FOLLOW_UP_AUTO_DRY_RUN=False,
    FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_AUTO_GAP_DAYS=[4, 7, 7],
    FOLLOW_UP_MAX_COUNT=4,
)


def _gmail(last_sender="sales@ourcompany.example", send_error=None):
    svc = MagicMock()
    svc.own_addresses.return_value = {"sales@ourcompany.example"}
    svc.get_thread_messages.return_value = [
        {"id": "m1", "sender": "Buyer <buyer@acme.example>"},
        {"id": "m2", "sender": last_sender},
    ]
    svc.get_message.return_value = {"id": "m2", "subject": "Re: Gasket quote", "thread_id": "t1"}
    if send_error:
        svc.send_reply.side_effect = send_error
    else:
        svc.send_reply.return_value = {"id": "sent-1", "threadId": "t1"}
    return svc


@override_settings(**AUTO_SETTINGS)
class AutoFollowUpTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        self.enquiries = {
            "E1": {"enquiry_id": "E1", "status": "open", "subject": "Gasket quote", "customer_name": "Acme"},
        }
        self.share_url, self.share_password = make_share("E1")
        p = patch.object(auto_follow_up.services, "list_enquiries", side_effect=lambda *a, **k: list(self.enquiries.values()))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(auto_follow_up.services, "fetch_enquiry", side_effect=lambda root, eid, **k: self.enquiries[eid])
        p.start()
        self.addCleanup(p.stop)

    def _sent(self, enquiry_id="E1", due=TODAY, thread="t1", days_ago=4, stage="waiting_po", token=None):
        return FollowUpSent.objects.create(
            enquiry_id=enquiry_id, customer_email="buyer@acme.example", sent_by=self.sales,
            status=FollowUpSent.STATUS_SENT, gmail_thread_id=thread, stage=stage,
            next_follow_up_on=due, sent_at=timezone.now() - timedelta(days=days_ago),
            submission_token=token,
        )

    def _due(self):
        """What the scheduler finds due today: {"enabled", "dry_run", "due": [...]} (nothing is sent)."""
        return {**scheduler._flags(), "due": auto_follow_up.due_follow_ups() if scheduler._flags()["enabled"] else []}

    def _send_one(self, enquiry_id):
        """What the scheduler does for one due enquiry: the follow-up outcome."""
        return {**scheduler._flags(), **auto_follow_up.run_auto_follow_up(enquiry_id)}

    # --- due list -------------------------------------------------------------------------
    def test_due_list_includes_only_eligible_enquiries(self):
        self._sent("E1")                                        # due
        self._sent("E2", due=TODAY + timedelta(days=2))         # not due yet
        self._sent("E3")                                        # closed
        self._sent("E4")                                        # paused
        self._sent("E5", thread="")                             # no confirmed conversation
        for _ in range(4):
            self._sent("E6")                                    # limit reached
        self.enquiries.update({
            "E2": {"enquiry_id": "E2", "status": "open"},
            "E3": {"enquiry_id": "E3", "status": "closed"},
            "E4": {"enquiry_id": "E4", "status": "open"},
            "E5": {"enquiry_id": "E5", "status": "open"},
            "E6": {"enquiry_id": "E6", "status": "open"},
        })
        EnquiryContact.objects.create(enquiry_id="E4", auto_follow_up_paused=True)

        data = self._due()
        self.assertEqual([d["enquiry_id"] for d in data["due"]], ["E1"])
        self.assertEqual(data["due"][0]["attempt"], 2)
        self.assertEqual(data["due"][0]["stage"], "waiting_po")

    @override_settings(FOLLOW_UP_AUTO_ENABLED=False)
    def test_due_list_empty_when_turned_off(self):
        self._sent("E1")
        data = self._due()
        self.assertEqual(data["due"], [])
        self.assertFalse(data["enabled"])

    # --- sending --------------------------------------------------------------------------
    def test_send_records_automatic_follow_up_with_next_date(self):
        self._sent("E1")
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            outcome = self._send_one("E1")
        self.assertEqual(outcome["result"], "sent")
        kwargs = gmail.send_reply.call_args.kwargs
        self.assertEqual(kwargs["to"], "buyer@acme.example")
        self.assertIn("meets your requirements", kwargs["body"])  # follow-up #2 wording
        self.assertIn(self.share_url, kwargs["body"])
        self.assertIn(f"Password: {self.share_password}", kwargs["body"])
        self.assertIn(f'href="{self.share_url}"', kwargs["html_body"])  # clickable in the HTML version
        self.assertNotIn("<a ", kwargs["body"])  # plain text: the link as text
        record = FollowUpSent.objects.get(source=FollowUpSent.SOURCE_AUTO)
        self.assertEqual(record.status, FollowUpSent.STATUS_SENT)
        self.assertEqual(record.next_follow_up_on, TODAY + timedelta(days=7))  # gap after #2

    def test_second_call_same_day_does_not_send_again(self):
        self._sent("E1")
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            self._send_one("E1")
            # Pretend the first send is still due (e.g. a run was repeated with an old due list).
            FollowUpSent.objects.filter(source=FollowUpSent.SOURCE_AUTO).update(next_follow_up_on=TODAY)
            second = self._send_one("E1")
        self.assertEqual(second["result"], "skipped")
        self.assertEqual(gmail.send_reply.call_count, 1)

    def test_customer_reply_skips(self):
        self._sent("E1")
        gmail = _gmail(last_sender="Buyer <buyer@acme.example>")
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            data = self._send_one("E1")
        self.assertEqual(data["result"], "skipped")
        self.assertIn("customer replied", data["reason"])
        gmail.send_reply.assert_not_called()

    @override_settings(FOLLOW_UP_AUTO_DRY_RUN=True)
    def test_dry_run_sends_and_records_nothing(self):
        self._sent("E1")
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            data = self._send_one("E1")
        self.assertEqual(data["result"], "dry_run")
        self.assertIn(self.share_url, data["body"])  # the fully rendered email is logged / returned
        self.assertIn(f"Password: {self.share_password}", data["body"])
        gmail.send_reply.assert_not_called()
        gmail.send_email.assert_not_called()
        self.assertFalse(FollowUpSent.objects.filter(source=FollowUpSent.SOURCE_AUTO).exists())

    def test_gmail_failure_is_recorded(self):
        self._sent("E1")
        gmail = _gmail(send_error=RuntimeError("quota exceeded"))
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            outcome = self._send_one("E1")
        self.assertEqual(outcome["result"], "failed")
        self.assertEqual(FollowUpSent.objects.get(source=FollowUpSent.SOURCE_AUTO).status, FollowUpSent.STATUS_FAILED)

    def test_enquiry_without_manual_follow_up_is_skipped(self):
        data = self._send_one("E1")
        self.assertEqual(data["result"], "skipped")

    @override_settings(FOLLOW_UP_AUTO_SENDER="")
    def test_missing_sender_fails(self):
        self._sent("E1")
        self.assertEqual(self._send_one("E1")["result"], "failed")

    # --- pause button ---------------------------------------------------------------------
    def test_pause_button_toggles(self):
        self.client.force_login(self.sales)
        url = reverse("enquiries:toggle_auto_follow_up", args=["E1"])
        with patch("magicwand.access.user_has_service_access", return_value=True):
            resp = self.client.post(url, {"next": "/enquiries/?status=open"})
            self.assertRedirects(resp, "/enquiries/?status=open", fetch_redirect_response=False)
            self.assertTrue(EnquiryContact.objects.get(enquiry_id="E1").auto_follow_up_paused)
            self.client.post(url, {"next": "https://evil.example/"})
        self.assertFalse(EnquiryContact.objects.get(enquiry_id="E1").auto_follow_up_paused)


@override_settings(FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_AUTO_GAP_DAYS=[4, 7, 7])
class ManualPageAutoChoiceTests(TestCase):
    """The 'send the next ones automatically' choice on the manual follow-up page."""

    MESSAGE = {"id": "gm-1", "thread_id": "th-1", "subject": "Gasket quote", "sender": "sales@ourcompany.example"}

    def setUp(self):
        self.user = get_user_model().objects.create_user("fu", "sales@ourcompany.example", "x")
        self.client.force_login(self.user)
        p = patch("dashboard.views._ensure_local_demo_emails")
        p.start()
        self.addCleanup(p.stop)
        session = self.client.session
        session["follow_up_context"] = {
            "enquiry_id": "ENQ-7", "customer_email": "buyer@acme.example", "customer_name": "Acme",
            "subject": "Gasket quote", "gmail_message_id": "gm-1", "gmail_thread_id": "th-1", "token": "tok-1",
        }
        session.save()

    def _send(self, **extra):
        svc = MagicMock()
        svc.get_message.return_value = dict(self.MESSAGE)
        svc.get_thread_messages.return_value = [dict(self.MESSAGE)]
        svc.own_addresses.return_value = {"sales@ourcompany.example"}
        svc.send_reply.return_value = {"id": "sent-1", "threadId": "th-1"}
        data = {
            "subject": "Re: Gasket quote", "body": "Dear Sir/Madam, ...", "follow_up_token": "tok-1",
            "follow_up_stage": "waiting_po", "next_follow_up_on": (TODAY + timedelta(days=3)).isoformat(),
            **extra,
        }
        with patch("dashboard.views.GmailService", return_value=svc):
            return self.client.post(reverse("dashboard:compose_email", args=["gm-1"]), data), svc

    def test_turning_automation_off_pauses_the_enquiry(self):
        resp, svc = self._send(auto_follow_up_shown="1")  # checkbox left unticked
        self.assertRedirects(resp, reverse("enquiries:board"), fetch_redirect_response=False)
        svc.send_reply.assert_called_once()
        self.assertTrue(EnquiryContact.objects.get(enquiry_id="ENQ-7").auto_follow_up_paused)

    def test_turning_automation_on_resumes_a_paused_enquiry(self):
        EnquiryContact.objects.create(enquiry_id="ENQ-7", auto_follow_up_paused=True)
        self._send(auto_follow_up_shown="1", auto_follow_up="1")
        self.assertFalse(EnquiryContact.objects.get(enquiry_id="ENQ-7").auto_follow_up_paused)

    def test_automation_needs_a_next_date_and_nothing_is_sent_without_it(self):
        with patch("dashboard.views.render", return_value=HttpResponse("form")):
            resp, svc = self._send(auto_follow_up_shown="1", auto_follow_up="1", next_follow_up_on="")
        svc.send_reply.assert_not_called()
        self.assertFalse(FollowUpSent.objects.exists())

    def test_choice_not_on_page_changes_nothing(self):
        EnquiryContact.objects.create(enquiry_id="ENQ-7", auto_follow_up_paused=True)
        self._send()  # last allowed follow-up: the checkbox is not shown
        self.assertTrue(EnquiryContact.objects.get(enquiry_id="ENQ-7").auto_follow_up_paused)

    def test_plan_lists_the_remaining_automatic_emails(self):
        from dashboard.views import _auto_follow_up_plan

        FollowUpSent.objects.create(enquiry_id="ENQ-7", sent_by=self.user, status=FollowUpSent.STATUS_SENT)
        plan = _auto_follow_up_plan({"enquiry_id": "ENQ-7", "subject": "Gasket quote"}, self.user)
        self.assertEqual(plan["this_attempt"], 2)
        steps = plan["stages"]["waiting_po"]
        self.assertEqual([s["attempt"] for s in steps], [3, 4])
        self.assertIn("7 days after follow-up #3", steps[1]["when"])
        self.assertTrue(steps[1]["final"])
        self.assertIn("gentle reminder", steps[1]["body"])


class TemplateAndScheduleTests(TestCase):
    def test_first_follow_up_wording_is_unchanged(self):
        self.assertEqual(
            build_follow_up_body("waiting_po", reference="E1", subject="Gasket"),
            build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=1),
        )

    def test_follow_up_4_is_the_last_template(self):
        body = build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=4)
        self.assertIn("gentle reminder", body)
        self.assertEqual(body, build_follow_up_body("waiting_po", reference="E1", subject="Gasket", attempt=9))

    @override_settings(FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, MARKETING_FOLLOW_UP_COUNT=4)
    def test_next_dates(self):
        d = date(2026, 11, 6)
        self.assertEqual(auto_follow_up.next_follow_up_date(1, d), date(2026, 11, 8))  # every 2 days
        self.assertEqual(auto_follow_up.next_follow_up_date(3, d), date(2026, 11, 8))
        self.assertIsNone(auto_follow_up.next_follow_up_date(4, d))
        # Marketing: 4 follow-ups, then one more step for its closing email.
        self.assertEqual(auto_follow_up.next_follow_up_date(4, d, "marketing"), date(2026, 11, 8))
        self.assertIsNone(auto_follow_up.next_follow_up_date(5, d, "marketing"))


@override_settings(**AUTO_SETTINGS)
class ShareLinkInFollowUpTests(TestCase):
    """Every automatic follow-up repeats the link and password of its quotation / PI email."""

    URL = "https://www.magicwand.biz/enquiries/share/magicwand/ENQ-3331/Akz7QxkCbOpW/"
    PASSWORD = "AG-p9xkKwZR"

    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gasket quote", "customer_name": "Acme"}
        for name, value in (("list_enquiries", [enquiry]), ("fetch_enquiry", enquiry)):
            p = patch.object(auto_follow_up.services, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _start(self, *, days_ago=4, url=URL, password=PASSWORD):
        from enquiries.models import FollowUpStart
        return FollowUpStart.objects.create(
            enquiry_id="E1", stage="waiting_po", gmail_thread_id="t1", gmail_message_id="m1",
            customer_email="buyer@acme.example", subject="Gasket quote", share_url=url, share_password=password,
            started_at=timezone.now() - timedelta(days=days_ago))

    def test_the_start_record_keeps_the_link_and_password(self):
        start = auto_follow_up.record_start("E1", "waiting_po", sent={"threadId": "t1", "id": "m1"},
                                            customer_email="buyer@acme.example", cc=[], subject="Gasket quote",
                                            share_url=self.URL, share_password=self.PASSWORD)
        self.assertEqual((start.share_url, start.share_password), (self.URL, self.PASSWORD))

    def test_the_automatic_email_has_the_link_and_password(self):
        url, password = make_share("E1", password=self.PASSWORD)
        self._start(url=url, password=password)
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            outcome = auto_follow_up.run_auto_follow_up("E1")
        self.assertEqual(outcome["result"], "sent")
        body = gmail.send_reply.call_args.kwargs["body"]
        self.assertIn(url, body)
        self.assertIn(f"Password: {self.PASSWORD}", body)
        self.assertIn("view the quotation online", body)

    def test_a_missing_share_link_is_generated_from_the_latest_revision(self):
        make_share("E1", password="AG-old", save_on_contact=False)
        url, password = make_share("E1", password="AG-new", save_on_contact=False)  # latest revision
        self._start(url="", password="")
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail),                 override_settings(ENQUIRY_SHARE_BASE_URL="https://portal.example"):
            outcome = auto_follow_up.run_auto_follow_up("E1")
        self.assertEqual(outcome["result"], "sent")
        body = gmail.send_reply.call_args.kwargs["body"]
        contact = EnquiryContact.objects.get(enquiry_id="E1")
        self.assertTrue(contact.follow_up_share_url.startswith("https://portal.example/enquiries/share/"))
        self.assertIn(contact.follow_up_share_url, body)
        self.assertIn(f"Password: {contact.follow_up_share_password}", body)
        self.assertNotEqual(contact.follow_up_share_url, url)  # a new link to the same latest revision

    def test_follow_up_uses_its_own_enquiry_link_never_another(self):
        make_share("E2", password="AG-other")  # another enquiry's link, newer
        url, password = make_share("E1", password=self.PASSWORD)
        self._start(url=url, password=password)
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            auto_follow_up.run_auto_follow_up("E1")
        body = gmail.send_reply.call_args.kwargs["body"]
        self.assertIn(url, body)
        self.assertNotIn("AG-other", body)

    def test_no_share_link_anywhere_skips_the_send_with_an_error(self):
        self._start(url="", password="")  # no ShareLink exists for E1
        gmail = _gmail()
        with patch.object(auto_follow_up, "GmailService", return_value=gmail),                 self.assertLogs("enquiries.auto_follow_up", level="ERROR"):
            outcome = auto_follow_up.run_auto_follow_up("E1")
        self.assertEqual(outcome["result"], "failed")
        gmail.send_reply.assert_not_called()
        self.assertFalse(FollowUpSent.objects.filter(source=FollowUpSent.SOURCE_AUTO).exists())

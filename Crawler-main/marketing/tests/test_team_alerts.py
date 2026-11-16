"""Marketing team alerts: To kumar@avigiri.com, CC manju@ and apai@, for revive, won back, customer replies and
the closing email."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from dashboard.gmail_service import GmailService
from enquiries import auto_follow_up
from enquiries.models import FollowUpSent, FollowUpStart
from marketing import notify
from marketing.models import MarketingActivity, MarketingLead

OURS = "sales@ourcompany.example"
FLOW = dict(
    FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_AUTO_GAP_DAYS=[2], MARKETING_AUTO_FOLLOW_UP=True, MARKETING_FOLLOW_UP_COUNT=4,
)


class AlertAddressTests(TestCase):
    def test_defaults(self):
        self.assertEqual(notify.alert_recipients(),
                         ("kumar@avigiri.com", ["manju@avigiri.com", "apai@avigiri.com"]))

    @override_settings(TEAM_ALERT_TO="Kumar@Avigiri.com", TEAM_ALERT_CC="kumar@avigiri.com; a@x.example, a@x.example")
    def test_cleaned_and_never_cc_the_to_address(self):
        self.assertEqual(notify.alert_recipients(), ("kumar@avigiri.com", ["a@x.example"]))

    def test_alert_email_goes_to_kumar_with_the_others_in_cc(self):
        user = get_user_model().objects.create_user("sales", OURS, "x")
        with patch.object(GmailService, "send_email") as send:
            problem = notify.notify_team(user, "Customer replied: Acme", ["Acme replied."], url="https://x/y")
        self.assertEqual(problem, "")
        args, kwargs = send.call_args
        self.assertEqual((args[0], args[1]), ("kumar@avigiri.com", "Customer replied: Acme"))
        self.assertEqual(kwargs["cc"], ["manju@avigiri.com", "apai@avigiri.com"])
        self.assertIn("Acme replied.", args[2])
        self.assertIn("https://x/y", args[2])

    @override_settings(TEAM_ALERT_TO="")
    def test_off_when_no_to_address(self):
        user = get_user_model().objects.create_user("sales", OURS, "x")
        with patch.object(GmailService, "send_email") as send:
            self.assertEqual(notify.notify_team(user, "S", ["L"]), "")
        send.assert_not_called()

    def test_a_gmail_problem_is_returned_never_raised(self):
        user = get_user_model().objects.create_user("sales", OURS, "x")
        with patch.object(GmailService, "send_email", side_effect=RuntimeError("boom")):
            self.assertIn("alert was not sent", notify.notify_team(user, "S", ["L"]))


def _gmail(last_sender, snippet="Please send a new quote"):
    svc = MagicMock()
    svc.own_addresses.return_value = {OURS}
    svc.get_thread_messages.return_value = [{"id": "m1", "sender": OURS},
                                            {"id": "m2", "sender": last_sender, "snippet": snippet}]
    svc.get_message.return_value = {"id": "m2", "subject": "Quotation Q-1", "thread_id": "T1", "snippet": snippet}
    svc.send_reply.return_value = {"id": "r1", "threadId": "T1"}
    return svc


@override_settings(**FLOW)
class AutoFollowUpAlertTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme"}
        p = patch.object(auto_follow_up.services, "fetch_enquiry", side_effect=lambda *a, **k: self.enquiry)
        p.start()
        self.addCleanup(p.stop)
        self.send = patch.object(GmailService, "send_email").start()
        self.addCleanup(patch.stopall)

    def _start(self, sent):
        FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="T1",
                                     customer_email="buyer@acme.example",
                                     started_at=timezone.now() - timedelta(days=2 * sent + 2, minutes=5))
        for n in range(sent):
            FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales,
                                        status="sent", gmail_thread_id="T1", stage="marketing",
                                        sent_at=timezone.now() - timedelta(days=2 * (sent - n), minutes=1),
                                        next_follow_up_on=timezone.localdate())

    def _run(self, gmail):
        with patch.object(auto_follow_up, "GmailService", return_value=gmail):
            return auto_follow_up.run_auto_follow_up("E1")

    def test_customer_reply_alerts_the_team_once(self):
        self._start(sent=1)
        gmail = _gmail("Buyer <buyer@acme.example>")
        self.assertEqual(self._run(gmail)["result"], "skipped")
        self.assertEqual(self.send.call_count, 1)
        args, kwargs = self.send.call_args
        self.assertEqual((args[0], args[1]), ("kumar@avigiri.com", "Customer replied: enquiry E1"))
        self.assertEqual(kwargs["cc"], ["manju@avigiri.com", "apai@avigiri.com"])
        self.assertIn("Please send a new quote", args[2])
        self._run(gmail)  # the next scheduled run does not alert again
        self.assertEqual(self.send.call_count, 1)
        self.assertTrue(MarketingActivity.objects.filter(text__startswith="Customer replied to Marketing").exists())

    def test_quotation_reply_does_not_alert_marketing(self):
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T1",
                                     customer_email="buyer@acme.example",
                                     started_at=timezone.now() - timedelta(days=3))
        self.enquiry["status"] = "open"
        self._run(_gmail("Buyer <buyer@acme.example>"))
        # The Enquiries portal's own alert goes out for a quotation reply, not Marketing's.
        self.assertEqual(self.send.call_args.args[1], "Customer replied: E1 - Acme")

    def test_closing_email_alerts_the_team(self):
        self._start(sent=4)
        self.assertEqual(self._run(_gmail(OURS))["result"], "sent")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.args[1], "Marketing closed enquiry E1")

    def test_ordinary_follow_up_sends_no_alert(self):
        self._start(sent=1)
        self.assertEqual(self._run(_gmail(OURS))["result"], "sent")
        self.send.assert_not_called()

    @override_settings(FOLLOW_UP_AUTO_DRY_RUN=True)
    def test_test_mode_sends_no_alert(self):
        self._start(sent=1)
        self._run(_gmail("Buyer <buyer@acme.example>"))
        self.send.assert_not_called()


@override_settings(MARKETING_WAIT_DAYS=30, STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class RevivedAndWonBackTests(TestCase):
    def test_won_back_alerts_the_team(self):
        from django.urls import reverse
        user = get_user_model().objects.create_superuser("mk", "mk@example.com", "x")
        self.client.force_login(user)
        enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme",
                   "customer_reference": "", "remarks": "", "received_at_dt": timezone.now() - timedelta(days=90)}
        with patch("enquiries.services.list_enquiries", return_value=[enquiry]), \
             patch("enquiries.services.fetch_enquiry", return_value=enquiry), \
             patch("enquiries.services.update_enquiry", return_value={}), \
             patch.object(GmailService, "send_email") as team_send, \
             patch("marketing.notify._push", return_value=0):
            self.client.post(reverse("marketing:detail", args=["E1"]),
                             {"action": "mark_accepted", "accept_note": "They called back"}, follow=True)
        self.assertTrue(MarketingLead.objects.get(enquiry_id="E1").accepted_at)
        self.assertEqual(team_send.call_args.args[:2], ("kumar@avigiri.com", "Enquiry E1 accepted: Acme"))
        self.assertEqual(team_send.call_args.kwargs["cc"], ["manju@avigiri.com", "apai@avigiri.com"])

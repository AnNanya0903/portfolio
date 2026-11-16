"""Team alerts for the Enquiries portal: To kumar@avigiri.com, CC manju@ and apai@, once per event."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.gmail_service import GmailService
from enquiries import auto_follow_up, status_emails, team_alerts
from enquiries.models import FollowUpSent, FollowUpStart, TeamAlertSent

OURS = "sales@ourcompany.example"
STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
CC = ["manju@avigiri.com", "apai@avigiri.com"]


def _sent(send, subject):
    """The (args, kwargs) of the alert with this subject, or fail."""
    call = next((c for c in send.call_args_list if c.args[1] == subject), None)
    assert call is not None, [c.args[1] for c in send.call_args_list]
    return call


class SharedAlertTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", OURS, "x")
        self.send = patch.object(GmailService, "send_email").start()
        self.addCleanup(patch.stopall)

    def test_goes_to_kumar_with_manju_and_apai_in_cc(self):
        self.assertEqual(team_alerts.notify_team(self.user, "Subject", ["Line one"], url="https://x/y"), "")
        args, kwargs = self.send.call_args
        self.assertEqual((args[0], args[1]), ("kumar@avigiri.com", "Subject"))
        self.assertEqual(kwargs["cc"], CC)
        self.assertTrue(args[2].startswith("Hello,"))
        self.assertIn("https://x/y", args[2])

    def test_once_key_sends_only_once(self):
        team_alerts.notify_team(self.user, "S", ["L"], once_key="k1")
        team_alerts.notify_team(self.user, "S", ["L"], once_key="k1")
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(TeamAlertSent.objects.count(), 1)

    @override_settings(TEAM_ALERT_TO="")
    def test_off_when_no_to_address(self):
        team_alerts.notify_team(self.user, "S", ["L"])
        self.send.assert_not_called()

    @override_settings(FOLLOW_UP_AUTO_SENDER="sales")
    def test_event_without_a_signed_in_user_uses_the_automatic_sender(self):
        team_alerts.notify_team(None, "S", ["L"])
        self.assertEqual(self.send.call_count, 1)

    @override_settings(FOLLOW_UP_AUTO_SENDER="")
    def test_no_sender_means_no_alert_and_no_error(self):
        self.assertEqual(team_alerts.notify_team(None, "S", ["L"]), "")
        self.send.assert_not_called()

    def test_gmail_problem_is_returned_not_raised(self):
        self.send.side_effect = RuntimeError("boom")
        self.assertIn("alert was not sent", team_alerts.notify_team(self.user, "S", ["L"]))


@override_settings(FOLLOW_UP_AUTO_SENDER="sales", **STATIC)
class EnquiryEventAlertTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("sales", OURS, "x")
        self.client.force_login(self.user)
        self.enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme"}
        self.send = patch.object(GmailService, "send_email").start()
        patch("enquiries.views.services.fetch_enquiry", side_effect=lambda *a, **k: dict(self.enquiry)).start()
        patch("enquiries.views.services.update_enquiry", return_value={}).start()
        patch("enquiries.views.status_emails.send_status_email",
              return_value={"result": "skipped", "note": ""}).start()
        self.addCleanup(patch.stopall)

    def test_customer_accepting_on_the_share_link_alerts_the_team(self):
        token = signing.dumps({"enquiry_id": "E1"}, salt="enquiry-share")
        response = self.client.post(reverse("enquiries:share_action"), {"token": token, "action": "accept"})
        self.assertEqual(response.json()["new_status"], "accepted")
        args, kwargs = _sent(self.send, "Customer accepted the quotation: E1 - Acme")
        self.assertEqual((args[0], kwargs["cc"]), ("kumar@avigiri.com", CC))

    def test_customer_rejecting_alerts_the_team_with_the_reason(self):
        token = signing.dumps({"enquiry_id": "E1"}, salt="enquiry-share")
        self.client.post(reverse("enquiries:share_action"),
                         {"token": token, "action": "reject", "reason": "Too expensive"})
        args, _ = _sent(self.send, "Customer rejected the quotation: E1 - Acme")
        self.assertIn("Too expensive", args[2])

    def test_payment_received_alerts_the_team(self):
        self.enquiry["status"] = "accepted"
        self.client.post(reverse("enquiries:board"), {"action": "update_payment", "enquiry_id": "E1",
                                                      "payment_status": "paid", "payment_reference": "UTR123"})
        args, _ = _sent(self.send, "Payment received: E1 - Acme")
        self.assertIn("UTR123", args[2])

    def test_staff_rejecting_alerts_the_team(self):
        from enquiries.views import _after_status_change
        request = MagicMock(user=self.user)
        request.build_absolute_uri.return_value = "https://x/e"
        with patch("enquiries.views.messages"):
            _after_status_change(request, "E1", {**self.enquiry, "status": "open"}, "rejected")
        _sent(self.send, "Enquiry rejected: E1 - Acme")


@override_settings(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
                   FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=2)
class FollowUpEventAlertTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme"}
        patch.object(auto_follow_up.services, "fetch_enquiry", side_effect=lambda *a, **k: self.enquiry).start()
        self.send = patch.object(GmailService, "send_email").start()
        self.addCleanup(patch.stopall)
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T1",
                                     customer_email="buyer@acme.example",
                                     started_at=timezone.now() - timedelta(days=3))

    def _gmail(self, last_sender):
        svc = MagicMock()
        svc.own_addresses.return_value = {OURS}
        svc.get_thread_messages.return_value = [{"id": "m1", "sender": OURS},
                                                {"id": "m2", "sender": last_sender, "snippet": "Send revised price"}]
        svc.get_message.return_value = {"id": "m2", "subject": "Quotation", "thread_id": "T1"}
        svc.send_reply.return_value = {"id": "r1", "threadId": "T1"}
        return svc

    def _run(self, svc):
        with patch.object(auto_follow_up, "GmailService", return_value=svc):
            return auto_follow_up.run_auto_follow_up("E1")

    def test_customer_reply_alerts_once_and_follow_ups_stop(self):
        svc = self._gmail("Buyer <buyer@acme.example>")
        self.assertEqual(self._run(svc)["result"], "skipped")
        self._run(svc)
        self.assertEqual(self.send.call_count, 1)
        args, _ = _sent(self.send, "Customer replied: E1 - Acme")
        self.assertIn("Send revised price", args[2])
        self.assertIn("quotation", args[2])

    def test_a_normal_follow_up_sends_no_alert(self):
        self.assertEqual(self._run(self._gmail(OURS))["result"], "sent")
        self.send.assert_not_called()

    def test_closing_email_alerts_the_team(self):
        with patch.object(status_emails, "send_status_email", return_value={"result": "sent"}), \
             patch("enquiries.services.update_enquiry", return_value={}):
            status_emails.send_closing_email(self.enquiry, "http://api")
        args, _ = _sent(self.send, "Enquiry closed, no reply: E1 - Acme")
        self.assertIn("Closed", args[2])

"""Tests for notifying the salesperson on Revive and the scheduled reply check."""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.gmail_service import GmailAPIError
from enquiries import scheduler
from marketing import campaigns, notify
from marketing.models import Campaign, CampaignRecipient, MarketingActivity, MarketingLead

BASE = dict(
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7, MARKETING_REPLY_CHECK_DAYS=60,
    MARKETING_REVIVE_NOTIFY_EMAIL=True,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
)

User = get_user_model()


@override_settings(**BASE)
class FindUserTests(TestCase):
    def setUp(self):
        self.priya = User.objects.create_user("priya", "priya@ourcompany.example", "x",
                                              first_name="Priya", last_name="Sharma")
        User.objects.create_user("arun1", "arun1@ourcompany.example", "x", first_name="Arun")
        User.objects.create_user("arun2", "arun2@ourcompany.example", "x", first_name="Arun")
        User.objects.create_user("gone", "gone@ourcompany.example", "x", first_name="Gone", is_active=False)

    def test_matches_username_email_full_and_unique_first_name(self):
        for value in ("priya", "PRIYA@ourcompany.example", "Priya Sharma", "priya"):
            self.assertEqual(notify.find_user(value), self.priya, value)
        self.assertEqual(notify.find_user("Priya"), self.priya)

    def test_no_guessing(self):
        self.assertIsNone(notify.find_user("Arun"))  # two people called Arun
        self.assertIsNone(notify.find_user("Gone"))  # inactive
        self.assertIsNone(notify.find_user(""))
        self.assertIsNone(notify.find_user("Someone Else"))


@override_settings(**BASE)
class ReviveNoticeTests(TestCase):
    def setUp(self):
        self.mk = User.objects.create_user("mk", "mk@ourcompany.example", "x", first_name="Meera")
        self.priya = User.objects.create_user("priya", "priya@ourcompany.example", "x",
                                              first_name="Priya", last_name="Sharma")
        self.ravi = User.objects.create_user("ravi", "ravi@ourcompany.example", "x", first_name="Ravi",
                                             last_name="Kumar")
        self.client.force_login(self.mk)
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "customer_name": "Acme", "subject": "Seal",
                        "customer_reference": "", "assigned_to": "Priya Sharma",
                        "received_at_dt": timezone.now() - timedelta(days=90)}
        self.url = reverse("marketing:detail", args=["E1"])

    def _patches(self, gmail_error=None):
        fetch = patch("enquiries.services.fetch_enquiry", return_value=self.enquiry)
        create = patch("enquiries.services.create_enquiry", return_value={"enquiry_id": "E50"})
        send = patch("marketing.campaigns.send_new_email", side_effect=gmail_error)
        push = patch("push_notifications.services.send_push_to_users", return_value=1)
        gmail = patch("marketing.notify.GmailService")
        return fetch, create, send, push, gmail

    def test_detail_preselects_the_original_salesperson(self):
        with patch("enquiries.services.fetch_enquiry", return_value=self.enquiry):
            response = self.client.get(self.url)
        self.assertEqual(response.context["matched_salesperson"], self.priya)

    def test_revive_notifies_the_same_salesperson(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create as create_mock, send as send_mock, push as push_mock, gmail:
            self.client.post(self.url, {"action": "revive", "assign_to": self.priya.pk, "revive_note": "Wants 5% less"})
        self.assertEqual(create_mock.call_args.args[1]["assigned_to"], "Priya Sharma")  # original wording kept
        push_mock.assert_called_once()
        self.assertEqual(push_mock.call_args.args[0], [self.priya])
        to, subject, body = send_mock.call_args.args[1:]
        self.assertEqual(to, "priya@ourcompany.example")
        self.assertIn("E50", subject)
        self.assertIn("Wants 5% less", body)
        self.assertIn("/enquiries/E50/", body)
        self.assertTrue(MarketingActivity.objects.filter(text="Priya Sharma was notified by phone and email.").exists())

    def test_revive_can_hand_over_to_someone_else(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create as create_mock, send as send_mock, push, gmail:
            self.client.post(self.url, {"action": "revive", "assign_to": self.ravi.pk})
        self.assertEqual(create_mock.call_args.args[1]["assigned_to"], "Ravi Kumar")
        self.assertEqual(send_mock.call_args.args[1], "ravi@ourcompany.example")

    def test_email_problem_does_not_undo_the_revive(self):
        fetch, create, send, push, gmail = self._patches(gmail_error=GmailAPIError("sign-in failed"))
        with fetch, create, send, push, gmail:
            response = self.client.post(self.url, {"action": "revive", "assign_to": self.priya.pk}, follow=True)
        self.assertEqual(MarketingLead.objects.get(enquiry_id="E1").revived_enquiry_id, "E50")
        self.assertContains(response, "could not email Priya Sharma")
        self.assertTrue(MarketingActivity.objects.filter(text__startswith="Notice: could not email").exists())

    def test_nobody_chosen_keeps_original_and_sends_nothing(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create as create_mock, send as send_mock, push as push_mock, gmail:
            self.client.post(self.url, {"action": "revive", "assign_to": ""})
        self.assertEqual(create_mock.call_args.args[1]["assigned_to"], "Priya Sharma")
        send_mock.assert_not_called()
        push_mock.assert_not_called()

    def test_unknown_person_is_refused(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create as create_mock, send, push, gmail:
            self.client.post(self.url, {"action": "revive", "assign_to": "99999"})
        create_mock.assert_not_called()

    @override_settings(MARKETING_REVIVE_NOTIFY_EMAIL=False)
    def test_says_so_when_nobody_could_be_told(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create, send, push as push_mock, gmail:
            push_mock.return_value = 0  # phone notifications not set up
            response = self.client.post(self.url, {"action": "revive", "assign_to": self.priya.pk}, follow=True)
        self.assertContains(response, "please tell them yourself")
        self.assertFalse(MarketingActivity.objects.filter(text__contains="was notified").exists())

    def test_no_email_to_yourself(self):
        fetch, create, send, push, gmail = self._patches()
        with fetch, create, send as send_mock, push, gmail:
            self.client.post(self.url, {"action": "revive", "assign_to": self.mk.pk})
        send_mock.assert_not_called()


@override_settings(**BASE)
class ScheduledReplyCheckTests(TestCase):
    def setUp(self):
        self.sender = User.objects.create_user("mk", "mk@ourcompany.example", "x")

    def _campaign(self, name, sent_days_ago, sender=None):
        campaign = Campaign.objects.create(name=name, subject="S", body="B", status=Campaign.STATUS_SENT,
                                           sent_by=sender or self.sender,
                                           sent_at=timezone.now() - timedelta(days=sent_days_ago))
        CampaignRecipient.objects.create(campaign=campaign, customer_email=f"{name}@cust.example",
                                         customer_name=name, enquiry_id=f"E-{name}",
                                         status=CampaignRecipient.STATUS_SENT, gmail_thread_id=f"t-{name}",
                                         sent_at=campaign.sent_at)
        return campaign

    def test_checks_recent_campaigns_with_their_senders_gmail(self):
        recent = self._campaign("recent", 5)
        self._campaign("old", 90)  # outside MARKETING_REPLY_CHECK_DAYS
        gmail = MagicMock()
        gmail.own_addresses.return_value = {"mk@ourcompany.example"}
        gmail.get_thread_messages.return_value = [{"sender": "mk@ourcompany.example"},
                                                  {"sender": "recent@cust.example", "snippet": "Interested"}]
        with patch("marketing.campaigns.GmailService", return_value=gmail) as gmail_cls, \
                patch("push_notifications.services.send_push_to_users", return_value=1) as push:
            data = scheduler.reply_check_step()
        self.assertEqual(data["replied"], 1)
        self.assertEqual([c["name"] for c in data["campaigns"]], ["recent"])
        gmail_cls.assert_called_once_with(self.sender)
        self.assertIsNotNone(recent.recipients.get().replied_at)
        self.assertEqual(push.call_args.args[0], [self.sender])

    def test_one_failing_campaign_does_not_stop_the_others(self):
        self._campaign("broken", 3)
        good = self._campaign("good", 2)
        good_gmail = MagicMock()
        good_gmail.own_addresses.return_value = {"mk@ourcompany.example"}
        good_gmail.get_thread_messages.return_value = [{"sender": "good@cust.example"}]
        bad_gmail = MagicMock()
        bad_gmail.own_addresses.return_value = {"mk@ourcompany.example"}
        bad_gmail.get_thread_messages.side_effect = GmailAPIError("quota")
        with patch("marketing.campaigns.GmailService", side_effect=[bad_gmail, good_gmail]), \
                patch("push_notifications.services.send_push_to_users", return_value=0):
            results = campaigns.check_all_replies()
        by_name = {r["name"]: r for r in results}
        self.assertEqual(by_name["broken"]["result"], "failed")
        self.assertEqual(by_name["good"]["replied"], 1)

    def test_inactive_sender_is_skipped(self):
        gone = User.objects.create_user("gone", "gone@ourcompany.example", "x", is_active=False)
        self._campaign("orphan", 3, sender=gone)
        with patch("marketing.campaigns.GmailService") as gmail_cls:
            results = campaigns.check_all_replies()
        gmail_cls.assert_not_called()
        self.assertEqual(results[0]["result"], "skipped")


@override_settings(**BASE)
class MarkAcceptedTests(TestCase):
    """Marketing sets a lost enquiry to Accepted: same enquiry, back in the Enquiries portal."""

    def setUp(self):
        self.mk = User.objects.create_user("mk", "mk@ourcompany.example", "x", first_name="Meera")
        self.priya = User.objects.create_user("priya", "priya@ourcompany.example", "x",
                                              first_name="Priya", last_name="Sharma")
        self.client.force_login(self.mk)
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "customer_name": "Acme", "subject": "Seal",
                        "customer_reference": "", "assigned_to": "Priya Sharma", "remarks": "Old note",
                        "received_at_dt": timezone.now() - timedelta(days=90)}
        self.url = reverse("marketing:detail", args=["E1"])

    def _run(self, data=None, update_error=None, push=1):
        with patch("enquiries.services.fetch_enquiry", return_value=self.enquiry), \
                patch("enquiries.services.update_enquiry", side_effect=update_error, return_value={}) as update, \
                patch("marketing.campaigns.send_new_email") as send, \
                patch("push_notifications.services.send_push_to_users", return_value=push), \
                patch("marketing.notify.GmailService"), \
                patch("enquiries.services.list_enquiries", return_value=[]):
            response = self.client.post(self.url, {"action": "mark_accepted", **(data or {})}, follow=True)
        return response, update, send

    def test_sets_the_same_enquiry_to_accepted_and_notifies_sales(self):
        response, update, send = self._run({"accept_note": "Confirmed by phone"})
        args = update.call_args.args
        self.assertEqual(args[1], "E1")
        self.assertEqual(args[2]["status"], "accepted")
        self.assertTrue(args[2]["remarks"].startswith("Old note\nWon back by marketing on "))
        self.assertIn("Confirmed by phone", args[2]["remarks"])
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertIsNotNone(lead.accepted_at)
        self.assertEqual(lead.accepted_by, self.mk)
        self.assertEqual(send.call_args.args[1], "priya@ourcompany.example")
        self.assertIn("E1 accepted", send.call_args.args[2])
        self.assertContains(response, "Enquiry E1 is now Accepted")
        self.assertEqual(response.redirect_chain[0][0], reverse("marketing:board"))
        self.assertEqual(response.context["won_back_count"], 1)
        self.assertTrue(MarketingActivity.objects.filter(text="Priya Sharma was notified by phone and email.").exists())

    def test_enquiry_service_error_changes_nothing(self):
        from enquiries.services import EnquiryServiceError
        response, update, send = self._run(update_error=EnquiryServiceError("down"))
        self.assertContains(response, "Could not set the enquiry to Accepted")
        self.assertIsNone(MarketingLead.objects.get(enquiry_id="E1").accepted_at)
        send.assert_not_called()

    def test_revived_enquiry_cannot_also_be_accepted(self):
        MarketingLead.objects.create(enquiry_id="E1", revived_enquiry_id="E50")
        response, update, send = self._run()
        update.assert_not_called()
        self.assertContains(response, "already revived as E50")

    def test_unknown_salesperson_is_reported(self):
        self.enquiry["assigned_to"] = "Someone Else"
        response, update, send = self._run()
        update.assert_called_once()
        send.assert_not_called()
        self.assertContains(response, "please tell sales yourself")

    def test_detail_page_has_the_button(self):
        with patch("enquiries.services.fetch_enquiry", return_value=self.enquiry):
            page = self.client.get(self.url)
        self.assertContains(page, "Mark as accepted")
        self.assertContains(page, 'value="mark_accepted"')

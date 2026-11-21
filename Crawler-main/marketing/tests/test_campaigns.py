"""Tests for campaign emails: who gets them, test mode, sending, the last safety checks and replies."""
import base64
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries.models import CustomerStatusEmail, EnquiryContact
from marketing import campaigns, lost
from marketing.models import Campaign, CampaignRecipient, MarketingActivity, MarketingLead

BASE = dict(
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7,
    MARKETING_CAMPAIGN_MAX_RECIPIENTS=50, MARKETING_CAMPAIGN_GAP_DAYS=30, MARKETING_CAMPAIGN_DRY_RUN=True,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
)
LIVE = dict(BASE, MARKETING_CAMPAIGN_DRY_RUN=False)


def _rejected(enquiry_id, email, *, lost_days_ago=40, name=None):
    """A rejected enquiry, lost `lost_days_ago` days ago, with a saved customer email."""
    row = CustomerStatusEmail.objects.create(enquiry_id=enquiry_id, kind=CustomerStatusEmail.KIND_REJECTION,
                                             event_key="rejection")
    CustomerStatusEmail.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=lost_days_ago))
    if email:
        EnquiryContact.objects.create(enquiry_id=enquiry_id, customer_email=email)
    return {"enquiry_id": enquiry_id, "status": "rejected", "customer_name": name or f"Customer {enquiry_id}",
            "subject": f"Gasket {enquiry_id}", "received_at_dt": timezone.now() - timedelta(days=90)}


def _gmail(sent_ids=("msg-1", "thread-1")):
    gmail = MagicMock()
    gmail.authenticate.return_value = True
    gmail.service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": sent_ids[0], "threadId": sent_ids[1]}
    gmail.own_addresses.return_value = {"sales@ourcompany.example"}
    return gmail


@override_settings(**BASE)
class AudienceTests(TestCase):
    def test_render_email_fills_in_customer_and_adds_opt_out(self):
        subject, body = campaigns.render_email("Offer for {enquiry_subject}", "Dear {customer_name}, ref {reference}.",
                                               {"greeting_name": "Acme", "subject": "Seal", "enquiry_id": "E1"})
        self.assertEqual(subject, "Offer for Seal")
        self.assertTrue(body.startswith("Dear Acme, ref E1."))
        self.assertTrue(body.endswith("Reply STOP if you'd prefer not to hear from us."))
        self.assertIn("AVIGIRI Team", body)

    def test_greeting_never_uses_a_subject_or_domain(self):
        _, known = campaigns.render_email("S", "Dear {greeting}, from {customer_name}.", {"greeting_name": "Acme"})
        self.assertTrue(known.startswith("Dear Acme team, from Acme."))
        _, unknown = campaigns.render_email("S", "Dear {greeting}, from {customer_name}.",
                                            {"greeting_name": "", "customer_name": "acme.example"})
        self.assertTrue(unknown.startswith("Dear Sir/Madam, from your company."))
        for template in campaigns.STARTER_TEMPLATES.values():
            self.assertIn("Dear {greeting},", template["body"])

    def test_only_ready_customers_with_email(self):
        enquiries = [
            _rejected("READY", "ready@a.example"),
            _rejected("NOMAIL", ""),
            _rejected("DNC", "dnc@a.example"),
            _rejected("REV", "rev@a.example"),
        ]
        MarketingLead.objects.create(enquiry_id="DNC", do_not_contact=True)
        MarketingLead.objects.create(enquiry_id="REV", revived_enquiry_id="NEW")
        audience = campaigns.build_audience(lost.build_lost_leads(enquiries), {})
        self.assertEqual([r["email"] for r in audience["recipients"]], ["ready@a.example"])
        reasons = {s["row"]["enquiry_id"]: s["reason"] for s in audience["skipped"]}
        self.assertEqual(reasons["NOMAIL"], "no customer email saved")
        self.assertEqual(reasons["DNC"], "Do not contact")
        self.assertIn("revived", reasons["REV"])

    def test_do_not_contact_and_duplicates_work_by_email_address(self):
        enquiries = [_rejected("A1", "buyer@a.example"), _rejected("A2", "Buyer@a.example"),
                     _rejected("B1", "x@b.example"), _rejected("B2", "x@b.example")]
        MarketingLead.objects.create(enquiry_id="B2", do_not_contact=True)
        audience = campaigns.build_audience(lost.build_lost_leads(enquiries), {})
        self.assertEqual([r["email"] for r in audience["recipients"]], ["buyer@a.example"])
        reasons = {s["row"]["enquiry_id"]: s["reason"] for s in audience["skipped"]}
        self.assertIn("same email", [v for k, v in reasons.items() if k in ("A1", "A2")][0])
        self.assertIn("another enquiry", reasons["B1"])

    def test_recently_emailed_addresses_are_left_out(self):
        enquiries = [_rejected("E1", "buyer@a.example")]
        old = Campaign.objects.create(name="Spring", subject="s", body="b", status=Campaign.STATUS_SENT)
        CampaignRecipient.objects.create(campaign=old, customer_email="buyer@a.example", enquiry_id="E1",
                                         status=CampaignRecipient.STATUS_SENT, sent_at=timezone.now() - timedelta(days=3))
        audience = campaigns.build_audience(lost.build_lost_leads(enquiries), {})
        self.assertEqual(audience["recipients"], [])
        self.assertIn('campaign "Spring"', audience["skipped"][0]["reason"])

    def test_filters_narrow_the_audience(self):
        enquiries = [_rejected("P", "p@a.example"), _rejected("Q", "q@a.example")]
        MarketingLead.objects.create(enquiry_id="P", lost_reason="price")
        audience = campaigns.build_audience(lost.build_lost_leads(enquiries), {"reason": "price"})
        self.assertEqual([r["row"]["enquiry_id"] for r in audience["recipients"]], ["P"])


@override_settings(**BASE)
class SendTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("mk", "mk@ourcompany.example", "x")
        self.enquiries = [_rejected("E1", "buyer@acme.example", name="Acme"), _rejected("E2", "b@beta.example")]
        self.campaign = Campaign.objects.create(name="Price", subject="Offer for {enquiry_subject}",
                                                body="Dear {customer_name}, new prices.", created_by=self.user)

    def leads(self):
        return lost.build_lost_leads(self.enquiries)

    def test_test_mode_sends_nothing(self):
        with patch("marketing.campaigns.GmailService") as gmail_cls:
            counts = campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        gmail_cls.assert_not_called()
        self.assertEqual(counts["dry_run"], 2)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.STATUS_SENT)
        acme = self.campaign.recipients.get(enquiry_id="E1")
        self.assertEqual(acme.status, CampaignRecipient.STATUS_DRY_RUN)
        self.assertEqual(acme.subject, "Offer for Gasket E1")
        self.assertTrue(acme.body.startswith("Dear Acme, new prices."))

    @override_settings(**LIVE)
    def test_live_send(self):
        gmail = _gmail()
        with patch("marketing.campaigns.GmailService", return_value=gmail):
            counts = campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        self.assertEqual(counts["sent"], 2)
        send = gmail.service.users.return_value.messages.return_value.send
        raw = base64.urlsafe_b64decode(send.call_args_list[0].kwargs["body"]["raw"]).decode()
        self.assertIn("To: ", raw)
        self.assertNotIn("Re:", raw)
        sent = self.campaign.recipients.get(enquiry_id="E1")
        self.assertEqual((sent.status, sent.gmail_thread_id), (CampaignRecipient.STATUS_SENT, "thread-1"))
        self.assertTrue(MarketingActivity.objects.filter(lead__enquiry_id="E1", kind="email").exists())

    def test_list_changed_since_preview(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=5)
        self.assertEqual(self.campaign.recipients.count(), 0)

    @override_settings(MARKETING_CAMPAIGN_MAX_RECIPIENTS=1)
    def test_too_many_customers(self):
        with self.assertRaises(campaigns.CampaignError):
            campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)

    def test_cannot_send_twice(self):
        campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        with self.assertRaises(campaigns.CampaignError):  # sent, and test mode is still on
            campaigns.send_campaign(self.campaign, self.user, self.leads())

    @override_settings(**LIVE)
    def test_last_check_stops_do_not_contact(self):
        gmail = _gmail()
        original = campaigns._still_allowed

        def mark_then_check(enquiry_id, address, campaign_id=None):
            if enquiry_id == "E2":
                MarketingLead.objects.update_or_create(enquiry_id="E2", defaults={"do_not_contact": True})
            return original(enquiry_id, address, campaign_id)

        with patch("marketing.campaigns.GmailService", return_value=gmail), \
                patch("marketing.campaigns._still_allowed", side_effect=mark_then_check):
            counts = campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        self.assertEqual((counts["sent"], counts["skipped"]), (1, 1))
        self.assertIn("Do not contact", self.campaign.recipients.get(enquiry_id="E2").note)

    @override_settings(**LIVE)
    def test_gmail_sign_in_failure_marks_failed_and_retry_sends(self):
        broken = _gmail()
        broken.authenticate.return_value = False
        with patch("marketing.campaigns.GmailService", return_value=broken):
            with self.assertRaises(campaigns.CampaignError):
                campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        self.assertEqual(self.campaign.recipients.filter(status=CampaignRecipient.STATUS_FAILED).count(), 2)
        self.campaign.refresh_from_db()
        with patch("marketing.campaigns.GmailService", return_value=_gmail()):
            counts = campaigns.send_campaign(self.campaign, self.user, [])
        self.assertEqual(counts["sent"], 2)

    @override_settings(**LIVE)
    def test_retry_skips_address_another_campaign_just_emailed(self):
        with override_settings(MARKETING_CAMPAIGN_DRY_RUN=True):
            campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        other = Campaign.objects.create(name="Other", subject="s", body="b", status=Campaign.STATUS_SENT)
        CampaignRecipient.objects.create(campaign=other, customer_email="buyer@acme.example", enquiry_id="E1",
                                         status=CampaignRecipient.STATUS_SENT, sent_at=timezone.now())
        with patch("marketing.campaigns.GmailService", return_value=_gmail()):
            counts = campaigns.send_campaign(self.campaign, self.user, [])
        self.assertEqual((counts["sent"], counts["skipped"]), (1, 1))
        self.assertIn("another campaign", self.campaign.recipients.get(enquiry_id="E1").note)

    @override_settings(**LIVE)
    def test_check_replies_and_unsubscribe(self):
        with patch("marketing.campaigns.GmailService", return_value=_gmail()):
            campaigns.send_campaign(self.campaign, self.user, self.leads(), expected_count=2)
        CampaignRecipient.objects.filter(enquiry_id="E2").update(gmail_thread_id="thread-2")
        gmail = _gmail()
        threads = {
            "thread-1": [{"sender": "AVIGIRI <sales@ourcompany.example>", "snippet": "Offer"},
                         {"sender": "Rahul <buyer@acme.example>", "snippet": "Please send a quote"}],
            "thread-2": [{"sender": "sales@ourcompany.example", "snippet": "Offer"},
                         {"sender": "b@beta.example", "snippet": "Unsubscribe please"}],
        }
        gmail.get_thread_messages.side_effect = lambda thread_id, limit=50: threads[thread_id]
        with patch("marketing.campaigns.GmailService", return_value=gmail):
            counts = campaigns.check_replies(self.campaign, self.user)
        self.assertEqual((counts["replied"], counts["unsubscribed"]), (2, 1))
        self.assertIsNotNone(self.campaign.recipients.get(enquiry_id="E1").replied_at)
        self.assertTrue(MarketingLead.objects.get(enquiry_id="E2").do_not_contact)
        self.assertFalse(MarketingLead.objects.get(enquiry_id="E1").do_not_contact)


@override_settings(**BASE)
class CampaignPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("mk", "mk@ourcompany.example", "x")
        self.client.force_login(self.user)
        self.enquiries = [_rejected("E1", "buyer@acme.example", name="Acme")]

    def _list(self):
        return patch("enquiries.services.list_enquiries", return_value=self.enquiries)

    def test_new_campaign_uses_list_filters(self):
        response = self.client.get(reverse("marketing:campaign_new"), {"reason": "price", "stage": "ready"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["reason"], "price")
        self.assertNotIn("stage", response.context["form"].initial)

    def test_create_preview_and_send_in_test_mode(self):
        response = self.client.post(reverse("marketing:campaign_new"), {
            "name": "Checking in", "subject": "About {enquiry_subject}",
            "body": "Dear {customer_name}, do you have a new requirement for us?", "kind": "", "reason": ""})
        campaign = Campaign.objects.get()
        self.assertRedirects(response, reverse("marketing:campaign_detail", args=[campaign.pk]),
                             fetch_redirect_response=False)
        with self._list():
            page = self.client.get(reverse("marketing:campaign_detail", args=[campaign.pk]))
        self.assertContains(page, "buyer@acme.example")
        self.assertContains(page, "About Gasket E1")
        self.assertContains(page, "Test mode is on")
        with self._list():
            self.client.post(reverse("marketing:campaign_send", args=[campaign.pk]), {"expected_count": "1"})
        self.assertEqual(campaign.recipients.get().status, CampaignRecipient.STATUS_DRY_RUN)
        page = self.client.get(reverse("marketing:campaign_detail", args=[campaign.pk]))
        self.assertContains(page, "Test mode (not sent)")
        lead_page_campaigns = CampaignRecipient.objects.filter(enquiry_id="E1").count()
        self.assertEqual(lead_page_campaigns, 1)

    def test_send_without_checking_the_list_is_refused(self):
        campaign = Campaign.objects.create(name="X", subject="S", body="B" * 30)
        with self._list(), patch("marketing.campaigns.send_campaign") as send:
            self.client.post(reverse("marketing:campaign_send", args=[campaign.pk]))
        send.assert_not_called()

    def test_sent_campaign_cannot_be_edited_or_deleted(self):
        campaign = Campaign.objects.create(name="X", subject="S", body="B" * 30, status=Campaign.STATUS_SENT)
        self.assertRedirects(self.client.get(reverse("marketing:campaign_edit", args=[campaign.pk])),
                             reverse("marketing:campaign_detail", args=[campaign.pk]), fetch_redirect_response=False)
        self.client.post(reverse("marketing:campaign_delete", args=[campaign.pk]))
        self.assertTrue(Campaign.objects.filter(pk=campaign.pk).exists())

    def test_delete_draft(self):
        campaign = Campaign.objects.create(name="X", subject="S", body="B" * 30)
        self.client.post(reverse("marketing:campaign_delete", args=[campaign.pk]))
        self.assertFalse(Campaign.objects.exists())

    def test_campaign_list_page(self):
        Campaign.objects.create(name="Spring offer", subject="S", body="B" * 30)
        response = self.client.get(reverse("marketing:campaign_list"))
        self.assertContains(response, "Spring offer")


@override_settings(**LIVE)
class CampaignCcTests(TestCase):
    def test_other_customer_emails_are_ccd_except_do_not_contact(self):
        user = get_user_model().objects.create_user("mk", "mk@ourcompany.example", "x")
        enquiries = [_rejected("E1", "buyer@acme.example", name="Acme"), _rejected("E2", "boss@acme.example")]
        EnquiryContact.objects.filter(enquiry_id="E1").update(
            other_customer_emails="boss@acme.example, cto@acme.example")
        MarketingLead.objects.create(enquiry_id="E2", do_not_contact=True)  # boss@ asked not to be emailed
        leads = lost.build_lost_leads(enquiries)
        audience = campaigns.build_audience(leads, {})
        self.assertEqual([(r["email"], r["cc"]) for r in audience["recipients"]],
                         [("buyer@acme.example", ["cto@acme.example"])])
        campaign = Campaign.objects.create(name="C", subject="S", body="Dear {greeting}, hello there friends.")
        gmail = _gmail()
        with patch("marketing.campaigns.GmailService", return_value=gmail):
            campaigns.send_campaign(campaign, user, leads, expected_count=1)
        send = gmail.service.users.return_value.messages.return_value.send
        raw = base64.urlsafe_b64decode(send.call_args.kwargs["body"]["raw"]).decode()
        self.assertIn("Cc: cto@acme.example", raw)
        self.assertNotIn("boss@acme.example", raw)
        self.assertEqual(campaign.recipients.get().cc, "cto@acme.example")

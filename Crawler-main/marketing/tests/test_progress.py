"""The Marketing board says plainly which lost enquiries are completed and which still need work."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries.models import FollowUpStart
from marketing import lost
from marketing.models import MarketingActivity, MarketingLead


class LeadProgressTests(SimpleTestCase):
    def progress(self, stage=lost.STAGE_READY, revived="", flag="", state=None, label=""):
        auto = {"state": state, "label": label} if state else None
        return lost.lead_progress(stage, revived, flag, auto)

    def test_completed(self):
        won = self.progress(lost.STAGE_REVIVED, revived="E2")
        self.assertEqual((won["key"], won["detail"]), ("done", "Won back: new enquiry E2"))
        self.assertEqual(self.progress(lost.STAGE_DO_NOT_CONTACT)["key"], "done")
        self.assertEqual(self.progress(state="closed")["detail"], "All follow-ups sent, no reply")
        self.assertEqual(self.progress(state="stopped", label="Stopped: email bounced", flag="")["key"], "done")

    def test_in_progress(self):
        running = self.progress(state="running", label="2 of 4 sent, next 19 Nov")
        self.assertEqual((running["key"], running["label"]), ("running", "In progress"))
        self.assertEqual(running["detail"], "Follow-ups: 2 of 4 sent, next 19 Nov")

    def test_needs_action(self):
        self.assertEqual(self.progress(state="replied")["detail"], "Customer replied: call or email them")
        self.assertEqual(self.progress(flag=MarketingLead.FLAG_BOUNCED, state="stopped")["key"], "action")
        self.assertEqual(self.progress(flag=MarketingLead.FLAG_THREAD_NOT_FOUND, state="running")["key"], "action")
        self.assertEqual(self.progress()["detail"], "Not contacted yet")


@override_settings(MARKETING_FOLLOW_UP_COUNT=4, FOLLOW_UP_AUTO_GAP_DAYS=[2],
                   STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class BoardShowsProgressTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("mk", "mk@example.com", "x"))
        old = timezone.now() - timedelta(days=40)
        enquiries = [{"enquiry_id": eid, "status": "rejected", "subject": "Gaskets", "customer_name": name,
                      "remarks": "", "received_at_dt": old}
                     for eid, name in (("E1", "Running Co"), ("E2", "Won Co"), ("E3", "Quiet Co"))]
        patch("enquiries.services.list_enquiries", return_value=enquiries).start()
        self.addCleanup(patch.stopall)
        FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                     customer_email="a@running.example", started_at=timezone.now())
        MarketingLead.objects.create(enquiry_id="E2", revived_enquiry_id="E9")

    def test_each_row_and_the_cards_show_progress(self):
        page = self.client.get(reverse("marketing:board"))
        rows = {r["enquiry_id"]: r["progress"] for r in page.context["rows"]}
        self.assertEqual(rows, {"E1": "running", "E2": "done", "E3": "action"})
        summary = page.context["summary"]
        self.assertEqual((summary["action"], summary["running"], summary["done"]), (1, 1, 1))
        self.assertContains(page, "data-progress-cards")
        self.assertContains(page, 'data-progress="done"')
        self.assertContains(page, "Not contacted yet")

    def test_filter_by_progress(self):
        page = self.client.get(reverse("marketing:board"), {"progress": "done"})
        self.assertEqual([r["enquiry_id"] for r in page.context["rows"]], ["E2"])
        # The campaign button never carries the progress filter (a campaign's audience is its own).
        self.assertNotIn("progress", page.context["campaign_query"])

    def test_customer_reply_needs_action(self):
        lead = MarketingLead.objects.create(enquiry_id="E1")
        MarketingActivity.objects.create(lead=lead, kind="system", text=lost.REPLY_NOTE + ".")
        page = self.client.get(reverse("marketing:board"), {"progress": "action"})
        self.assertEqual(sorted(r["enquiry_id"] for r in page.context["rows"]), ["E1", "E3"])

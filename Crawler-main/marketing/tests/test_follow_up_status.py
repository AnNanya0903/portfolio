"""The Marketing portal shows where each rejected enquiry's automatic follow-ups stand."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries.models import FollowUpSent, FollowUpStart
from marketing import lost
from marketing.models import MarketingActivity, MarketingLead

SETTINGS = dict(MARKETING_FOLLOW_UP_COUNT=4, FOLLOW_UP_AUTO_GAP_DAYS=[2], MARKETING_WAIT_DAYS=30,
                STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
NEXT = date(2026, 11, 19)


def cycle(count, next_on=NEXT):
    return SimpleNamespace(stage="marketing", count=count, next_on=next_on)


@override_settings(**SETTINGS)
class StatusLabelTests(TestCase):
    def status(self, count, lead=None, replied=False, kind="rejected", stage="marketing", next_on=NEXT):
        c = cycle(count, next_on)
        c.stage = stage
        return lost.auto_follow_up_status(kind, lead, c, replied)

    def test_progress_labels(self):
        self.assertEqual(self.status(0)["label"], "First follow-up 19 Nov")
        self.assertEqual(self.status(2)["label"], "2 of 4 sent, next 19 Nov")
        self.assertEqual(self.status(4)["label"], "4 of 4 sent, closing email 19 Nov")
        done = self.status(5, next_on=None)
        self.assertEqual((done["state"], done["label"]), ("closed", "Closing email sent"))
        self.assertEqual(done["sent"], 4)

    def test_stops_show_why(self):
        self.assertEqual(self.status(1, MarketingLead(enquiry_id="E", do_not_contact=True))["label"],
                         "Stopped: do not contact")
        self.assertEqual(self.status(1, MarketingLead(enquiry_id="E", revived_enquiry_id="E2"))["label"],
                         "Stopped: won back")
        replied = self.status(1, replied=True)
        self.assertEqual((replied["state"], replied["next_on"]), ("replied", None))

    def test_only_rejected_enquiries_with_a_marketing_sequence(self):
        self.assertIsNone(self.status(1, kind="no_response"))
        self.assertIsNone(self.status(1, stage="waiting_po"))
        self.assertIsNone(lost.auto_follow_up_status("rejected", None, None, False))


@override_settings(**SETTINGS)
class PortalShowsProgressTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("mk", "mk@example.com", "x"))
        self.enquiry = {"enquiry_id": "E1", "status": "rejected", "subject": "Gaskets", "customer_name": "Acme",
                        "customer_reference": "", "remarks": "", "received_at_dt": timezone.now() - timedelta(days=40)}
        for name, value in (("list_enquiries", [self.enquiry]), ("fetch_enquiry", self.enquiry)):
            patch(f"enquiries.services.{name}", return_value=value).start()
        self.addCleanup(patch.stopall)
        self.start = FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                                  customer_email="buyer@acme.example",
                                                  started_at=timezone.now() - timedelta(days=1))

    def _pages(self):
        return (self.client.get(reverse("marketing:board")), self.client.get(reverse("marketing:detail", args=["E1"])))

    def test_list_and_detail_show_the_first_follow_up(self):
        board, detail = self._pages()
        self.assertContains(board, 'data-auto-follow-up="running"')
        self.assertContains(board, "Auto: First follow-up")
        self.assertContains(detail, "Automatic follow-ups")
        self.assertContains(detail, "separate from the enquiry")

    def test_progress_after_two_emails_and_after_a_reply(self):
        FollowUpStart.objects.update(started_at=timezone.now() - timedelta(days=6))  # the rejection, 6 days ago
        for days in (4, 2):
            FollowUpSent.objects.create(enquiry_id="E1", customer_email="buyer@acme.example",
                                        sent_by=get_user_model().objects.first(), status="sent",
                                        gmail_thread_id="TM", stage="marketing", source="auto",
                                        sent_at=timezone.now() - timedelta(days=days),
                                        next_follow_up_on=timezone.localdate() + timedelta(days=1))
        board, _ = self._pages()
        self.assertContains(board, "Auto: 2 of 4 sent, next")
        lead = MarketingLead.objects.create(enquiry_id="E1")
        MarketingActivity.objects.create(lead=lead, kind="system", text=lost.REPLY_NOTE + ": automatic follow-ups stopped.")
        board, detail = self._pages()
        self.assertContains(board, 'data-auto-follow-up="replied"')
        self.assertContains(detail, "Customer replied: follow-ups stopped")

    def test_a_lost_enquiry_without_a_marketing_sequence_shows_nothing(self):
        FollowUpStart.objects.all().delete()
        board, detail = self._pages()
        self.assertNotContains(board, "data-auto-follow-up")
        self.assertNotContains(detail, "Automatic follow-ups")

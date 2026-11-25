"""The Won back tab lists every customer who came back: accepted again, or revived as a new enquiry."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from marketing.models import MarketingLead


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class WonBackTabTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("mk", "mk@example.com", "x", first_name="Kumar")
        self.client.force_login(self.user)
        old = timezone.now() - timedelta(days=40)
        enquiries = [
            {"enquiry_id": "E1", "status": "accepted", "customer_name": "Acme Seals", "subject": "Gaskets",
             "remarks": "", "received_at_dt": old},
            {"enquiry_id": "E2", "status": "rejected", "customer_name": "Beta Rubber", "subject": "O-rings",
             "remarks": "", "received_at_dt": old},
            {"enquiry_id": "E9", "status": "converted", "customer_name": "Beta Rubber", "subject": "O-rings again",
             "remarks": "", "received_at_dt": timezone.now()},
            {"enquiry_id": "E3", "status": "rejected", "customer_name": "Still Lost", "subject": "Foam",
             "remarks": "", "received_at_dt": old},
        ]
        patch("enquiries.services.list_enquiries", return_value=enquiries).start()
        self.addCleanup(patch.stopall)
        MarketingLead.objects.create(enquiry_id="E1", lead_source="rejected", lost_reason="price",
                                     accepted_at=timezone.now() - timedelta(days=2), accepted_by=self.user)
        MarketingLead.objects.create(enquiry_id="E2", lead_source="no_response", revived_enquiry_id="E9",
                                     revived_at=timezone.now() - timedelta(days=1), revived_by=self.user)
        MarketingLead.objects.create(enquiry_id="E3", do_not_contact=True)

    def test_lists_who_came_back_and_how(self):
        page = self.client.get(reverse("marketing:won_back"))
        self.assertEqual(page.status_code, 200)
        rows = page.context["rows"]
        self.assertEqual([r["enquiry_id"] for r in rows], ["E2", "E1"])  # newest first, the lost one is not here
        beta, acme = rows
        self.assertEqual((beta["how"], beta["how_label"], beta["status_now"], beta["reason_label"]),
                         ("revived", "New enquiry E9", "Converted", "Never replied"))
        self.assertEqual((acme["how"], acme["status_now"], acme["reason_label"], acme["won_by"]),
                         ("accepted", "Accepted", "Price too high", "Kumar"))
        self.assertContains(page, "Acme Seals")
        self.assertContains(page, "Beta Rubber")
        self.assertNotContains(page, "Still Lost")
        self.assertEqual((page.context["accepted_count"], page.context["revived_count"]), (1, 1))

    def test_board_card_links_to_the_tab(self):
        page = self.client.get(reverse("marketing:board"))
        self.assertEqual(page.context["won_back_count"], 2)
        self.assertContains(page, "data-won-back-card")
        self.assertContains(page, reverse("marketing:won_back"))

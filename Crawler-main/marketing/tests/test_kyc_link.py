"""Marketing and KYC connected: leads show their KYC customer and use its contacts; the KYC page shows Marketing."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries.models import EnquiryContact
from kyc.models import Customer, CustomerContact, EnquiryCustomer
from marketing import campaigns, lost
from marketing.models import Campaign, CampaignRecipient, MarketingLead

SETTINGS = dict(
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7, MARKETING_WAIT_DAYS=30,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
)


def _enquiry(enquiry_id, status="rejected", **extra):
    data = {"enquiry_id": enquiry_id, "status": status, "customer_name": "", "subject": f"Gasket {enquiry_id}",
            "customer_reference": "", "remarks": "", "received_at_dt": timezone.now() - timedelta(days=90)}
    data.update(extra)
    return data


@override_settings(**SETTINGS)
class MarketingUsesKycTests(TestCase):
    def setUp(self):
        self.acme = Customer.objects.create(code="AV-0001", name="Acme Seals")
        CustomerContact.objects.create(customer=self.acme, category="sales_marketing", name="Meera",
                                       email="Meera@Acme.example")
        CustomerContact.objects.create(customer=self.acme, category="accounts", name="Ravi",
                                       email="accounts@acme.example")
        CustomerContact.objects.create(customer=self.acme, category="technical", name="Tej",
                                       email="tech@acme.example", is_purchaser=True)
        EnquiryCustomer.objects.create(enquiry_id="E1", customer=self.acme)

    def test_lead_gets_code_name_and_sales_contacts(self):
        row = lost.build_lost_leads([_enquiry("E1"), _enquiry("E2")])
        rows = {r["enquiry_id"]: r for r in row}
        self.assertEqual(rows["E1"]["kyc_code"], "AV-0001")
        self.assertEqual((rows["E1"]["customer_name"], rows["E1"]["customer_name_source"]), ("Acme Seals", "kyc"))
        # No saved email: the purchaser becomes the main address, the Sales team is CC'd (not Accounts).
        self.assertEqual(rows["E1"]["customer_email"], "tech@acme.example")
        self.assertEqual(rows["E1"]["other_emails"], ["meera@acme.example"])
        self.assertEqual(rows["E2"]["kyc_code"], "")

    def test_saved_email_and_enquiry_name_still_win(self):
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example")
        row = lost.build_lost_leads([_enquiry("E1", customer_name="Acme Seals Pvt Ltd")])[0]
        self.assertEqual(row["customer_email"], "buyer@acme.example")
        self.assertEqual(row["customer_name"], "Acme Seals Pvt Ltd")
        self.assertEqual(row["other_emails"], ["tech@acme.example", "meera@acme.example"])

    def test_campaign_audience_ccs_the_kyc_contacts(self):
        rows = lost.build_lost_leads([_enquiry("E1")])
        audience = campaigns.build_audience(rows, {})
        item = next(r for r in audience["recipients"] if r["row"]["enquiry_id"] == "E1")
        self.assertEqual((item["email"], item["cc"]), ("tech@acme.example", ["meera@acme.example"]))

    def test_do_not_contact_on_another_lead_blocks_a_kyc_contact(self):
        EnquiryCustomer.objects.create(enquiry_id="E2", customer=Customer.objects.create(code="BW-1", name="Bolt"))
        EnquiryContact.objects.create(enquiry_id="E2", customer_email="meera@acme.example")
        MarketingLead.objects.create(enquiry_id="E2", do_not_contact=True)
        rows = lost.build_lost_leads([_enquiry("E1"), _enquiry("E2")])
        item = next(r for r in campaigns.build_audience(rows, {})["recipients"] if r["row"]["enquiry_id"] == "E1")
        self.assertEqual(item["cc"], [])  # meera is Do not contact, so she is not CC'd

    def test_contact_with_several_emails(self):
        CustomerContact.objects.filter(name="Meera").update(email="meera@acme.example, Sales@Acme.example")
        row = lost.build_lost_leads([_enquiry("E1")])[0]
        self.assertEqual(row["customer_email"], "tech@acme.example")
        self.assertEqual(row["other_emails"], ["meera@acme.example", "sales@acme.example"])

    def test_search_finds_the_kyc_code(self):
        rows, _ = lost.filter_leads(lost.build_lost_leads([_enquiry("E1"), _enquiry("E2")]), {"q": "av-0001"})
        self.assertEqual([r["enquiry_id"] for r in rows], ["E1"])


@override_settings(**SETTINGS)
class PagesShowTheLinkTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("mk", "mk@example.com", "x"))
        self.acme = Customer.objects.create(code="AV-0001", name="Acme Seals")
        EnquiryCustomer.objects.create(enquiry_id="E1", customer=self.acme)

    def test_marketing_board_and_detail_show_the_code(self):
        enquiry = _enquiry("E1")
        with patch("enquiries.services.list_enquiries", return_value=[enquiry]), \
             patch("enquiries.services.fetch_enquiry", return_value=enquiry):
            board = self.client.get(reverse("marketing:board"))
            detail = self.client.get(reverse("marketing:detail", args=["E1"]))
        kyc_url = reverse("kyc:detail", args=[self.acme.pk])
        self.assertContains(board, f'href="{kyc_url}"')
        self.assertContains(board, "AV-0001")
        self.assertContains(detail, "AV-0001</a> · Acme Seals")

    def test_kyc_page_shows_marketing_activity(self):
        MarketingLead.objects.create(enquiry_id="E1", lost_reason="price")
        campaign = Campaign.objects.create(name="Diwali offer", subject="S", body="B", status=Campaign.STATUS_SENT)
        CampaignRecipient.objects.create(campaign=campaign, customer_email="meera@acme.example", enquiry_id="E1",
                                         status=CampaignRecipient.STATUS_SENT, sent_at=timezone.now())
        CampaignRecipient.objects.create(campaign=campaign, customer_email="x@other.example", enquiry_id="E9",
                                         status=CampaignRecipient.STATUS_SENT)
        response = self.client.get(reverse("kyc:detail", args=[self.acme.pk]))
        self.assertContains(response, "Lost enquiry #E1")
        self.assertContains(response, "Price too high")
        self.assertContains(response, 'Campaign "Diwali offer"')
        self.assertContains(response, "meera@acme.example")
        self.assertNotContains(response, "x@other.example")

    def test_kyc_page_without_marketing(self):
        response = self.client.get(reverse("kyc:detail", args=[self.acme.pk]))
        self.assertContains(response, "No marketing activity for this customer")

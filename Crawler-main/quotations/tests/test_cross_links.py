"""Quotations, enquiries, KYC and Marketing link to each other, and the menu lists Quotations above Enquiries."""
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from dashboard.models import UserServiceAccess
from kyc.models import Customer
from quotations import links
from quotations.models import Quotation

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
User = get_user_model()


@override_settings(**STATIC)
class CrossLinkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("boss", "boss@example.com", "x", is_superuser=True)
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals")
        self.q1 = Quotation.objects.create(customer=self.customer, customer_name="Acme Seals", enquiry_id="ENQ-7",
                                           title="Gasket quote")
        self.q2 = Quotation.objects.create(customer_name="Other Ltd", enquiry_id="ENQ-8", title="Other quote")

    def test_the_menu_lists_quotations_above_enquiries(self):
        html = self.client.get(reverse("quotations:list")).content.decode()
        self.assertLess(html.index('href="/quotations/"'), html.index('href="/enquiries/"'))
        self.assertLess(html.index('href="/enquiries/"'), html.index('href="/marketing/"'))
        self.assertLess(html.index('href="/marketing/"'), html.index('href="/kyc/"'))

    def test_a_quotation_links_to_its_enquiry_and_its_kyc_customer(self):
        page = self.client.get(reverse("quotations:detail", args=[self.q1.pk]))
        self.assertContains(page, f'href="{reverse("enquiries:detail", args=["ENQ-7"])}"')
        self.assertContains(page, f'href="{reverse("kyc:detail", args=[self.customer.pk])}"')
        listing = self.client.get(reverse("quotations:list"))
        self.assertContains(listing, f'href="{reverse("enquiries:detail", args=["ENQ-7"])}"')
        self.assertContains(listing, f'href="{reverse("kyc:detail", args=[self.customer.pk])}"')

    def test_no_link_to_a_module_the_user_cannot_open(self):
        limited = User.objects.create_user("limited", "l@example.com", "x")
        UserServiceAccess.objects.create(user=limited, restrict=True, allowed_services=["quotations"])
        self.client.force_login(limited)
        page = self.client.get(reverse("quotations:detail", args=[self.q1.pk]))
        self.assertNotContains(page, f'href="{reverse("enquiries:detail", args=["ENQ-7"])}"')
        self.assertNotContains(page, f'href="{reverse("kyc:detail", args=[self.customer.pk])}"')
        self.assertContains(page, "Enquiry #ENQ-7")

    def test_the_kyc_customer_page_lists_their_quotations(self):
        page = self.client.get(reverse("kyc:detail", args=[self.customer.pk]))
        self.assertContains(page, "Quotations")
        self.assertContains(page, f'href="{reverse("quotations:detail", args=[self.q1.pk])}"')
        self.assertContains(page, "?customer=AV-0001")
        self.assertNotContains(page, self.q2.number)  # another customer's quotation

    def test_helpers(self):
        self.assertEqual(links.counts_for_enquiries(["ENQ-7", "ENQ-8", "ENQ-9", ""]), {"ENQ-7": 1, "ENQ-8": 1})
        self.assertEqual(links.counts_for_enquiries([]), {})
        self.assertEqual([q.pk for q in links.for_enquiry("ENQ-7")], [self.q1.pk])
        self.assertEqual([q.pk for q in links.for_customer(self.customer)], [self.q1.pk])

    @mock.patch("enquiries.negotiation_views.services.fetch_enquiry", return_value={"enquiry_id": "ENQ-7", "customer_name": "Acme", "status": "open"})
    @mock.patch("enquiries.views._get_api_root", return_value="http://svc")
    def test_the_negotiation_page_lists_the_enquiry_quotations(self, *_):
        page = self.client.get(reverse("enquiries:negotiation", args=["ENQ-7"]))
        self.assertContains(page, f'href="{reverse("quotations:detail", args=[self.q1.pk])}"')
        self.assertContains(page, "?enquiry=ENQ-7")

    @mock.patch("enquiries.views.services.list_enquiries", return_value=[
        {"enquiry_id": "ENQ-7", "status": "open", "customer_name": "Acme Seals", "subject": "S"},
        {"enquiry_id": "ENQ-9", "status": "open", "customer_name": "New Co", "subject": "S2"}])
    def test_the_enquiry_board_shows_quotations_or_a_new_quotation_link(self, *_):
        page = self.client.get(reverse("enquiries:board"))
        html = page.content.decode()
        rows = {e["enquiry_id"]: e for e in page.context["enquiries"]}
        self.assertEqual((rows["ENQ-7"]["quotation_count"], rows["ENQ-9"]["quotation_count"]), (1, 0))
        self.assertIn("Quotations (1)", html)
        self.assertIn(reverse("quotations:list") + "?q=ENQ-7", html)
        self.assertIn(reverse("quotations:new") + "?enquiry=ENQ-9", html)

    def test_new_quotation_is_prefilled_from_the_link(self):
        page = self.client.get(reverse("quotations:new") + "?enquiry=ENQ-9&customer=AV-0001&customer_name=New+Co")
        self.assertContains(page, 'value="ENQ-9"')
        self.assertContains(page, 'value="AV-0001"')

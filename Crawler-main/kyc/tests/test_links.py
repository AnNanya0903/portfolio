"""Tests for linking enquiries to KYC customers: the picker search, the board forms and the customer page."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from enquiries.models import EnquiryContact
from kyc import links
from kyc.models import Customer, CustomerContact, EnquiryCustomer

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)
        self.acme = Customer.objects.create(code="AV-0001", name="Acme Seals", city="Bengaluru")
        CustomerContact.objects.create(customer=self.acme, category="accounts", name="Ravi",
                                       email="ravi@acme.example")
        CustomerContact.objects.create(customer=self.acme, category="sales_marketing", name="Meera",
                                       email="meera@acme.example", is_purchaser=True)
        self.bolt = Customer.objects.create(code="BW-0001", name="Bolt Works")


class LinkHelperTests(Base):
    def test_contact_email_prefers_the_purchaser_then_sales(self):
        self.assertEqual(links.contact_email(self.acme), "meera@acme.example")
        self.acme.contacts.filter(is_purchaser=True).update(is_purchaser=False)
        self.assertEqual(links.contact_email(self.acme), "meera@acme.example")  # still the sales contact
        self.acme.contacts.filter(category="sales_marketing").delete()
        self.assertEqual(links.contact_email(self.acme), "")  # accounts is never used for enquiry emails
        self.assertEqual(links.contact_email(self.bolt), "")

    def test_set_link_replaces_and_removes(self):
        links.set_link("E1", self.acme, self.user)
        links.set_link("E1", self.bolt, self.user)
        self.assertEqual(links.customers_for(["E1", "E2"]), {"E1": self.bolt})
        links.set_link("E1", None)
        self.assertEqual(EnquiryCustomer.objects.count(), 0)
        links.set_link("", self.acme)
        self.assertEqual(EnquiryCustomer.objects.count(), 0)

    def test_code_lookup_ignores_case_and_spaces(self):
        self.assertEqual(links.customer_for_code(" av - 0001 "), self.acme)
        self.assertIsNone(links.customer_for_code("NOPE"))
        self.assertIsNone(links.customer_for_code(""))

    def test_suggestions_need_exactly_one_customer_with_that_name(self):
        Customer.objects.create(code="BW-0002", name="bolt works")
        found = links.suggestions_for(["  ACME   seals ", "Bolt Works", "", None, "Unknown"])
        self.assertEqual(found, {"acme seals": self.acme})


@override_settings(**BASE)
class SearchApiTests(Base):
    def test_search_by_code_name_and_email(self):
        url = reverse("kyc:customer_search")
        for q, codes in (("av-00", ["AV-0001"]), ("bolt", ["BW-0001"]), ("meera@", ["AV-0001"]),
                         ("", ["AV-0001", "BW-0001"]), ("zzz", [])):
            data = self.client.get(url, {"q": q}).json()
            self.assertEqual([c["code"] for c in data["customers"]], codes, q)
        first = self.client.get(url, {"q": "AV"}).json()["customers"][0]
        self.assertEqual(first, {"code": "AV-0001", "name": "Acme Seals", "city": "Bengaluru", "gstin": "",
                                 "email": "meera@acme.example"})

    def test_enquiry_users_may_search_but_others_may_not(self):
        url = reverse("kyc:customer_search")
        access = UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["enquiries"])
        self.assertEqual(self.client.get(url).status_code, 200)
        access.allowed_services = ["mrp"]
        access.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 403)  # signed out: refused, nothing is listed


def _enquiry(enquiry_id, name, **extra):
    data = {"enquiry_id": enquiry_id, "customer_name": name, "subject": f"Seal {enquiry_id}", "status": "open"}
    data.update(extra)
    return data


@override_settings(**BASE)
class BoardTests(Base):
    def _post(self, **data):
        with patch("enquiries.views.services.fetch_board", return_value={}), \
                patch("enquiries.views.services.list_enquiries", return_value=[]), \
                patch("enquiries.views.services.update_enquiry") as update:
            response = self.client.post(reverse("enquiries:board"),
                                        dict({"action": "update_customer", "enquiry_id": "E1"}, **data), follow=True)
        return response, update

    def test_picking_a_code_fills_name_and_email_and_links_the_enquiry(self):
        response, update = self._post(customer_code="av-0001", customer_name="", customer_email="")
        self.assertEqual(update.call_args.args[2], {"customer_name": "Acme Seals"})
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="E1").customer_email, "meera@acme.example")
        self.assertEqual(links.customers_for(["E1"]), {"E1": self.acme})
        self.assertContains(response, "customer code AV-0001")

    def test_typed_name_and_email_win_over_the_kyc_values(self):
        _, update = self._post(customer_code="AV-0001", customer_name="Acme (Pune plant)",
                               customer_email="pune@acme.example")
        self.assertEqual(update.call_args.args[2], {"customer_name": "Acme (Pune plant)"})
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="E1").customer_email, "pune@acme.example")
        self.assertEqual(links.customers_for(["E1"]), {"E1": self.acme})

    def test_unknown_code_changes_nothing(self):
        links.set_link("E1", self.bolt)
        response, update = self._post(customer_code="NOPE-1", customer_name="Acme")
        update.assert_not_called()
        self.assertContains(response, "NOPE-1 is not in KYC")
        self.assertEqual(links.customers_for(["E1"]), {"E1": self.bolt})

    def test_emptied_code_removes_the_link_but_a_form_without_the_field_keeps_it(self):
        links.set_link("E1", self.acme)
        self._post(customer_name="Acme Seals")  # an older form without the code field
        self.assertEqual(links.customers_for(["E1"]), {"E1": self.acme})
        self._post(customer_name="Acme Seals", customer_code="")
        self.assertEqual(links.customers_for(["E1"]), {})

    def test_board_shows_the_code_and_offers_a_link_for_a_matching_name(self):
        links.set_link("E1", self.acme)
        rows = [_enquiry("E1", "Acme Seals"), _enquiry("E2", "bolt works"), _enquiry("E3", "Someone Else")]
        with patch("enquiries.views.services.fetch_board", return_value={}), \
                patch("enquiries.views.services.list_enquiries", return_value=rows):
            response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        by_id = {e["enquiry_id"]: e for e in response.context["enquiries"]}
        self.assertEqual(by_id["E1"]["kyc_customer"], self.acme)
        self.assertEqual(by_id["E2"]["kyc_suggestion"], self.bolt)
        self.assertIsNone(by_id["E3"].get("kyc_suggestion"))
        self.assertContains(response, "Link to BW-0001")
        self.assertContains(response, 'id="kycCustomerOptions"')

    def test_pdf_upload_with_a_code_links_the_new_enquiry(self):
        pdf = SimpleUploadedFile("rfq.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        with patch("enquiries.views.services.create_enquiry", return_value={"enquiry_id": "E77"}) as create:
            self.client.post(reverse("enquiries:upload_pdf_enquiry"),
                             {"pdf": pdf, "title": "Door seal", "customer_name": "Acme Seals",
                              "customer_code": "AV-0001"})
        self.assertEqual(create.call_args.args[1]["customer_name"], "Acme Seals")
        self.assertEqual(links.customers_for(["E77"]), {"E77": self.acme})
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="E77").customer_email, "meera@acme.example")


@override_settings(**BASE)
class CustomerPageTests(Base):
    def test_customer_page_lists_linked_enquiries(self):
        links.set_link("E1", self.acme)
        response = self.client.get(reverse("kyc:detail", args=[self.acme.pk]))
        self.assertContains(response, "Enquiry #E1")
        self.assertEqual(response.context["enquiry_link_count"], 1)

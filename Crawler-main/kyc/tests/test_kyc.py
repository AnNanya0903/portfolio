"""Tests for the KYC customer master: customer codes, contacts by team, search and access."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from kyc.forms import CustomerContactForm, CustomerForm
from kyc.models import Customer, CustomerContact

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


def _customer(code="AV-0001", name="Acme Seals", **extra):
    return Customer.objects.create(code=code, name=name, **extra)


class CustomerFormTests(TestCase):
    def test_code_is_stored_in_capitals_without_spaces(self):
        form = CustomerForm({"code": " av - 0001 ", "name": "Acme"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().code, "AV-0001")

    def test_same_code_in_other_case_is_rejected(self):
        _customer()
        form = CustomerForm({"code": "av-0001", "name": "Other"})
        self.assertFalse(form.is_valid())
        self.assertIn("already has this customer code", form.errors["code"][0])

    def test_editing_a_customer_keeps_its_own_code(self):
        customer = _customer()
        form = CustomerForm({"code": "AV-0001", "name": "Acme Seals Pvt Ltd"}, instance=customer)
        self.assertTrue(form.is_valid(), form.errors)

    def test_code_with_other_symbols_is_rejected(self):
        self.assertIn("code", CustomerForm({"code": "AV#1", "name": "Acme"}).errors)

    def test_gstin_and_pan_are_checked_only_when_given(self):
        self.assertTrue(CustomerForm({"code": "A1", "name": "Acme"}).is_valid())
        good = CustomerForm({"code": "A2", "name": "Acme", "gstin": "29abcde1234f1z5", "pan": "abcde1234f"})
        self.assertTrue(good.is_valid(), good.errors)
        self.assertEqual(good.cleaned_data["gstin"], "29ABCDE1234F1Z5")
        bad = CustomerForm({"code": "A3", "name": "Acme", "gstin": "123", "pan": "12345"})
        self.assertIn("gstin", bad.errors)
        self.assertIn("pan", bad.errors)


class ContactFormTests(TestCase):
    def test_contact_needs_email_or_phone(self):
        form = CustomerContactForm({"category": "accounts", "name": "Ravi"})
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())
        self.assertTrue(CustomerContactForm({"category": "accounts", "name": "Ravi", "phone": "98450"}).is_valid())

    def test_purchaser_needs_an_email(self):
        form = CustomerContactForm({"category": "sales_marketing", "name": "Ravi", "phone": "98450",
                                    "is_purchaser": "on"})
        self.assertIn("email", form.errors)

    def test_unknown_team_is_rejected(self):
        form = CustomerContactForm({"category": "hr", "name": "Ravi", "email": "ravi@acme.example"})
        self.assertIn("category", form.errors)


@override_settings(**BASE)
class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)

    def test_pages_need_login(self):
        self.client.logout()
        response = self.client.get(reverse("kyc:list"))
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, reverse("kyc:list"))

    def test_restricted_user_without_kyc_is_turned_away(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["enquiries"])
        response = self.client.get(reverse("kyc:list"))
        self.assertRedirects(response, reverse("dashboard_hub"), fetch_redirect_response=False)

    def test_restricted_user_with_kyc_gets_in(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["kyc"])
        self.assertEqual(self.client.get(reverse("kyc:list")).status_code, 200)

    def test_create_customer_then_add_contact(self):
        response = self.client.post(reverse("kyc:new"), {"code": "av-0002", "name": "Bolt Works", "country": "India"})
        customer = Customer.objects.get(code="AV-0002")
        self.assertRedirects(response, reverse("kyc:detail", args=[customer.pk]))
        self.assertEqual(customer.created_by, self.user)

        response = self.client.post(reverse("kyc:contact_new", args=[customer.pk]), {
            "category": "sales_marketing", "name": "Meera", "email": "Meera@Bolt.example", "is_purchaser": "on"})
        self.assertRedirects(response, reverse("kyc:detail", args=[customer.pk]))
        contact = customer.contacts.get()
        self.assertEqual(contact.email, "meera@bolt.example")
        self.assertTrue(contact.is_purchaser)

    def test_duplicate_code_shows_the_form_again(self):
        _customer()
        response = self.client.post(reverse("kyc:new"), {"code": "AV-0001", "name": "Copy"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has this customer code")
        self.assertEqual(Customer.objects.count(), 1)

    def test_search_by_code_name_and_contact_email(self):
        acme = _customer()
        _customer("BW-0001", "Bolt Works")
        CustomerContact.objects.create(customer=acme, category="accounts", name="Ravi", email="ravi@acme.example")
        for q, expected in (("av-00", ["AV-0001"]), ("bolt", ["BW-0001"]), ("ravi@acme", ["AV-0001"]), ("zzz", [])):
            response = self.client.get(reverse("kyc:list"), {"q": q})
            self.assertEqual([c.code for c in response.context["page"]], expected, q)

    def test_customer_with_two_matching_contacts_is_listed_once(self):
        acme = _customer()
        for name in ("Ravi", "Rani"):
            CustomerContact.objects.create(customer=acme, category="accounts", name=name,
                                           email=f"{name.lower()}@acme.example")
        response = self.client.get(reverse("kyc:list"), {"q": "acme.example"})
        self.assertEqual(len(response.context["page"]), 1)
        self.assertEqual(response.context["page"][0].contact_count, 2)

    def test_detail_groups_contacts_by_team_and_flags_gaps(self):
        acme = _customer()
        CustomerContact.objects.create(customer=acme, category="accounts", name="Ravi", email="ravi@acme.example")
        response = self.client.get(reverse("kyc:detail", args=[acme.pk]))
        groups = {g["key"]: g["contacts"] for g in response.context["groups"]}
        self.assertEqual([c.name for c in groups["accounts"]], ["Ravi"])
        self.assertEqual(groups["technical"], [])
        self.assertFalse(response.context["has_purchaser"])
        self.assertNotIn("Accounts", response.context["missing_teams"])
        self.assertIn("Top management", response.context["missing_teams"])
        self.assertContains(response, "No purchaser yet")

    def test_new_contact_form_preselects_the_team(self):
        acme = _customer()
        response = self.client.get(reverse("kyc:contact_new", args=[acme.pk]), {"category": "technical"})
        self.assertEqual(response.context["form"].initial, {"category": "technical"})
        response = self.client.get(reverse("kyc:contact_new", args=[acme.pk]), {"category": "bogus"})
        self.assertEqual(response.context["form"].initial, {})

    def test_edit_and_delete_contact(self):
        acme = _customer()
        other = _customer("BW-0001", "Bolt Works")
        contact = CustomerContact.objects.create(customer=acme, category="accounts", name="Ravi",
                                                 email="ravi@acme.example")
        self.client.post(reverse("kyc:contact_edit", args=[acme.pk, contact.pk]),
                         {"category": "top_management", "name": "Ravi K", "email": "ravi@acme.example"})
        contact.refresh_from_db()
        self.assertEqual((contact.category, contact.name), ("top_management", "Ravi K"))

        # A contact cannot be reached through another customer's URL.
        wrong = self.client.post(reverse("kyc:contact_delete", args=[other.pk, contact.pk]))
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.client.get(reverse("kyc:contact_delete", args=[acme.pk, contact.pk])).status_code, 405)
        self.client.post(reverse("kyc:contact_delete", args=[acme.pk, contact.pk]))
        self.assertFalse(CustomerContact.objects.filter(pk=contact.pk).exists())

    def test_edit_customer_records_who_changed_it(self):
        acme = _customer()
        self.client.post(reverse("kyc:edit", args=[acme.pk]), {"code": "AV-0001", "name": "Acme Seals Pvt Ltd"})
        acme.refresh_from_db()
        self.assertEqual((acme.name, acme.updated_by), ("Acme Seals Pvt Ltd", self.user))

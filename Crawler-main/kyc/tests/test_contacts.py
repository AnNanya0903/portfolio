"""Contacts at a customer: designation dropdown (with Other), 10-digit mobile and alternate number, email checks,
and one primary contact per customer."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from kyc.forms import CustomerContactForm
from kyc.models import CONTACT_DESIGNATIONS, Customer, CustomerContact

User = get_user_model()


def contact_data(**extra):
    data = {
        "category": CustomerContact.CATEGORY_SALES, "name": "Ravi Kumar", "designation": "Director",
        "designation_other": "", "phone": "9876543210", "alternate_phone": "", "email": "ravi@acme.example",
        "notes": "", "is_purchaser": "",
    }
    data.update(extra)
    return data


class ContactFormTests(TestCase):
    def valid(self, **extra):
        form = CustomerContactForm(contact_data(**extra))
        return form.is_valid(), form

    def test_designation_is_required_and_comes_from_the_list(self):
        ok, form = self.valid(designation="")
        self.assertFalse(ok)
        self.assertIn("designation", form.errors)
        ok, form = self.valid(designation="Chief Wizard")
        self.assertFalse(ok)
        self.assertIn("designation", form.errors)

    def test_every_listed_designation_is_accepted(self):
        for designation in CONTACT_DESIGNATIONS:
            if designation == "Other":
                continue
            ok, form = self.valid(designation=designation)
            self.assertTrue(ok, (designation, form.errors))

    def test_other_needs_a_typed_designation_which_is_saved_as_typed(self):
        ok, form = self.valid(designation="Other", designation_other="")
        self.assertFalse(ok)
        self.assertIn("designation_other", form.errors)
        ok, form = self.valid(designation="Other", designation_other="  Plant   Supervisor ")
        self.assertTrue(ok, form.errors)
        self.assertEqual(form.cleaned_data["designation"], "Plant Supervisor")

    def test_mobile_is_required_and_must_be_ten_digits(self):
        ok, form = self.valid(phone="")
        self.assertFalse(ok)
        self.assertIn("phone", form.errors)
        ok, form = self.valid(phone="12345")
        self.assertFalse(ok)
        ok, form = self.valid(phone="98765 43210")
        self.assertTrue(ok, form.errors)
        self.assertEqual(form.cleaned_data["phone"], "9876543210")

    def test_alternate_number_is_optional_but_must_be_ten_digits_when_given(self):
        ok, form = self.valid(alternate_phone="")
        self.assertTrue(ok, form.errors)
        ok, form = self.valid(alternate_phone="98765-4321")
        self.assertFalse(ok)
        self.assertIn("alternate_phone", form.errors)

    def test_each_email_is_checked(self):
        ok, form = self.valid(email="ravi@acme.example, not-an-address")
        self.assertFalse(ok)
        self.assertIn("email", form.errors)
        ok, form = self.valid(email="Ravi@Acme.example, ravi@acme.example")
        self.assertTrue(ok, form.errors)
        self.assertEqual(form.cleaned_data["email"], "ravi@acme.example")

    def test_the_primary_contact_needs_an_email(self):
        ok, form = self.valid(is_purchaser="on", email="")
        self.assertFalse(ok)
        self.assertIn("email", form.errors)

    def test_a_typed_designation_is_shown_as_other_when_editing(self):
        customer = Customer.objects.create(code="AV-0100", name="Typed Designation Co")
        contact = CustomerContact.objects.create(
            customer=customer, category=CustomerContact.CATEGORY_SALES, name="Meena", designation="Plant Supervisor",
            phone="9876543210")
        form = CustomerContactForm(instance=contact)
        self.assertEqual(form["designation"].value(), "Other")
        self.assertEqual(form["designation_other"].value(), "Plant Supervisor")


class ContactPagesTests(TestCase):
    def setUp(self):
        call_command("seed_departments")  # the customer page groups contacts under the departments
        self.user = User.objects.create_user("contacts-user", "contacts-user@example.com", "x")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(code="AV-0101", name="Pages Co")

    def post_new(self, **extra):
        return self.client.post(reverse("kyc:contact_new", args=[self.customer.pk]), contact_data(**extra))

    def test_a_contact_is_added_with_its_designation_and_numbers(self):
        response = self.post_new(designation="Other", designation_other="Store Keeper", alternate_phone="9123456789")
        self.assertEqual(response.status_code, 302)
        contact = self.customer.contacts.get()
        self.assertEqual(contact.designation, "Store Keeper")
        self.assertEqual(contact.alternate_phone, "9123456789")

    def test_only_one_primary_contact_per_customer(self):
        self.post_new(name="First", is_purchaser="on")
        self.post_new(name="Second", is_purchaser="on", email="second@acme.example")
        self.assertEqual(self.customer.contacts.filter(is_purchaser=True).get().name, "Second")
        self.assertEqual(self.customer.contacts.filter(name="First").get().is_purchaser, False)

    def test_editing_a_contact_keeps_it_primary_without_affecting_others(self):
        first = CustomerContact.objects.create(customer=self.customer, category=CustomerContact.CATEGORY_SALES,
                                               name="Keep", phone="9876543210", email="keep@acme.example",
                                               is_purchaser=True)
        other = CustomerContact.objects.create(customer=self.customer, category=CustomerContact.CATEGORY_ACCOUNTS,
                                               name="Other", phone="9876500000")
        response = self.client.post(reverse("kyc:contact_edit", args=[self.customer.pk, other.pk]),
                                    contact_data(name="Other", category=CustomerContact.CATEGORY_ACCOUNTS,
                                                 phone="9876500000", email="", is_purchaser=""))
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertTrue(first.is_purchaser)

    def test_the_detail_page_shows_mobile_alternate_and_remarks(self):
        CustomerContact.objects.create(customer=self.customer, category=CustomerContact.CATEGORY_SALES,
                                       name="Show Me", designation="Director", phone="9876543210",
                                       alternate_phone="9123456789", notes="Prefers calls after 3 pm")
        response = self.client.get(reverse("kyc:detail", args=[self.customer.pk]))
        self.assertContains(response, "Mobile 9876543210")
        self.assertContains(response, "Alt. 9123456789")
        self.assertContains(response, "Remarks: Prefers calls after 3 pm")

    def test_delete_is_only_possible_by_post(self):
        contact = CustomerContact.objects.create(customer=self.customer, category=CustomerContact.CATEGORY_SALES,
                                                 name="Gone", phone="9876543210")
        url = reverse("kyc:contact_delete", args=[self.customer.pk, contact.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.assertFalse(CustomerContact.objects.filter(pk=contact.pk).exists())

    def test_the_form_page_offers_the_designation_list_and_other_box(self):
        response = self.client.get(reverse("kyc:contact_new", args=[self.customer.pk]))
        self.assertContains(response, "Owner / Proprietor")
        self.assertContains(response, "designationOtherWrap")
        self.assertContains(response, "Primary contact")

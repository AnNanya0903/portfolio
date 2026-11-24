"""Telling apart customers with the same company name: GSTIN must be unique, a same name needs a confirm tick."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from kyc import links, sheet_import
from kyc.forms import CustomerForm
from kyc.models import Customer

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
GSTIN = "33AKNPT5683R1ZZ"


def _data(**extra):
    return {"code": "AY-0002", "name": "AYSH", "country": "India"} | extra


class DuplicateFormTests(TestCase):
    def setUp(self):
        self.aysh = Customer.objects.create(code="AY-0001", name="AYSH", city="Trichy", gstin=GSTIN)

    def test_same_gstin_is_refused_everywhere(self):
        for check in (True, False):  # pages and sheet imports alike
            form = CustomerForm(_data(name="Other Co", gstin=GSTIN.lower()), check_same_name=check)
            self.assertFalse(form.is_valid())
            self.assertIn("already used by customer AY-0001 (AYSH)", form.errors["gstin"][0])
            self.assertEqual(form.gstin_customer, self.aysh)

    def test_same_name_needs_the_confirm_tick_on_pages(self):
        form = CustomerForm(_data(name=" aysh "), check_same_name=True)
        self.assertFalse(form.is_valid())
        self.assertIn("Another customer already has this company name", form.errors["name"][0])
        self.assertEqual(form.same_name_customers, [self.aysh])
        self.assertTrue(CustomerForm(_data(same_name_ok="on"), check_same_name=True).is_valid())

    def test_sheet_imports_do_not_ask_about_names(self):
        self.assertTrue(CustomerForm(_data()).is_valid())

    def test_editing_a_customer_does_not_match_itself(self):
        form = CustomerForm(_data(code="AY-0001", gstin=GSTIN), instance=self.aysh, check_same_name=True)
        self.assertTrue(form.is_valid(), form.errors)

    def test_different_names_and_gstins_are_fine(self):
        self.assertTrue(CustomerForm(_data(name="Bolt Works", gstin="29AABCU9603R1ZX"), check_same_name=True)
                        .is_valid())

    def test_code_picker_search_shows_and_finds_gstin(self):
        self.assertEqual(links.search("33AKNPT")[0]["gstin"], GSTIN)
        self.assertEqual(links.search("aysh")[0]["code"], "AY-0001")


@override_settings(**BASE)
class DuplicatePageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("kycuser", "kyc@example.com", "x"))
        self.aysh = Customer.objects.create(code="AY-0001", name="AYSH", city="Trichy", gstin=GSTIN)

    def test_new_customer_with_same_name_shows_the_existing_one_and_saves_after_the_tick(self):
        response = self.client.post(reverse("kyc:new"), _data(city="Chennai"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-duplicate-name")
        self.assertContains(response, reverse("kyc:detail", args=[self.aysh.pk]))
        self.assertContains(response, "AY-0001")
        self.assertContains(response, f"GSTIN {GSTIN}")
        self.assertEqual(Customer.objects.count(), 1)
        response = self.client.post(reverse("kyc:new"), _data(city="Chennai", same_name_ok="on"))
        branch = Customer.objects.get(code="AY-0002")
        self.assertRedirects(response, reverse("kyc:detail", args=[branch.pk]))

    def test_new_customer_with_same_gstin_links_to_the_existing_one(self):
        response = self.client.post(reverse("kyc:new"), _data(name="Different Name", gstin=GSTIN))
        self.assertContains(response, "data-duplicate-gstin")
        self.assertContains(response, reverse("kyc:detail", args=[self.aysh.pk]))
        self.assertEqual(Customer.objects.count(), 1)

    def test_editing_into_another_customers_name_warns_too(self):
        other = Customer.objects.create(code="BW-0001", name="Bolt Works")
        response = self.client.post(reverse("kyc:edit", args=[other.pk]), {"code": "BW-0001", "name": "AYSH",
                                                                         "country": "India"})
        self.assertContains(response, "data-duplicate-name")
        other.refresh_from_db()
        self.assertEqual(other.name, "Bolt Works")


class DuplicateSheetTests(TestCase):
    def _plan(self, text):
        upload = SimpleUploadedFile("customers.csv", text.encode("utf-8"), content_type="text/csv")
        return sheet_import.build_plan(sheet_import.read_rows(upload))

    def test_same_gstin_twice_in_one_sheet_keeps_only_the_first(self):
        plan = self._plan(f"Customer code,Company name,GSTIN\nAY-0001,AYSH,{GSTIN}\nAY-0002,AYSH Chennai,{GSTIN}\n")
        self.assertEqual([c["code"] for c in plan["customers"]], ["AY-0001"])
        self.assertIn("also given for customer AY-0001", plan["problems"][0]["message"])

    def test_same_gstin_as_an_existing_customer_is_left_out(self):
        Customer.objects.create(code="AY-0001", name="AYSH", gstin=GSTIN)
        plan = self._plan(f"Customer code,Company name,GSTIN\nAY-0005,Someone,{GSTIN}\n")
        self.assertEqual(plan["customers"], [])
        self.assertIn("already used by customer AY-0001", plan["problems"][0]["message"])

"""The customer's Team: a multi-select that has every department ticked unless someone unticks some."""
import importlib
import re

from django.apps import apps
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from kyc import sheet_import
from kyc.forms import CustomerForm
from kyc.models import Customer, Department
from kyc.tests import *  # noqa: F401,F403  (Python 3.14 test-client fix for Django 4.2)
from kyc.tests.test_departments import make_user

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
BASE = {"code": "AV-0100", "name": "Zed Seals", "country": "India", "preferred_transporter": "ARC"}


@override_settings(**STATIC)
class TeamTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.everyone = set(Department.objects.values_list("name", flat=True))
        self.client.force_login(make_user("sam"))

    def _names(self, customer):
        return set(customer.departments.values_list("name", flat=True))

    def test_new_customer_form_shows_every_department_ticked_and_all_box_ticked(self):
        page = self.client.get(reverse("kyc:new"))
        form = page.context["form"]
        self.assertEqual(len(form.initial["departments"]), 5)
        self.assertTrue(form.initial["all_departments"])
        html = page.content.decode()
        self.assertIn("Select all / All departments", html)
        self.assertEqual(html.count('name="departments" value='), 5)
        boxes = re.findall(r'<input[^>]*name="departments"[^>]*>', html)
        self.assertEqual(len(boxes), 5)
        self.assertTrue(all("checked" in box for box in boxes))

    def test_saving_with_the_boxes_the_browser_sends(self):
        ids = list(Department.objects.values_list("pk", flat=True))
        self.client.post(reverse("kyc:new"), {**BASE, "departments_present": "1", "all_departments": "on",
                                              "departments": ids})
        self.assertEqual(self._names(Customer.objects.get(code="AV-0100")), self.everyone)

    def test_a_few_departments_can_be_chosen(self):
        sales = Department.objects.get(name="Sales")
        self.client.post(reverse("kyc:new"), {**BASE, "departments_present": "1", "departments": [sales.pk]})
        self.assertEqual(self._names(Customer.objects.get(code="AV-0100")), {"Sales"})

    def test_at_least_one_department_is_required(self):
        response = self.client.post(reverse("kyc:new"), {**BASE, "departments_present": "1"})
        self.assertContains(response, "Select at least one department.")
        self.assertFalse(Customer.objects.filter(code="AV-0100").exists())

    def test_no_team_sent_means_every_active_department(self):
        Department.objects.filter(name="Marketing").update(is_active=False)
        form = CustomerForm(dict(BASE))
        self.assertTrue(form.is_valid(), form.errors)
        customer = form.save()
        self.assertEqual(self._names(customer), self.everyone - {"Marketing"})
        plain = Customer.objects.create(code="AV-0101", name="Made in code")  # no form at all
        self.assertEqual(self._names(plain), self.everyone - {"Marketing"})

    def test_a_customer_read_from_a_document_gets_every_department(self):
        from kyc.models import KycDocument
        doc = KycDocument.objects.create(extracted_data={"customer": {}, "contacts": []}, original_name="x.pdf")
        self.client.post(reverse("kyc:import_review", args=[doc.pk]), {
            **BASE, "contacts-TOTAL_FORMS": "0", "contacts-INITIAL_FORMS": "0",
            "contacts-MIN_NUM_FORMS": "0", "contacts-MAX_NUM_FORMS": "1000"})
        self.assertEqual(self._names(Customer.objects.get(code="AV-0100")), self.everyone)

    def test_a_sheet_import_gives_every_department(self):
        plan = {"customers": [{"data": dict(BASE), "contacts": []}]}
        self.assertEqual(sheet_import.apply_plan(plan, make_user("imp"))["customers"], 1)
        self.assertEqual(self._names(Customer.objects.get(code="AV-0100")), self.everyone)

    def test_list_and_profile_show_badges(self):
        customer = Customer.objects.create(code="AV-0102", name="Badge Co")
        customer.departments.set(Department.objects.filter(name__in=["Sales", "Accounts"]))
        for url in (reverse("kyc:list"), reverse("kyc:detail", args=[customer.pk])):
            html = self.client.get(url).content.decode()
            self.assertIn('kyc-chip--draft me-1', html)
            self.assertIn("Sales</span>", html)
            self.assertIn("Accounts</span>", html)
            self.assertNotIn("Marketing</span>", html)

    def test_editing_shows_the_saved_team(self):
        customer = Customer.objects.create(code="AV-0103", name="Edit Co")
        customer.departments.set(Department.objects.filter(name="Technical/Design"))
        form = self.client.get(reverse("kyc:edit", args=[customer.pk])).context["form"]
        self.assertEqual(len(form.initial["departments"]), 1)
        self.assertFalse(form.initial["all_departments"])

    def test_the_link_table_is_unique_per_pair(self):
        table = Customer.departments.through
        self.assertEqual(table._meta.db_table, "kyc_customer_departments")
        customer = Customer.objects.create(code="AV-0104", name="Pair Co")
        customer.departments.add(*Department.objects.all())  # adding again does not duplicate
        self.assertEqual(table.objects.filter(customer=customer).count(), 5)


class MigrationDataTests(TestCase):
    def test_existing_customers_keep_their_team_and_gain_all_the_others(self):
        module = importlib.import_module("kyc.migrations.0009_customers_in_every_department")
        call_command("seed_departments")
        customer = Customer.objects.create(code="AV-0200", name="Old Co")
        customer.departments.clear()
        accounts = Department.objects.get(name="Accounts")

        class OldCustomer:  # the historical model still had the single "handled by" column
            handled_by_id = accounts.pk
            departments = customer.departments

        class OldCustomers:
            def exists(self):
                return True

            def all(self):
                return self

            def iterator(self):
                return iter([OldCustomer()])

        class FakeApps:
            def get_model(self, app, name):
                if name == "Customer":
                    return type("Customer", (), {"objects": OldCustomers()})
                return apps.get_model(app, name)

        module.give_every_customer_every_department(FakeApps(), None)
        self.assertEqual(set(customer.departments.values_list("name", flat=True)),
                         set(Department.objects.values_list("name", flat=True)))

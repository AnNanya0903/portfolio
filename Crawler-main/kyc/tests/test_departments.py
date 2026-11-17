"""Departments, who sees PAN in full, the audit trail, and that every department sees every customer."""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from kyc.departments import departments_of, mask_pan
from kyc.models import Customer, Department, DepartmentContact, KycAuditLog, UserDepartment

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
User = get_user_model()


def make_user(name, email=None, **extra):
    return User.objects.create_user(name, email or f"{name}@example.com", "x", **extra)


class SeedTests(TestCase):
    def test_seed_creates_the_five_departments_and_is_safe_to_repeat(self):
        call_command("seed_departments")
        call_command("seed_departments")
        self.assertEqual(Department.objects.count(), 5)
        self.assertEqual(DepartmentContact.objects.count(), 10)
        marketing = Department.objects.get(name="Marketing")
        self.assertEqual(marketing.emails(), ["kumar@avigiri.com", "manju@avigiri.com", "apai@avigiri.com"])
        self.assertEqual(marketing.contacts.filter(is_primary=True).count(), 1)
        self.assertTrue(marketing.contacts.get(email="kumar@avigiri.com").is_primary)

    def test_the_same_email_may_be_in_two_departments_but_not_twice_in_one(self):
        call_command("seed_departments")
        self.assertEqual(DepartmentContact.objects.filter(email="apai@avigiri.com").count(), 2)
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            DepartmentContact.objects.create(department=Department.objects.get(name="Sales"), email="rabin@avigiri.com")

    def test_seed_keeps_a_deactivation_and_the_primary_but_restores_a_missing_address(self):
        call_command("seed_departments")
        sales = Department.objects.get(name="Sales")
        sales.contacts.filter(email="rabin@avigiri.com").delete()
        sales.contacts.filter(email="customerservice@avigiri.com").update(is_primary=False)
        Department.objects.filter(name="Sales").update(is_active=False)
        call_command("seed_departments")
        sales.refresh_from_db()
        self.assertFalse(sales.is_active)
        self.assertEqual(sales.contacts.filter(is_primary=True).count(), 0)  # not forced back
        self.assertTrue(sales.contacts.filter(email="rabin@avigiri.com").exists())


@override_settings(**BASE)
class VisibilityAndPanTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.accounts = Department.objects.get(name="Accounts")
        self.sales = Department.objects.get(name="Sales")
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals", pan="ABCDE1234F")
        self.customer.departments.set([self.accounts])
        self.sales_user = make_user("sam", "rabin@avigiri.com")
        self.accounts_user = make_user("amy", "accounts@avigiri.com")
        self.outsider = make_user("nobody", "nobody@example.com")
        self.admin = make_user("boss", "boss@example.com", is_superuser=True)

    def test_every_department_sees_every_customer_whoever_handles_it(self):
        for user in (self.sales_user, self.accounts_user, self.outsider):
            self.client.force_login(user)
            self.assertContains(self.client.get(reverse("kyc:list")), "Acme Seals")
            self.assertContains(self.client.get(reverse("kyc:detail", args=[self.customer.pk])), "Acme Seals")

    def test_team_shows_in_the_list_and_on_the_profile_with_mailto_links(self):
        self.client.force_login(self.sales_user)
        self.assertContains(self.client.get(reverse("kyc:list")), "Accounts")
        page = self.client.get(reverse("kyc:detail", args=[self.customer.pk]))
        self.assertContains(page, 'kyc-chip--draft me-1">Accounts')
        self.assertContains(page, 'href="mailto:accounts1@avigiri.com"')
        self.assertLess(page.content.decode().index("accounts1@avigiri.com"), page.content.decode().index("mailto:accounts@avigiri.com"))

    def test_by_default_every_department_sees_the_full_details_including_pan(self):
        url = reverse("kyc:detail", args=[self.customer.pk])
        for user in (self.sales_user, self.accounts_user, self.outsider, self.admin):
            self.client.force_login(user)
            self.assertContains(self.client.get(url), "ABCDE1234F")
        self.client.force_login(self.sales_user)
        self.assertContains(self.client.get(reverse("kyc:edit", args=[self.customer.pk])), 'name="pan"')

    @mock.patch("kyc.departments.MASK_PAN", True)
    def test_pan_is_masked_for_other_departments_and_full_for_accounts_and_admins(self):
        url = reverse("kyc:detail", args=[self.customer.pk])
        self.client.force_login(self.sales_user)
        page = self.client.get(url)
        self.assertContains(page, "ABCXX1234F")
        self.assertNotContains(page, "ABCDE1234F")
        self.client.force_login(self.outsider)
        self.assertNotContains(self.client.get(url), "ABCDE1234F")
        for user in (self.accounts_user, self.admin):
            self.client.force_login(user)
            self.assertContains(self.client.get(url), "ABCDE1234F")

    @mock.patch("kyc.departments.MASK_PAN", True)
    def test_top_management_sees_pan_in_full(self):
        user = make_user("apai", "apai@avigiri.com")
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse("kyc:detail", args=[self.customer.pk])), "ABCDE1234F")

    @mock.patch("kyc.departments.MASK_PAN", True)
    def test_an_explicit_user_department_beats_the_email_match(self):
        UserDepartment.objects.create(user=self.accounts_user, department=self.sales)
        self.assertEqual([d.name for d in departments_of(self.accounts_user)], ["Sales"])
        self.client.force_login(self.accounts_user)
        self.assertNotContains(self.client.get(reverse("kyc:detail", args=[self.customer.pk])), "ABCDE1234F")

    @mock.patch("kyc.departments.MASK_PAN", True)
    def test_the_edit_form_does_not_hand_over_pan_and_saving_keeps_it(self):
        url = reverse("kyc:edit", args=[self.customer.pk])
        self.client.force_login(self.sales_user)
        page = self.client.get(url)
        self.assertNotContains(page, "ABCDE1234F")
        self.assertNotContains(page, 'name="pan"')
        response = self.client.post(url, {"code": "AV-0001", "name": "Acme Seals Ltd", "country": "India",
                                          "preferred_transporter": "ARC", "pan": "ZZZZZ9999Z"})
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, "Acme Seals Ltd")
        self.assertEqual(self.customer.pan, "ABCDE1234F")

    @mock.patch("kyc.departments.MASK_PAN", True)
    def test_accounts_can_edit_pan(self):
        url = reverse("kyc:edit", args=[self.customer.pk])
        self.client.force_login(self.accounts_user)
        self.assertContains(self.client.get(url), 'name="pan"')

    def test_team_can_be_changed_on_the_form_but_not_left_empty(self):
        self.client.force_login(self.sales_user)
        url = reverse("kyc:edit", args=[self.customer.pk])
        data = {"code": "AV-0001", "name": "Acme Seals", "country": "India", "preferred_transporter": "ARC",
                "departments_present": "1"}
        self.client.post(url, {**data, "departments": [self.sales.pk]})
        self.assertEqual(list(self.customer.departments.all()), [self.sales])
        response = self.client.post(url, data)  # every box unticked
        self.assertContains(response, "Select at least one department.")
        self.assertEqual(list(self.customer.departments.all()), [self.sales])

    def test_mask_pan(self):
        self.assertEqual(mask_pan("ABCDE1234F"), "ABCXX1234F")
        self.assertEqual(mask_pan(""), "")
        self.assertEqual(mask_pan("SHORT"), "XHORT")


@override_settings(**BASE)
class AuditTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals", pan="ABCDE1234F")
        self.user = make_user("sam", "rabin@avigiri.com")
        self.client.force_login(self.user)

    def test_viewing_a_customer_is_logged_with_user_and_department(self):
        self.client.get(reverse("kyc:detail", args=[self.customer.pk]))
        row = KycAuditLog.objects.get()
        self.assertEqual((row.username, row.department, row.action, row.customer), ("sam", "Sales", "view", self.customer))

    def test_an_edit_logs_each_changed_field_but_never_the_values(self):
        self.client.post(reverse("kyc:edit", args=[self.customer.pk]), {
            "code": "AV-0001", "name": "Acme Seals Ltd", "city": "Chennai", "country": "India",
            "pan": "ABCDE1234F", "preferred_transporter": "ARC"})
        rows = KycAuditLog.objects.filter(action="edit")
        self.assertEqual({r.field_changed for r in rows}, {"name", "city"})
        self.assertFalse(any("Chennai" in str(r.field_changed) + r.customer_label for r in rows))

    def test_contact_changes_and_deletion_are_logged(self):
        self.client.post(reverse("kyc:contact_new", args=[self.customer.pk]),
                         {"category": "accounts", "name": "Rahul", "email": "r@acme.example"})
        self.assertTrue(KycAuditLog.objects.filter(field_changed="contact added: Rahul").exists())
        self.client.post(reverse("kyc:delete", args=[self.customer.pk]), {"confirm_code": "AV-0001"})
        row = KycAuditLog.objects.get(action="delete")
        self.assertEqual(row.customer_label, "AV-0001 - Acme Seals")
        self.assertIsNone(row.customer)


@override_settings(**BASE)
class AdminPageTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.admin = make_user("boss", is_superuser=True)
        self.user = make_user("sam")
        self.sales = Department.objects.get(name="Sales")

    def test_only_admins_can_manage_departments(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("kyc:departments_manage")).status_code, 403)
        response = self.client.post(reverse("kyc:department_email_add", args=[self.sales.pk]), {"email": "x@avigiri.com"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DepartmentContact.objects.filter(email="x@avigiri.com").exists())

    def test_contact_departments_cards_list_active_departments_with_all_emails(self):
        Department.objects.filter(name="Accounts").update(is_active=False)
        self.client.force_login(self.user)
        page = self.client.get(reverse("kyc:departments"))
        for text in ("Sales", "customerservice@avigiri.com", "rabin@avigiri.com", "Top Management", "mailto:sagar@avigiri.com"):
            self.assertContains(page, text)
        self.assertNotContains(page, "accounts1@avigiri.com")
        self.assertNotContains(page, "Manage departments")

    def test_admin_adds_edits_and_removes_emails_and_keeps_one_primary(self):
        self.client.force_login(self.admin)
        pk = self.sales.pk
        self.client.post(reverse("kyc:department_email_add", args=[pk]), {"email": " New@Avigiri.com "})
        self.assertTrue(self.sales.contacts.filter(email="new@avigiri.com", is_primary=False).exists())
        self.client.post(reverse("kyc:department_email_add", args=[pk]), {"email": "new@avigiri.com"})  # duplicate
        self.client.post(reverse("kyc:department_email_add", args=[pk]), {"email": "not-an-email"})
        self.assertEqual(self.sales.contacts.count(), 3)
        new = self.sales.contacts.get(email="new@avigiri.com")
        self.client.post(reverse("kyc:department_email_primary", args=[pk, new.pk]))
        self.assertEqual(list(self.sales.contacts.filter(is_primary=True)), [new])
        self.client.post(reverse("kyc:department_email_edit", args=[pk, new.pk]), {"email": "newer@avigiri.com"})
        self.assertTrue(self.sales.contacts.filter(email="newer@avigiri.com", is_primary=True).exists())
        self.client.post(reverse("kyc:department_email_remove", args=[pk, new.pk]))
        self.assertEqual(self.sales.contacts.filter(is_primary=True).count(), 1)
        self.assertEqual(self.sales.contacts.count(), 2)

    def test_admin_creates_and_deactivates_but_cannot_delete(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("kyc:department_add"), {"name": "Stores", "description": "Warehouse"})
        stores = Department.objects.get(name="Stores")
        self.client.post(reverse("kyc:department_add"), {"name": "stores"})  # same name, other case
        self.assertEqual(Department.objects.filter(name__iexact="stores").count(), 1)
        self.client.post(reverse("kyc:department_toggle", args=[stores.pk]))
        stores.refresh_from_db()
        self.assertFalse(stores.is_active)
        self.assertEqual(self.client.get(f"/kyc/departments/{stores.pk}/delete/").status_code, 404)

    def test_a_deactivated_department_keeps_its_customers(self):
        customer = Customer.objects.create(code="AV-0009", name="Zed")
        customer.departments.set([self.sales])
        self.client.force_login(self.admin)
        self.client.post(reverse("kyc:department_toggle", args=[self.sales.pk]))
        customer.refresh_from_db()
        self.assertEqual(list(customer.departments.all()), [self.sales])

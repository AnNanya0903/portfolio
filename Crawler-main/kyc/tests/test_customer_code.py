"""Automatic customer codes (AVG-YY-NNNN): format, running numbers, locking, the form, lists and the backfill."""
import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from kyc import links
from kyc.models import Customer, CustomerCodeCounter, next_customer_code

User = get_user_model()
IST = ZoneInfo("Asia/Kolkata")


def make_customer(name, code=None, **extra):
    return Customer.objects.create(code=code or name.upper().replace(" ", "-")[:20], name=name, **extra)


class CustomerCodeGenerationTests(TestCase):
    def test_new_customers_get_sequential_codes_starting_at_1001(self):
        year = timezone.now().astimezone(IST).year % 100
        first = make_customer("Alpha Works", "AV-0001")
        second = make_customer("Beta Works", "AV-0002")
        self.assertEqual(first.customer_code, f"AVG-{year:02d}-1001")
        self.assertEqual(second.customer_code, f"AVG-{year:02d}-1002")

    def test_a_saved_customer_keeps_its_code_when_edited(self):
        customer = make_customer("Gamma Works", "AV-0003")
        code = customer.customer_code
        customer.name = "Gamma Works Limited"
        customer.save()
        customer.refresh_from_db()
        self.assertEqual(customer.customer_code, code)

    def test_a_deleted_customer_number_is_never_reused(self):
        a = make_customer("Delta Works", "AV-0004")
        b = make_customer("Epsilon Works", "AV-0005")
        b.delete()
        c = make_customer("Zeta Works", "AV-0006")
        self.assertNotEqual(c.customer_code, a.customer_code)
        self.assertEqual(c.customer_code[-4:], "1003")

    def test_a_year_rollover_starts_again_at_1001(self):
        self.assertEqual(next_customer_code(2026), "AVG-26-1001")
        with transaction.atomic():
            self.assertEqual(next_customer_code(2027), "AVG-27-1001")

    def test_numbers_keep_counting_after_9999_without_padding(self):
        CustomerCodeCounter.objects.create(year=2030, last_number=9999)
        with transaction.atomic():
            self.assertEqual(next_customer_code(2030), "AVG-30-10000")

    def test_a_duplicate_customer_code_is_refused_by_the_database(self):
        customer = make_customer("Eta Works", "AV-0007")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(code="AV-0008", name="Copy", customer_code=customer.customer_code)

    def test_the_code_is_not_changed_by_a_posted_value(self):
        user = User.objects.create_user("kyc-user", "kyc-user@example.com", "x")
        self.client.force_login(user)
        customer = make_customer("Theta Works", "AV-0009")
        code = customer.customer_code
        response = self.client.post(reverse("kyc:edit", args=[customer.pk]), {
            "code": "AV-0009", "name": "Theta Works", "customer_code": "AVG-99-1",
            "country": "India", "preferred_transporter": "ARC", "departments": [],
            "all_departments": "on", "msme_applicable": "no",
        })
        self.assertIn(response.status_code, (200, 302))
        customer.refresh_from_db()
        self.assertEqual(customer.customer_code, code)


class CustomerCodeNeedsTransactionTests(TransactionTestCase):
    def test_the_code_is_only_generated_inside_a_transaction(self):
        with self.assertRaises(RuntimeError):
            next_customer_code(2026)


class CustomerCodeListAndSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", "viewer@example.com", "x")
        self.client.force_login(self.user)

    def test_the_list_shows_the_code_and_finds_customers_by_it(self):
        customer = make_customer("Iota Works", "AV-0010")
        response = self.client.get(reverse("kyc:list"), {"q": customer.customer_code})
        self.assertContains(response, customer.customer_code)
        self.assertContains(response, "Iota Works")

    def test_sorting_by_code_compares_running_numbers_not_text(self):
        # 1001 must come before 10000 (as numbers), although "10000" sorts before "1001" as text.
        make_customer("Kappa Works", "AV-0011")
        year = timezone.now().astimezone(IST).year
        CustomerCodeCounter.objects.update_or_create(year=year, defaults={"last_number": 9999})
        make_customer("Lambda Works", "AV-0012")
        make_customer("Mu Works", "AV-0013")
        response = self.client.get(reverse("kyc:list"), {"sort": "customer_code"})
        self.assertEqual(response.status_code, 200)
        numbers = [int(c.customer_code.split("-")[2]) for c in response.context["page"].object_list]
        self.assertEqual(numbers, [1001, 10000, 10001])

    def test_the_customer_lookup_accepts_either_code(self):
        customer = make_customer("Nu Works", "AV-0012")
        self.assertEqual(links.customer_for_code(customer.customer_code), customer)
        self.assertEqual(links.customer_for_code("av-0012"), customer)

    def test_the_form_page_shows_the_code_as_read_only_and_does_not_offer_it_as_a_field(self):
        customer = make_customer("Xi Works", "AV-0013")
        response = self.client.get(reverse("kyc:edit", args=[customer.pk]))
        self.assertContains(response, 'readonly')
        self.assertContains(response, customer.customer_code)
        self.assertNotIn("customer_code", response.context["form"].fields)


class CustomerCodeBackfillTests(TestCase):
    def test_existing_customers_are_numbered_oldest_first_and_the_counter_continues(self):
        migration = importlib.import_module("kyc.migrations.0011_customer_code")
        older = make_customer("Omicron Works", "AV-0014")
        newer = make_customer("Pi Works", "AV-0015")
        Customer.objects.filter(pk=older.pk).update(
            customer_code=None, created_at=datetime(2025, 3, 1, 10, 0, tzinfo=IST))
        Customer.objects.filter(pk=newer.pk).update(
            customer_code=None, created_at=datetime(2025, 6, 1, 10, 0, tzinfo=IST))
        CustomerCodeCounter.objects.all().delete()

        from django.apps import apps
        migration.assign_existing_codes(apps, None)

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.customer_code, "AVG-25-1001")
        self.assertEqual(newer.customer_code, "AVG-25-1002")
        self.assertEqual(CustomerCodeCounter.objects.get(year=2025).last_number, 1002)
        with transaction.atomic():
            self.assertEqual(next_customer_code(2025), "AVG-25-1003")

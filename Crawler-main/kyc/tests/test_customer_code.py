"""Customer codes (AVGNNNNXX): the format, the letters from the company name, one running number that never resets,
the locked counter, the form, the lists, and the backfill of existing customers."""
import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from kyc import links
from kyc.models import Customer, CustomerCodeCounter, code_letters, next_customer_code

User = get_user_model()
IST = ZoneInfo("Asia/Kolkata")


def make_customer(name, code=None, **extra):
    return Customer.objects.create(code=code or name.upper().replace(" ", "-")[:20], name=name, **extra)


class CodeLettersTests(TestCase):
    def test_the_first_two_letters_of_the_name(self):
        self.assertEqual(code_letters("Tata Steel Ltd"), "TA")
        self.assertEqual(code_letters("Sharma Constructions"), "SH")
        self.assertEqual(code_letters("Bharat Electricals"), "BH")

    def test_common_prefixes_are_skipped(self):
        self.assertEqual(code_letters("M/s Kumar Traders"), "KU")
        self.assertEqual(code_letters("M/S Kumar Traders"), "KU")
        self.assertEqual(code_letters("Messrs Ravi & Co"), "RA")
        self.assertEqual(code_letters("The Hindu Agencies"), "HI")

    def test_only_letters_count(self):
        self.assertEqual(code_letters("3M India"), "MI")
        self.assertEqual(code_letters("A-B Works"), "AB")

    def test_a_name_with_fewer_than_two_letters_is_padded_with_x(self):
        self.assertEqual(code_letters("Z"), "ZX")
        self.assertEqual(code_letters("The"), "XX")
        self.assertEqual(code_letters(""), "XX")

    def test_a_word_that_only_starts_with_the_is_kept(self):
        self.assertEqual(code_letters("Theatre Co"), "TH")


class CustomerCodeGenerationTests(TestCase):
    def test_a_new_customer_gets_the_next_number_and_the_letters_of_its_name(self):
        first = make_customer("Tata Steel Ltd", "AV-0001")
        second = make_customer("Sharma Constructions", "AV-0002")
        self.assertEqual((first.customer_number, first.customer_code), (1001, "AVG1001TA"))
        self.assertEqual((second.customer_number, second.customer_code), (1002, "AVG1002SH"))

    def test_one_number_is_shared_by_all_customers_even_with_the_same_letters(self):
        make_customer("Tata Steel Ltd", "AV-0003")
        motors = make_customer("Tata Motors", "AV-0004")
        self.assertEqual(motors.customer_code, "AVG1002TA")

    def test_prefixes_are_skipped_when_the_code_is_made(self):
        self.assertEqual(make_customer("M/s Kumar Traders", "AV-0005").customer_code, "AVG1001KU")

    def test_a_one_letter_name_gets_x(self):
        self.assertEqual(make_customer("Z", "AV-0006").customer_code, "AVG1001ZX")

    def test_two_customers_created_in_one_transaction_get_different_numbers(self):
        with transaction.atomic():
            first = make_customer("Alpha Works", "AV-0007")
            second = make_customer("Alpha Traders", "AV-0008")
        self.assertEqual((first.customer_number, second.customer_number), (1001, 1002))
        self.assertNotEqual(first.customer_code, second.customer_code)

    def test_an_edited_company_name_does_not_change_the_code(self):
        customer = make_customer("Gamma Works", "AV-0009")
        code = customer.customer_code
        customer.name = "Delta Limited"
        customer.save()
        customer.refresh_from_db()
        self.assertEqual(customer.customer_code, code)
        self.assertEqual(customer.customer_code, "AVG1001GA")

    def test_a_deleted_customer_number_is_never_reused(self):
        make_customer("Epsilon Works", "AV-0010")
        make_customer("Zeta Works", "AV-0011").delete()
        third = make_customer("Eta Works", "AV-0012")
        self.assertEqual((third.customer_number, third.customer_code), (1003, "AVG1003ET"))

    def test_numbers_keep_counting_after_9999_without_padding(self):
        CustomerCodeCounter.objects.update_or_create(pk=1, defaults={"last_number": 9999})
        self.assertEqual(make_customer("Tata Motors", "AV-0013").customer_code, "AVG10000TA")

    def test_a_duplicate_customer_code_is_refused_by_the_database(self):
        customer = make_customer("Theta Works", "AV-0014")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(code="AV-0015", name="Copy", customer_code=customer.customer_code)

    def test_the_code_is_not_changed_by_a_posted_value(self):
        user = User.objects.create_user("kyc-user", "kyc-user@example.com", "x")
        self.client.force_login(user)
        customer = make_customer("Iota Works", "AV-0016")
        code = customer.customer_code
        response = self.client.post(reverse("kyc:edit", args=[customer.pk]), {
            "code": "AV-0016", "name": "Iota Works", "customer_code": "AVG1ZZ",
            "country": "India", "preferred_transporter": "ARC", "departments": [],
            "all_departments": "on", "msme_applicable": "no",
        })
        self.assertIn(response.status_code, (200, 302))
        customer.refresh_from_db()
        self.assertEqual(customer.customer_code, code)


class CustomerCodeNeedsTransactionTests(TransactionTestCase):
    def test_the_code_is_only_generated_inside_a_transaction(self):
        with self.assertRaises(RuntimeError):
            next_customer_code("Kappa Works")


class CustomerCodeListAndSearchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", "viewer@example.com", "x")
        self.client.force_login(self.user)

    def test_the_list_shows_the_code_and_finds_customers_by_it(self):
        customer = make_customer("Kappa Works", "AV-0020")
        response = self.client.get(reverse("kyc:list"), {"q": customer.customer_code})
        self.assertContains(response, customer.customer_code)
        self.assertContains(response, "Kappa Works")

    def test_sorting_by_code_uses_the_number_not_the_text(self):
        # 1001 must come before 10000, although "10000" sorts before "1001" as text.
        make_customer("Lambda Works", "AV-0021")
        CustomerCodeCounter.objects.update_or_create(pk=1, defaults={"last_number": 9999})
        make_customer("Mu Works", "AV-0022")
        make_customer("Nu Works", "AV-0023")
        response = self.client.get(reverse("kyc:list"), {"sort": "customer_code"})
        self.assertEqual([c.customer_number for c in response.context["page"].object_list], [1001, 10000, 10001])
        response = self.client.get(reverse("kyc:list"), {"sort": "customer_code_desc"})
        self.assertEqual([c.customer_number for c in response.context["page"].object_list], [10001, 10000, 1001])

    def test_the_customer_lookup_accepts_either_code(self):
        customer = make_customer("Xi Works", "AV-0024")
        self.assertEqual(links.customer_for_code(customer.customer_code), customer)
        self.assertEqual(links.customer_for_code(customer.code), customer)

    def test_the_form_page_shows_the_code_as_read_only_and_does_not_offer_it_as_a_field(self):
        customer = make_customer("Omicron Works", "AV-0025")
        response = self.client.get(reverse("kyc:edit", args=[customer.pk]))
        self.assertContains(response, "readonly")
        self.assertContains(response, customer.customer_code)
        self.assertNotIn("customer_code", response.context["form"].fields)


class CustomerCodeBackfillTests(TestCase):
    def test_existing_customers_are_numbered_oldest_first_and_the_counter_continues(self):
        migration = importlib.import_module("kyc.migrations.0014_customer_code_format")
        newer = make_customer("Pi Works", "AV-0026")
        older = make_customer("Rho Traders", "AV-0027")
        Customer.objects.filter(pk=older.pk).update(
            customer_code="AVG-25-1001", customer_number=None,
            created_at=datetime(2025, 3, 1, 10, 0, tzinfo=IST))
        Customer.objects.filter(pk=newer.pk).update(
            customer_code="AVG-25-1002", customer_number=None,
            created_at=datetime(2025, 6, 1, 10, 0, tzinfo=IST))
        CustomerCodeCounter.objects.all().delete()
        CustomerCodeCounter.objects.create(pk=1, last_number=1000)

        migration.renumber_customers(apps, None)

        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual((older.customer_number, older.customer_code), (1001, "AVG1001RH"))
        self.assertEqual((newer.customer_number, newer.customer_code), (1002, "AVG1002PI"))
        self.assertEqual(CustomerCodeCounter.objects.get(pk=1).last_number, 1002)
        with transaction.atomic():
            self.assertEqual(next_customer_code("Sigma Works")[1], "AVG1003SI")

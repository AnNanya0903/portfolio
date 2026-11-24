"""Board search: every word in any field, "#" and punctuation ignored, exact number first, saved emails / KYC."""
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from enquiries.models import EnquiryContact
from enquiries.views import _search_enquiries

ROWS = [
    {"enquiry_id": "3355", "customer_name": "Acme Seals", "subject": "EPDM gasket for door", "status": "accepted",
     "project_name": "AP-7781", "received_at_dt": datetime(2026, 5, 1)},
    {"enquiry_id": "4100", "customer_name": "Beta Motors", "subject": "Silicone profile", "status": "open",
     "price_per_piece": 3355.5, "received_at_dt": datetime(2026, 5, 2)},
    {"enquiry_id": "4200", "customer_name": "Gamma  Industries", "subject": "Door gasket", "status": "open",
     "received_at_dt": datetime(2026, 5, 3)},
]


def ids(rows):
    return [r["enquiry_id"] for r in rows]


class SearchHelperTests(TestCase):
    def search(self, q):
        return ids(_search_enquiries([dict(r) for r in ROWS], q.lower()))

    def test_words_in_different_fields_and_any_order(self):
        self.assertEqual(self.search("acme door"), ["3355"])
        self.assertEqual(self.search("gasket acme"), ["3355"])

    def test_hash_is_ignored_and_exact_number_comes_first(self):
        self.assertEqual(self.search("#3355"), ["3355", "4100"])
        self.assertEqual(self.search("3355")[0], "3355")

    def test_punctuation_and_spacing_do_not_matter(self):
        self.assertEqual(self.search("ap7781"), ["3355"])
        self.assertEqual(self.search("AP 7781"), ["3355"])
        self.assertEqual(self.search("gamma industries"), ["4200"])

    def test_saved_customer_email_is_searched(self):
        EnquiryContact.objects.create(enquiry_id="4200", customer_email="buyer@gamma.example")
        self.assertEqual(self.search("buyer@gamma"), ["4200"])

    def test_no_match(self):
        self.assertEqual(self.search("nothing-like-this"), [])


class BoardSearchViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", "b@example.com", "x"))
        patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in ROWS]).start()
        # Django 4.2's test client cannot copy template contexts on Python 3.14; the HTML is all this test needs.
        patch("django.test.client.store_rendered_templates").start()
        self.addCleanup(patch.stopall)

    def test_board_filters_by_query(self):
        response = self.client.get(reverse("enquiries:board"), {"q": "#3355"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-enquiry-id="3355"')
        self.assertNotContains(response, 'data-enquiry-id="4200"')

    def test_search_with_open_status_and_empty_filters(self):
        # Same query string as the board's search form: blank year / month / payment / document filters.
        response = self.client.get(reverse("enquiries:board"), {
            "per_page": "50", "fin_year": "", "month": "", "status": "open", "payment": "", "document": "",
            "q": "PI Required"})
        self.assertEqual(response.status_code, 200)

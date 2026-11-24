"""Board filter "Document": PROFORMA INVOICE or Feasibility & Quotation Report shows only the enquiries with that file."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from enquiries.views import _has_document

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
ROWS = [
    {"enquiry_id": "P1", "status": "accepted", "customer_name": "Proforma Co",
     "general_attachments": [{"attachment_id": "a", "file_name": "Proforma Invoice.pdf"}]},
    {"enquiry_id": "F1", "status": "open", "customer_name": "Report Co",
     "general_attachments": [{"attachment_id": "b", "file_name": "Feasibility-and-Quotation.pdf"}]},
    {"enquiry_id": "N1", "status": "open", "customer_name": "No Doc Co",
     "general_attachments": [{"attachment_id": "c", "file_name": "drawing.dwg"}]},
    {"enquiry_id": "N2", "status": "open", "customer_name": "Bare Co", "general_attachments": []},
]


@override_settings(**STATIC)
@mock.patch("enquiries.views._get_api_root", return_value="http://svc")
@mock.patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in ROWS])
class DocumentFilterTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss", "b@example.com", "x"))

    def _ids(self, document=None):
        params = {"document": document} if document else {}
        page = self.client.get(reverse("enquiries:board"), params)
        return sorted(e["enquiry_id"] for e in page.context["enquiries"])

    def test_all_shows_every_enquiry(self, *_):
        self.assertEqual(self._ids(), ["F1", "N1", "N2", "P1"])

    def test_proforma_invoice_shows_only_enquiries_with_a_proforma(self, *_):
        self.assertEqual(self._ids("proforma"), ["P1"])

    def test_feasibility_report_shows_only_enquiries_with_that_report(self, *_):
        self.assertEqual(self._ids("feasibility"), ["F1"])

    def test_an_unknown_choice_is_ignored(self, *_):
        self.assertEqual(self._ids("bogus"), ["F1", "N1", "N2", "P1"])

    def test_the_dropdown_offers_the_two_documents(self, *_):
        page = self.client.get(reverse("enquiries:board"), {"document": "proforma"})
        self.assertContains(page, "PROFORMA INVOICE")
        self.assertContains(page, "Feasibility &amp; Quotation Report")
        self.assertContains(page, '<option value="proforma" selected>')

    def test_the_chosen_document_is_remembered_on_the_page(self, *_):
        page = self.client.get(reverse("enquiries:board"), {"document": "feasibility"})
        self.assertEqual(page.context["selected_document"], "feasibility")

    def test_name_match_ignores_case_and_checks_every_attachment(self, *_):
        self.assertTrue(_has_document({"general_attachments": [{"file_name": "PROFORMA invoice.PDF"}]}, ("proforma",)))
        self.assertTrue(_has_document({"general_attachments": [{"file_name": "x.pdf"}, {"file_name": "quotation-v2.pdf"}]}, ("quotation",)))
        self.assertFalse(_has_document({"general_attachments": []}, ("proforma",)))


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
@mock.patch("enquiries.views._get_api_root", return_value="http://svc")
@mock.patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in ROWS])
class StatusFilterDoesNotRememberTests(TestCase):
    """The browser must not put back an old Status choice (it made the filter show Accepted by itself)."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("boss2", "b2@example.com", "x"))

    def test_the_filters_do_not_let_the_browser_remember_a_choice(self, *_):
        html = self.client.get(reverse("enquiries:board")).content.decode()
        for name in ("statusFilter", "paymentFilter", "documentFilter"):
            start = html.index(f'id="{name}"')
            tag = html[html.rindex("<select", 0, start):html.index(">", start)]
            self.assertIn('autocomplete="off"', tag, name)
        self.assertIn('data-search-form="true" autocomplete="off"', html)

    def test_a_fresh_visit_shows_all_statuses(self, *_):
        page = self.client.get(reverse("enquiries:board"))
        self.assertEqual(page.context["selected_status"], "")
        self.assertIn('<option value="">All Statuses</option>', page.content.decode())

"""Every share link gets its own password: Avigiri plus 5 random digits (preview page, or the server if empty)."""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries.views import SHARE_PASSWORD_PREFIX, generate_share_password

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
ENQUIRY = {"enquiry_id": "ENQ-7", "status": "open", "subject": "Viton gaskets", "customer_name": "Deccan Chemicals",
           "customer_reference": "", "remarks": "", "attachments": []}
PATTERN = re.compile(r"^Avigiri\d{5}$")


class GeneratePasswordTests(TestCase):
    def test_password_is_avigiri_plus_five_digits_and_differs_per_link(self):
        self.assertEqual(SHARE_PASSWORD_PREFIX, "Avigiri")
        passwords = [generate_share_password() for _ in range(200)]
        for password in passwords:
            self.assertRegex(password, PATTERN)
        self.assertGreater(len(set(passwords)), 150)  # random, not one fixed password

    def test_preview_page_creates_a_new_password_each_time(self):
        source = get_template("enquiries/preview.html").template.source
        self.assertIn('value="" data-auto-password', source)  # nothing fixed in the page
        self.assertIn("if (!input.value) input.value = make();", source)
        self.assertIn("return 'Avigiri' +", source)
        self.assertIn("data-new-password", source)
        self.assertNotIn("Avigiri12345", source)


@override_settings(**STATIC)
class GenerateLinkPasswordTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("sales", "sales@avigiri.example", "x"))

    def _generate(self, password):
        pdf = SimpleUploadedFile("quote.pdf", b"%PDF-quote", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(ENQUIRY)):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-7", "share_password": password, "share_pdf": pdf,
                "subject": "Viton gaskets", "customer_name": "Deccan Chemicals"})
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response

    def test_empty_password_gets_a_new_one_for_each_link(self):
        first = self._generate("")
        second = self._generate("")
        for response in (first, second):
            self.assertRegex(response.context["share_password"], PATTERN)
            self.assertContains(response, response.context["share_password"])
        # Two links getting the same 5 digits by chance is a 1 in 100,000 event.
        self.assertNotEqual(first.context["share_password"], second.context["share_password"])

    def test_typed_password_is_kept(self):
        self.assertEqual(self._generate("Mine-123").context["share_password"], "Mine-123")

"""The share-link password is created automatically on the preview page (and by the server if it is empty)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries.views import SHARE_PASSWORD_ALPHABET, SHARE_PASSWORD_PREFIX, generate_share_password

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
ENQUIRY = {"enquiry_id": "ENQ-7", "status": "open", "subject": "Viton gaskets", "customer_name": "Deccan Chemicals",
           "customer_reference": "", "remarks": "", "attachments": []}


class GeneratePasswordTests(TestCase):
    def test_password_is_long_random_and_easy_to_read(self):
        passwords = {generate_share_password() for _ in range(50)}
        self.assertEqual(len(passwords), 50)
        self.assertEqual(SHARE_PASSWORD_PREFIX, "AG-")
        for password in passwords:
            self.assertTrue(password.startswith("AG-"), password)
            self.assertEqual(len(password), 11)
            self.assertTrue(set(password[3:]) <= set(SHARE_PASSWORD_ALPHABET))
        for look_alike in "0O1lI":
            self.assertNotIn(look_alike, SHARE_PASSWORD_ALPHABET)

    def test_preview_page_fills_in_the_password(self):
        source = get_template("enquiries/preview.html").template.source
        self.assertIn("data-auto-password", source)
        self.assertIn("data-new-password", source)
        self.assertIn("return 'AG-' +", source)
        self.assertNotIn('name="share_password" placeholder="Enter password" required', source)


@override_settings(**STATIC)
class EmptyPasswordIsCreatedTests(TestCase):
    def test_generate_link_without_password_creates_one(self):
        user = get_user_model().objects.create_superuser("sales", "sales@avigiri.example", "x")
        self.client.force_login(user)
        pdf = SimpleUploadedFile("quote.pdf", b"%PDF-quote", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(ENQUIRY)):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-7", "share_password": "", "share_pdf": pdf,
                "subject": "Viton gaskets", "customer_name": "Deccan Chemicals"})
        self.assertEqual(response.status_code, 200, response.content[:500])
        password = response.context["share_password"]
        self.assertTrue(password.startswith("AG-"), password)
        self.assertEqual(len(password), 11)
        self.assertContains(response, password)

    def test_typed_password_is_kept(self):
        user = get_user_model().objects.create_superuser("sales2", "sales2@avigiri.example", "x")
        self.client.force_login(user)
        pdf = SimpleUploadedFile("quote.pdf", b"%PDF-quote", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(ENQUIRY)):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-7", "share_password": "Mine-123", "share_pdf": pdf,
                "subject": "Viton gaskets", "customer_name": "Deccan Chemicals"})
        self.assertEqual(response.context["share_password"], "Mine-123")

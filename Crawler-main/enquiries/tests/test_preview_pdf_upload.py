"""Preview page: the quotation PDF can always be uploaded, also when the enquiry already has a PDF."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries.models import ShareLink

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
WITH_PDF = {"enquiry_id": "ENQ-9", "subject": "Gaskets", "customer_name": "Acme",
            "general_attachments": [{"attachment_id": "a1", "file_name": "customer-rfq.pdf",
                                     "content_type": "application/pdf"}]}
WITHOUT_PDF = {"enquiry_id": "ENQ-9", "subject": "Gaskets", "customer_name": "Acme", "general_attachments": []}


@override_settings(**STATIC)
class PreviewPdfUploadTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("sales", "s@avigiri.example", "x"))

    def _preview(self, enquiry):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(enquiry)):
            return self.client.get(reverse("enquiries:preview", args=["ENQ-9"]))

    def test_upload_shown_and_optional_when_enquiry_has_a_pdf(self):
        response = self._preview(WITH_PDF)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="share_pdf" accept="application/pdf">')
        self.assertContains(response, "<strong>customer-rfq.pdf</strong>")

    def test_upload_required_when_enquiry_has_no_pdf(self):
        response = self._preview(WITHOUT_PDF)
        self.assertContains(response, 'name="share_pdf" accept="application/pdf" required>')
        self.assertNotContains(response, "already attached to this enquiry")

    def test_uploaded_quotation_is_used_instead_of_the_existing_pdf(self):
        pdf = SimpleUploadedFile("Quotation-ENQ-9.pdf", b"%PDF-quotation", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITH_PDF)), \
             patch("enquiries.views.services.download_attachment") as download:
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "share_pdf": pdf, "subject": "Gaskets",
                "customer_name": "Acme"})
        self.assertEqual(response.status_code, 200)
        download.assert_not_called()
        data = signing.loads(ShareLink.objects.get().token, salt="enquiry-share")
        self.assertEqual(data["pdf_filename"], "Quotation-ENQ-9.pdf")

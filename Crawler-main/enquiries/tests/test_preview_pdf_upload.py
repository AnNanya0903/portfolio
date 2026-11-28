"""Preview page (Generate Link): without a quotation in the portal the PDF is uploaded, or picked on purpose from
the PDFs already attached to the enquiry; an attached PDF is never used silently."""
import base64
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries.models import EnquiryAttachment, ShareLink

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
WITH_PDF = {"enquiry_id": "ENQ-9", "subject": "Gaskets", "customer_name": "Acme",
            "general_attachments": [{"attachment_id": "a1", "file_name": "customer-rfq.pdf",
                                     "content_type": "application/pdf"}]}
WITHOUT_PDF = {"enquiry_id": "ENQ-9", "subject": "Gaskets", "customer_name": "Acme", "general_attachments": []}


@override_settings(**STATIC)
class PreviewPdfUploadTests(TestCase):
    def setUp(self):
        # The attachment storage is fixed when the model loads, so point it at a temporary folder for the test.
        folder = tempfile.mkdtemp()
        field = EnquiryAttachment._meta.get_field("file")
        original = field.storage
        field.storage = FileSystemStorage(location=folder)
        self.addCleanup(lambda: (setattr(field, "storage", original), shutil.rmtree(folder, ignore_errors=True)))
        self.client.force_login(get_user_model().objects.create_superuser("sales", "s@avigiri.example", "x"))

    def _preview(self, enquiry):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(enquiry)):
            return self.client.get(reverse("enquiries:preview", args=["ENQ-9"]))

    def test_upload_required_even_when_enquiry_has_a_pdf(self):
        response = self._preview(WITH_PDF)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="share_pdf" accept="application/pdf" required>')
        self.assertNotContains(response, "already attached to this enquiry")

    def test_no_silent_fallback_to_the_enquiry_pdf(self):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITH_PDF)), \
             patch("enquiries.views.services.download_attachment") as download:
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "subject": "Gaskets", "customer_name": "Acme"})
        self.assertEqual(response.status_code, 200)
        download.assert_not_called()
        self.assertFalse(ShareLink.objects.exists())
        self.assertContains(response, "Upload a PDF to generate the share link.")

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

    def test_attached_pdfs_are_offered_as_an_explicit_choice(self):
        self.assertNotContains(self._preview(WITHOUT_PDF), 'name="attached_pdf"')
        EnquiryAttachment.objects.create(enquiry_id="ENQ-9", file=ContentFile(b"%PDF-q", name="qt.pdf"),
                                         file_name="QT-101.pdf", content_type="application/pdf",
                                         category=EnquiryAttachment.CAT_QUOTATION)
        EnquiryAttachment.objects.create(enquiry_id="ENQ-9", file=ContentFile(b"xls", name="cost.xlsx"),
                                         file_name="cost.xlsx", content_type="application/vnd.ms-excel")
        response = self._preview(WITH_PDF)
        self.assertContains(response, 'name="attached_pdf"')
        self.assertContains(response, "QT-101.pdf")
        self.assertContains(response, "customer-rfq.pdf")
        self.assertNotContains(response, "cost.xlsx")

    def test_pdf_added_on_the_negotiation_page_generates_the_link(self):
        item = EnquiryAttachment.objects.create(enquiry_id="ENQ-9", file=ContentFile(b"%PDF-qt", name="qt.pdf"),
                                                file_name="QT-101.pdf", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITHOUT_PDF)),              patch("enquiries.views.services.download_attachment") as download:
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "attached_pdf": f"file:{item.pk}", "subject": "Gaskets",
                "customer_name": "Acme"})
        self.assertEqual(response.status_code, 200)
        download.assert_not_called()
        data = signing.loads(ShareLink.objects.get().token, salt="enquiry-share")
        self.assertEqual(data["pdf_filename"], "QT-101.pdf")
        self.assertEqual(base64.b64decode(data["pdf_content_b64"]), b"%PDF-qt")

    def test_another_enquirys_file_cannot_be_shared(self):
        item = EnquiryAttachment.objects.create(enquiry_id="ENQ-1", file=ContentFile(b"%PDF-x", name="x.pdf"),
                                                file_name="other.pdf", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITHOUT_PDF)):
            self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "attached_pdf": f"file:{item.pk}", "subject": "Gaskets",
                "customer_name": "Acme"})
        self.assertFalse(ShareLink.objects.exists())

    def test_enquiry_service_pdf_generates_the_link(self):
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITH_PDF)),              patch("enquiries.views.services.download_attachment",
                   return_value=(b"%PDF-attached", "attachment", "application/octet-stream")) as download:
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "attached_pdf": "svc:a1", "subject": "Gaskets",
                "customer_name": "Acme"})
        self.assertEqual(response.status_code, 200)
        download.assert_called_once()
        data = signing.loads(ShareLink.objects.get().token, salt="enquiry-share")
        self.assertEqual(data["pdf_filename"], "customer-rfq.pdf")
        self.assertEqual(data["pdf_content_type"], "application/pdf")

    def test_attached_pdf_that_cannot_be_downloaded_asks_for_an_upload(self):
        from enquiries import services
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(WITH_PDF)),              patch("enquiries.views.services.download_attachment", side_effect=services.EnquiryServiceError("down")):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-9", "attached_pdf": "svc:a1", "subject": "Gaskets",
                "customer_name": "Acme"}, follow=True)
        self.assertFalse(ShareLink.objects.exists())
        self.assertContains(response, "could not be downloaded")

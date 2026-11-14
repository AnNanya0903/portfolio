"""The quotation PDF's Voucher No. is read when the link is generated and used in the email subject."""
import io
import unittest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from enquiries import share_email

try:  # only needed to build sample PDFs for these tests
    from reportlab.pdfgen import canvas
except ImportError:  # pragma: no cover
    canvas = None

ENQUIRY = {"enquiry_id": "ENQ-7", "subject": "Viton gaskets"}


def _pdf(lines):
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    y = 800
    for line in lines:
        page.drawString(50, y, line)
        y -= 18
    page.save()
    return buffer.getvalue()


class VoucherFromTextTests(SimpleTestCase):
    def test_number_on_the_same_line(self):
        for text, expected in (
            ("Quotation\nVoucher No.: AG/25-26/014\nDated 12-Nov-26", "AG/25-26/014"),
            ("Voucher No. AG-QT-0042 Dated 12-Nov-2026", "AG-QT-0042"),
            ("VOUCHER NO : 1182", "1182"),
            ("Voucher Number - QTN_77.", "QTN_77"),
        ):
            self.assertEqual(share_email.voucher_number_from_text(text), expected, text)

    def test_number_on_the_line_below_like_tally(self):
        text = "AVIGIRI\nVoucher No. Dated\nAG/QT/25-26/118 12-Nov-26\nMode/Terms of Payment"
        self.assertEqual(share_email.voucher_number_from_text(text), "AG/QT/25-26/118")
        self.assertEqual(share_email.voucher_number_from_text("Voucher No.\n\n557\nDated\n12-Nov-26"), "557")

    def test_a_date_is_never_taken_as_the_number(self):
        self.assertEqual(share_email.voucher_number_from_text("Voucher No. Dated\n12-Nov-26 AG/118"), "AG/118")
        self.assertEqual(share_email.voucher_number_from_text("Voucher No. Dated\n12/11/2026"), "")

    def test_nothing_found(self):
        for text in ("", "Quotation for gaskets\nTotal 1,200.00", "Voucher No.\nBuyer\nTerms\nDelivery\n123"):
            self.assertEqual(share_email.voucher_number_from_text(text), "", text)


class VoucherFromPdfTests(SimpleTestCase):
    def test_unreadable_pdf_gives_nothing(self):
        for data in (b"", b"%PDF-quote", b"not a pdf at all"):
            self.assertEqual(share_email.voucher_number(data), "")

    @unittest.skipIf(canvas is None, "reportlab is not installed")
    def test_reads_the_number_from_a_real_pdf(self):
        pdf = _pdf(["AVIGIRI SEALING SOLUTIONS", "Quotation", "Voucher No.   AG/QT/25-26/118", "Dated   12-Nov-26"])
        self.assertEqual(share_email.voucher_number(pdf), "AG/QT/25-26/118")
        self.assertEqual(share_email.voucher_number(_pdf(["Quotation", "Total 1,200.00"])), "")


class SubjectTests(SimpleTestCase):
    def test_subject_uses_the_voucher_number(self):
        subject, body = share_email.default_message("ENQ-7", ENQUIRY, "https://x/s/1", "AG-abc", "AG/QT/25-26/118")
        self.assertEqual(subject, "Quotation AG/QT/25-26/118 - Viton gaskets")
        self.assertIn("quotation no. AG/QT/25-26/118, reference ENQ-7", body)

    def test_without_a_voucher_number_the_enquiry_number_is_used(self):
        subject, body = share_email.default_message("ENQ-7", ENQUIRY, "https://x/s/1", "AG-abc")
        self.assertEqual(subject, "Quotation ENQ-7 - Viton gaskets")
        self.assertIn("(reference ENQ-7)", body)
        self.assertNotIn("quotation no.", body)



@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class GenerateLinkSubjectTests(TestCase):
    """Generate Link with a quotation PDF: the 'Send to customer' subject carries its Voucher No."""

    def _generate(self, pdf_bytes):
        user = get_user_model().objects.create_superuser("sales", "sales@avigiri.example", "x")
        self.client.force_login(user)
        pdf = SimpleUploadedFile("quotation.pdf", pdf_bytes, content_type="application/pdf")
        enquiry = {"enquiry_id": "ENQ-7", "status": "open", "subject": "Viton gaskets",
                   "customer_name": "Deccan Chemicals", "customer_reference": "", "remarks": "", "attachments": []}
        with patch("enquiries.views.services.fetch_enquiry", return_value=enquiry):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-7", "share_password": "AG-test1234", "share_pdf": pdf,
                "subject": "Viton gaskets", "customer_name": "Deccan Chemicals"})
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response

    @unittest.skipIf(canvas is None, "reportlab is not installed")
    def test_subject_has_the_voucher_number_from_the_uploaded_pdf(self):
        response = self._generate(_pdf(["Quotation", "Voucher No. Dated", "AG/QT/25-26/118 12-Nov-26"]))
        email = response.context["email"]
        self.assertEqual(email["voucher_no"], "AG/QT/25-26/118")
        self.assertEqual(email["subject"], "Quotation AG/QT/25-26/118 - Viton gaskets")
        self.assertContains(response, 'value="Quotation AG/QT/25-26/118 - Viton gaskets"')
        self.assertContains(response, "was read from the quotation PDF")

    def test_pdf_without_a_voucher_number_keeps_the_enquiry_number(self):
        response = self._generate(b"%PDF-quote")
        email = response.context["email"]
        self.assertEqual(email["voucher_no"], "")
        self.assertEqual(email["subject"], "Quotation ENQ-7 - Viton gaskets")
        self.assertContains(response, "No Voucher No. could be read")

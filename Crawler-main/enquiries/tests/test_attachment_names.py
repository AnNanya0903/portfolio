"""Send to customer: every attachment has a real file name and type (not a nameless "attachment")."""
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from enquiries import share_email
from enquiries.models import ShareLink
from enquiries.views import _create_share_link_from_values, _download_primary_pdf

PDF = b"%PDF-1.4 quotation"


class FileNameTests(TestCase):
    def test_generic_names_get_a_real_one(self):
        self.assertEqual(share_email.proper_file_name("attachment", content=PDF, fallback="Quotation-ENQ-7"),
                         "Quotation-ENQ-7.pdf")
        self.assertEqual(share_email.proper_file_name("", preferred="rfq.pdf", content=PDF), "rfq.pdf")
        self.assertEqual(share_email.proper_file_name("attachment", preferred="drawing.dwg", content=b"x"),
                         "drawing.dwg")
        self.assertEqual(share_email.proper_file_name("quote", content=PDF), "quote.pdf")  # extension added

    def test_a_real_name_is_kept(self):
        self.assertEqual(share_email.proper_file_name("Sales Order_AG_PI_2627.pdf", preferred="other.pdf",
                                                      content=PDF), "Sales Order_AG_PI_2627.pdf")
        self.assertEqual(share_email.proper_file_name('"a b.pdf"', content=PDF), "a b.pdf")

    def test_content_type(self):
        self.assertEqual(share_email.proper_content_type("application/octet-stream", "attachment", PDF),
                         "application/pdf")
        self.assertEqual(share_email.proper_content_type("application/octet-stream", "plan.dwg", b"AC10"),
                         "application/octet-stream" if not __import__("mimetypes").guess_type("plan.dwg")[0]
                         else __import__("mimetypes").guess_type("plan.dwg")[0])
        self.assertEqual(share_email.proper_content_type("image/png; charset=x", "a.png", b"\x89PNG"), "image/png")


def _share(pdf_filename, enquiry_id="ENQ-3304", content_type="application/octet-stream"):
    result = _create_share_link_from_values(RequestFactory().get("/"), {"subject": "Gaskets"},
                                            share_password="Secret-123", enquiry_id=enquiry_id, pdf_bytes=PDF,
                                            pdf_filename=pdf_filename, pdf_content_type=content_type)
    return signing_loads(ShareLink.objects.get(token=result["token"]).token)


def signing_loads(token):
    from django.core import signing
    return signing.loads(token, salt="enquiry-share")


class QuotationAttachmentTests(TestCase):
    def test_the_old_nameless_attachment_is_sent_as_a_named_pdf(self):
        data = _share("attachment")  # how most existing share links were stored
        attachment = share_email.quotation_attachment(data, "ENQ-3304")
        self.assertEqual((attachment["filename"], attachment["mime_type"]),
                         ("Quotation-ENQ-3304.pdf", "application/pdf"))
        self.assertEqual(attachment["content"], PDF)

    def test_voucher_number_names_the_file(self):
        attachment = share_email.quotation_attachment(_share("attachment"), "ENQ-3304", "AG/Q-77")
        self.assertEqual(attachment["filename"], "Quotation-AG-Q-77.pdf")

    def test_an_uploaded_pdf_keeps_its_own_name(self):
        attachment = share_email.quotation_attachment(_share("Quotation Acme.pdf", content_type="application/pdf"),
                                                      "ENQ-3304")
        self.assertEqual(attachment["filename"], "Quotation Acme.pdf")


class DownloadedFilesTests(TestCase):
    def test_enquiry_pdf_used_for_the_link_gets_the_enquiry_file_name(self):
        record = {"general_attachments": [{"attachment_id": "a1", "file_name": "RFQ Acme.pdf",
                                           "content_type": "application/pdf"}]}
        with patch("enquiries.views.services.download_attachment",
                   return_value=(PDF, "attachment", "application/octet-stream")):
            content, name, content_type = _download_primary_pdf("http://api", "ENQ-1", record, None)
        self.assertEqual((name, content_type), ("RFQ Acme.pdf", "application/pdf"))

    def test_ticked_documents_get_their_names_and_types(self):
        docs = [{"id": "a2", "name": "drawing.pdf", "download_url": ""}, {"id": "a3", "name": "notes", "download_url": ""}]
        results = iter([(PDF, "attachment", "application/octet-stream"), (b"hello", "attachment", "")])
        with patch("enquiries.share_email.services.download_attachment", side_effect=lambda *a, **k: next(results)):
            files = share_email.download_documents("http://api", "ENQ-1", docs, ["a2", "a3"])
        self.assertEqual([(f["filename"], f["mime_type"]) for f in files],
                         [("drawing.pdf", "application/pdf"), ("notes", "application/octet-stream")])

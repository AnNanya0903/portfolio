"""'Send to customer' on the quotation share link: link + password in the email, quotation PDF and chosen
documents attached, sent from the employee's Gmail."""
import base64
from email import message_from_bytes, policy
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.gmail_service import GmailAPIError, GmailService
from enquiries import share_email
from enquiries.models import EnquiryContact, ShareLink
from enquiries.views import _create_share_link_from_values

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
ENQUIRY = {
    "enquiry_id": "ENQ-7", "subject": "Viton gaskets", "customer_name": "Deccan Chemicals",
    "general_attachments": [
        {"attachment_id": "a1", "file_name": "quote.pdf", "content_type": "application/pdf"},
        {"attachment_id": "a2", "file_name": "drawing.dwg", "size": 2048},
        {"attachment_id": "a3", "file_name": "test-cert.pdf", "download_url": "/enquiries/ENQ-7/attachments/a3"},
    ],
}


def make_share(password="Secret-123", enquiry_id="ENQ-7"):
    request = RequestFactory().get("/")
    result = _create_share_link_from_values(request, {"subject": "Viton gaskets"}, share_password=password,
                                            enquiry_id=enquiry_id, pdf_bytes=b"%PDF-quote", pdf_filename="quote.pdf")
    return ShareLink.objects.get(token=result["token"]), result


class DefaultMessageTests(TestCase):
    def test_body_has_link_password_and_house_style(self):
        subject, body = share_email.default_message("ENQ-7", ENQUIRY, "https://x/share/1", "Secret-123")
        self.assertEqual(subject, "Quotation ENQ-7 - Viton gaskets")
        self.assertTrue(body.startswith("Dear Sir/Madam,"))
        self.assertIn("https://x/share/1\nPassword: Secret-123", body)
        self.assertTrue(body.endswith("Best regards,\nAVIGIRI Team"))

    def test_documents_skip_the_quotation_itself(self):
        docs = share_email.enquiry_documents(ENQUIRY, skip_name="Quote.pdf")
        self.assertEqual([d["id"] for d in docs], ["a2", "a3"])


@override_settings(**STATIC)
class SendShareEmailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("sales", "sales@avigiri.example", "x")
        self.client.force_login(self.user)
        self.record, _ = make_share()
        patch("enquiries.views.services.fetch_enquiry", return_value=dict(ENQUIRY)).start()
        self.download = patch("enquiries.share_email.services.download_attachment",
                              side_effect=lambda root, url, **k: (b"DOC:" + url.encode(), url.rsplit("/", 1)[-1],
                                                                  "application/pdf")).start()
        self.send = patch("enquiries.views.GmailService.send_email", return_value={"id": "m1"}).start()
        self.addCleanup(patch.stopall)

    def _post(self, **data):
        payload = {"share_id": self.record.id, "share_password": "Secret-123", "to": "Buyer@Deccan.example",
                   "cc": "boss@avigiri.example", "subject": "Quotation ENQ-7", "body": "Dear Sir/Madam, ..."}
        payload.update(data)
        return self.client.post(reverse("enquiries:send_share_email"), payload)

    def test_sends_quotation_and_ticked_documents(self):
        response = self._post(documents=["a3"])
        self.assertContains(response, "Quotation emailed to buyer@deccan.example (CC: boss@avigiri.example)")
        to, subject, body = self.send.call_args.args
        self.assertEqual((to, subject), ("buyer@deccan.example", "Quotation ENQ-7"))
        self.assertEqual(self.send.call_args.kwargs["cc"], ["boss@avigiri.example"])
        files = self.send.call_args.kwargs["attachments"]
        self.assertEqual([f["filename"] for f in files], ["quote.pdf", "test-cert.pdf"])  # the enquiry's own file name
        self.assertEqual(files[0]["content"], b"%PDF-quote")
        self.download.assert_called_once()
        contact = EnquiryContact.objects.get(enquiry_id="ENQ-7")
        self.assertEqual((contact.customer_email, contact.follow_up_cc), ("buyer@deccan.example", "boss@avigiri.example"))

    def test_saved_customer_email_is_not_overwritten(self):
        EnquiryContact.objects.create(enquiry_id="ENQ-7", customer_email="orders@deccan.example")
        self._post()
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-7").customer_email, "orders@deccan.example")

    def test_wrong_password_or_unknown_link_sends_nothing(self):
        self._post(share_password="nope")
        self._post(share_id="missing")
        self.send.assert_not_called()

    def test_bad_recipient_or_unknown_document_shows_error(self):
        self.assertContains(self._post(to="not-an-email"), "one valid customer email")
        self.assertContains(self._post(cc="x@y.example, bad"), "not a valid CC")
        self.assertContains(self._post(documents=["zzz"]), "no longer on this enquiry")
        self.assertContains(self._post(subject=""), "fill in the subject")
        self.send.assert_not_called()
        self.assertFalse(EnquiryContact.objects.exists())

    def test_too_large_attachments_are_refused(self):
        with patch.object(share_email, "MAX_ATTACHMENT_BYTES", 5):
            self.assertContains(self._post(), "Please untick some documents")
        self.send.assert_not_called()

    def test_gmail_error_is_shown(self):
        self.send.side_effect = GmailAPIError("Authentication failed")
        response = self._post()
        self.assertContains(response, "Gmail could not send the email")
        self.assertFalse(EnquiryContact.objects.exists())

    def test_local_test_mode_says_not_sent(self):
        self.send.return_value = {"id": "demo", "local_demo": True}
        self.assertContains(self._post(), "Test mode: the email to buyer@deccan.example")


@override_settings(**STATIC)
class GenerateLinkShowsEmailFormTests(TestCase):
    def test_form_is_prefilled(self):
        user = get_user_model().objects.create_superuser("sales", "sales@avigiri.example", "x")
        self.client.force_login(user)
        EnquiryContact.objects.create(enquiry_id="ENQ-7", customer_email="buyer@deccan.example")
        pdf = SimpleUploadedFile("quote.pdf", b"%PDF-quote", content_type="application/pdf")
        with patch("enquiries.views.services.fetch_enquiry", return_value=dict(ENQUIRY)):
            response = self.client.post(reverse("enquiries:generate_link"), {
                "mode": "update", "enquiry_id": "ENQ-7", "share_password": "Secret-123", "share_pdf": pdf,
                "subject": "Viton gaskets", "customer_name": "Deccan Chemicals"})
        self.assertEqual(response.status_code, 200, response.content[:500])
        email = response.context["email"]
        self.assertEqual(email["to"], "buyer@deccan.example")
        self.assertIn("Password: Secret-123", email["body"])
        self.assertEqual([d["id"] for d in email["documents"]], ["a2", "a3"])
        self.assertContains(response, "Send to customer")


class GmailSendEmailTests(TestCase):
    def test_new_email_with_cc_and_attachment(self):
        svc = GmailService.__new__(GmailService)
        svc.user = MagicMock(username="sales", is_authenticated=False)
        svc.service = MagicMock()
        svc.send_email("buyer@deccan.example", "Quotation ENQ-7", "Hello", cc=["boss@avigiri.example"],
                       attachments=[{"filename": "quote.pdf", "mime_type": "application/pdf", "content": b"%PDF"}])
        raw = svc.service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
        msg = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        self.assertEqual((msg["To"], msg["Cc"], msg["Subject"]),
                         ("buyer@deccan.example", "boss@avigiri.example", "Quotation ENQ-7"))
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["quote.pdf"])

    def test_local_demo_sends_nothing(self):
        svc = GmailService.__new__(GmailService)
        svc.user = MagicMock(username="sales")
        svc.service = MagicMock()
        with patch.object(GmailService, "_local_demo_enabled", return_value=True):
            result = svc.send_email("buyer@deccan.example", "Q", "Hello")
        self.assertTrue(result["local_demo"])
        svc.service.users.assert_not_called()

"""KYC by shareable link: tokens, expiry, OTP limits, uploads, permissions and status changes."""
import io
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from kyc import link_mail, link_pdf
from kyc import link_requests as svc
from kyc.link_requests import Actor, KycLinkError
from kyc.link_security import hash_token, sniff_mime
from kyc.models import (Customer, CustomerContact, EmailLog, KycDetails, KycOtp, KycRequest, KycRequestAuditLog,
                        KycRequestDocument)

KEY = Fernet.generate_key().decode()
PRIVATE_DIR = tempfile.mkdtemp(prefix="kyc-private-test-")
BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
            KYC_LINK_DRY_RUN=True, KYC_LINK_EXPIRY_HOURS=72, KYC_LINK_REMINDER_HOURS=48,
            KYC_FIELD_ENCRYPTION_KEYS=KEY, KYC_PRIVATE_DIR=PRIVATE_DIR, KYC_PRIVATE_BUCKET="", KYC_LINK_BASE_URL="",
            KYC_LINK_SENDER="")
User = get_user_model()

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(out, format="PNG")
    return out.getvalue()


PNG = _png()

GOOD = {
    "company_name": "Acme Seals Pvt Ltd", "company_address": "12 Industrial Area\nBengaluru 560058",
    "billing_address": "12 Industrial Area\nBengaluru 560058", "delivery_address": "Plot 4, Hosur Road\nBengaluru 560068",
    "gstin": "29ABCDE1234F1Z5", "msme_applicable": "YES", "udyam_aadhar_no": "udyam-ka-03-0012345",
    "transportation": "VRL-TO PAY-DOOR", "preferred_transporter": "VRL", "delivery_type": "DOOR",
    "contact_name": "Meera Rao", "contact_designation": "Purchase Manager",
    "contact_email": "meera@acme.example, stores@acme.example", "contact_phone": "98450 12345",
    "special_requirements": "Deliver before 5 PM", "pan": "ABCDE1234F", "account_holder_name": "Acme Seals Pvt Ltd",
    "account_number": "123456789012", "ifsc": "HDFC0001234", "bank_name": "HDFC Bank",
}


def make_user(name, email):
    return User.objects.create_user(name, email, "x")


def pdf(name="doc.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@override_settings(**BASE)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(PRIVATE_DIR, ignore_errors=True)

    def setUp(self):
        cache.clear()
        call_command("seed_departments", stdout=io.StringIO())
        self.sales = make_user("rabin", "rabin@avigiri.com")
        self.accounts = make_user("accounts", "accounts@avigiri.com")
        self.top = make_user("apai", "apai@avigiri.com")
        self.design = make_user("design", "design@avigiri.com")
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals", city="Bengaluru")
        CustomerContact.objects.create(customer=self.customer, category="sales_marketing", name="Meera",
                                       email="meera@acme.example", is_purchaser=True)

    def create(self, user=None) -> svc.Issued:
        return svc.create(self.customer, "meera@acme.example", Actor.staff(user or self.sales))

    def fill_everything(self, req, actor):
        self.assertEqual(svc.save_draft(req, GOOD, actor), {})
        for doc_type in ("gst_certificate", "pan_card", "cancelled_cheque"):
            svc.upload_document(req, doc_type, pdf(), actor)

    def verify_in_browser(self, token):
        """Send and enter the OTP through the public endpoints (test mode shows the code)."""
        r = self.client.post(reverse("kyc:link_otp_send", args=[token]))
        self.assertEqual(r.status_code, 200, r.content)
        code = r.json()["testCode"]
        r = self.client.post(reverse("kyc:link_otp_verify", args=[token]), {"code": code},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)


class TokenTests(Base):
    def test_only_the_hash_of_the_token_is_stored(self):
        issued = self.create()
        req = issued.request
        self.assertEqual(len(issued.token), 43)  # token_urlsafe(32)
        self.assertEqual(req.token_hash, hash_token(issued.token))
        self.assertNotIn(issued.token, str(KycRequest.objects.filter(pk=req.pk).values().first()))
        self.assertTrue(issued.link.endswith(f"/kyc/form/{issued.token}/"))
        self.assertEqual(svc.find_by_token(issued.token).pk, req.pk)

    def test_unknown_or_tampered_tokens_find_nothing(self):
        issued = self.create()
        self.assertIsNone(svc.find_by_token(issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B")))
        self.assertIsNone(svc.find_by_token(""))
        self.assertIsNone(svc.find_by_token("x" * 500))
        self.assertEqual(self.client.get(reverse("kyc:link_form", args=["nope"])).status_code, 404)

    def test_link_expires_after_72_hours(self):
        issued = self.create()
        self.assertEqual(svc.link_state(issued.request), "open")
        delta = issued.request.expires_at - issued.request.sent_at
        self.assertEqual(delta, timedelta(hours=72))
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=73)):
            page = self.client.get(reverse("kyc:link_form", args=[issued.token]))
            self.assertContains(page, "This link has expired")
        req = KycRequest.objects.get(pk=issued.request.pk)
        self.assertEqual(req.status, KycRequest.STATUS_EXPIRED)
        self.assertEqual(svc.link_state(req), "expired")

    @override_settings(KYC_LINK_EXPIRY_HOURS=5)
    def test_expiry_is_configurable(self):
        req = self.create().request
        self.assertEqual(req.expires_at - req.sent_at, timedelta(hours=5))

    def test_resend_ends_the_old_link_and_gives_a_fresh_expiry(self):
        first = self.create()
        second = svc.resend(first.request, Actor.staff(self.sales))
        self.assertNotEqual(first.token, second.token)
        self.assertIsNone(svc.find_by_token(first.token))
        self.assertEqual(svc.find_by_token(second.token).pk, first.request.pk)
        self.assertTrue(KycRequestAuditLog.objects.filter(request=first.request, action="resent").exists())

    def test_revoked_link_shows_a_message(self):
        issued = self.create()
        svc.revoke(issued.request, Actor.staff(self.sales))
        self.assertEqual(KycRequest.objects.get(pk=issued.request.pk).status, KycRequest.STATUS_REVOKED)
        self.assertContains(self.client.get(reverse("kyc:link_form", args=[issued.token])), "This link is not valid",
                            status_code=404)

    def test_one_open_request_per_customer(self):
        self.create()
        with self.assertRaises(KycLinkError):
            self.create()

    def test_known_details_are_prefilled(self):
        self.customer.gstin = "29ABCDE1234F1Z5"
        self.customer.pan = "ABCDE1234F"
        self.customer.save()
        details = KycDetails.objects.get(request=self.create().request)
        self.assertEqual(details.company_name, "Acme Seals")
        self.assertEqual(details.gstin, "29ABCDE1234F1Z5")
        self.assertEqual(details.pan_last4, "234F")
        self.assertNotIn("ABCDE1234F", details.pan_encrypted)
        self.assertEqual(details.contact_email, "meera@acme.example")
        self.assertEqual(details.preferred_transporter, "ARC")  # the paper form's default
        self.assertEqual(details.delivery_address, details.company_address)

    def test_live_email_log_never_holds_the_token(self):
        with override_settings(KYC_LINK_DRY_RUN=False), \
                mock.patch("kyc.link_mail.mailer._send_raw", return_value="gmail-1") as send, \
                mock.patch("kyc.link_mail.GmailService"):
            issued = self.create()
        self.assertEqual(issued.send.status, link_mail.SENT)
        self.assertIn(issued.token, send.call_args.args[1].body)
        log = EmailLog.objects.get(customer=self.customer)
        self.assertNotIn(issued.token, log.body)
        self.assertIn("AVIGIRI Team", log.body)

    def test_a_failed_email_closes_the_link(self):
        with override_settings(KYC_LINK_DRY_RUN=False), \
                mock.patch("kyc.link_mail.mailer._send_raw", side_effect=link_mail.GmailAPIError("no")), \
                mock.patch("kyc.link_mail.GmailService"):
            issued = self.create()
        self.assertEqual(issued.send.status, link_mail.FAILED)
        self.assertEqual(svc.link_state(KycRequest.objects.get(pk=issued.request.pk)), "expired")


class OtpTests(Base):
    def setUp(self):
        super().setUp()
        self.issued = self.create()
        self.req = self.issued.request
        self.customer_actor = Actor.customer("1.2.3.4")

    def test_form_data_is_hidden_until_the_otp_is_verified(self):
        url = reverse("kyc:link_form", args=[self.issued.token])
        page = self.client.get(url)
        self.assertContains(page, "Email me the code")
        self.assertNotContains(page, "kycFormConfig")
        r = self.client.put(url, {"values": {"company_name": "X"}}, content_type="application/json")
        self.assertEqual(r.status_code, 403)
        self.verify_in_browser(self.issued.token)
        self.assertContains(self.client.get(url), "kycFormConfig")
        self.assertEqual(KycRequest.objects.get(pk=self.req.pk).status, KycRequest.STATUS_OPENED)

    def test_otp_is_stored_hashed_and_expires_after_10_minutes(self):
        result = svc.send_otp(self.req, self.customer_actor)
        otp = KycOtp.objects.get(request=self.req)
        self.assertNotEqual(otp.otp_hash, result.test_code)
        self.assertNotIn(result.test_code, otp.otp_hash)
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=11)):
            self.assertFalse(svc.verify_otp(self.req, result.test_code, self.customer_actor).ok)

    def test_five_wrong_codes_lock_the_otp(self):
        code = svc.send_otp(self.req, self.customer_actor).test_code
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertFalse(svc.verify_otp(self.req, wrong, self.customer_actor).ok)
        result = svc.verify_otp(self.req, code, self.customer_actor)
        self.assertFalse(result.ok)
        self.assertIn("Too many", result.message)
        self.assertEqual(KycOtp.objects.get(request=self.req).attempts, 5)

    def test_a_new_code_replaces_the_old_one(self):
        old = svc.send_otp(self.req, self.customer_actor).test_code
        new = svc.send_otp(self.req, self.customer_actor).test_code
        if old != new:
            self.assertFalse(svc.verify_otp(self.req, old, self.customer_actor).ok)
        self.assertTrue(svc.verify_otp(self.req, new, self.customer_actor).ok)

    def test_at_most_five_codes_an_hour(self):
        for _ in range(5):
            self.assertTrue(svc.send_otp(self.req, self.customer_actor).ok)
        self.assertFalse(svc.send_otp(self.req, self.customer_actor).ok)

    def test_otp_endpoints_are_rate_limited(self):
        url = reverse("kyc:link_otp_verify", args=[self.issued.token])
        codes = [self.client.post(url, {"code": "123456"}, content_type="application/json").status_code
                 for _ in range(16)]
        self.assertEqual(codes[-1], 429)

    def test_a_new_link_needs_a_new_otp(self):
        self.verify_in_browser(self.issued.token)
        second = svc.resend(self.req, Actor.staff(self.sales))
        page = self.client.get(reverse("kyc:link_form", args=[second.token]))
        self.assertContains(page, "Email me the code")


class UploadTests(Base):
    def setUp(self):
        super().setUp()
        self.req = self.create().request
        self.actor = Actor.customer()

    def test_pdf_and_png_are_accepted_and_renamed(self):
        doc = svc.upload_document(self.req, "gst_certificate", pdf("my gst.pdf"), self.actor)
        self.assertEqual(doc.mime_type, "application/pdf")
        self.assertEqual(doc.original_name, "my gst.pdf")
        self.assertRegex(doc.storage_key, r"^kyc-requests/[0-9a-f]{32}\.pdf$")
        png = svc.upload_document(self.req, "pan_card",
                                  SimpleUploadedFile("pan.png", PNG, content_type="image/png"), self.actor)
        self.assertEqual(png.mime_type, "image/png")
        self.assertEqual(KycRequest.objects.get(pk=self.req.pk).status, KycRequest.STATUS_IN_PROGRESS)

    def test_type_is_decided_by_the_content_not_the_name(self):
        fake = SimpleUploadedFile("cheque.pdf", b"MZ\x90\x00 this is an exe", content_type="application/pdf")
        with self.assertRaisesMessage(KycLinkError, "Only PDF, JPG and PNG"):
            svc.upload_document(self.req, "cancelled_cheque", fake, self.actor)
        self.assertEqual(sniff_mime(b"GIF89a"), "")
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")

    def test_files_over_5_mb_are_refused(self):
        big = SimpleUploadedFile("big.pdf", PDF + b"0" * (5 * 1024 * 1024), content_type="application/pdf")
        with self.assertRaisesMessage(KycLinkError, "larger than 5 MB"):
            svc.upload_document(self.req, "gst_certificate", big, self.actor)

    def test_a_new_upload_replaces_the_old_file(self):
        svc.upload_document(self.req, "gst_certificate", pdf(), self.actor)
        svc.upload_document(self.req, "gst_certificate", pdf("second.pdf"), self.actor)
        self.assertEqual(list(KycRequestDocument.objects.filter(request=self.req).values_list("original_name",
                                                                                             flat=True)),
                         ["second.pdf"])

    def test_documents_are_only_for_signed_in_staff_and_each_download_is_logged(self):
        doc = svc.upload_document(self.req, "gst_certificate", pdf(), self.actor)
        url = reverse("kyc:link_document", args=[doc.pk])
        self.assertNotEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.accounts)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(KycRequestAuditLog.objects.filter(action="document_downloaded").exists())


class ValidationTests(Base):
    def setUp(self):
        super().setUp()
        self.req = self.create().request

    def test_formats_are_checked_on_the_server(self):
        errors = svc.save_draft(self.req, {"gstin": "29ABCDE1234F1Z", "pan": "ABCD1234F", "ifsc": "HDFC1001234",
                                           "contact_email": "not-an-email", "contact_phone": "12345",
                                           "company_name": "Fine Name"}, Actor.customer())
        self.assertEqual(set(errors), {"gstin", "pan", "ifsc", "contact_email", "contact_phone"})
        self.assertEqual(KycDetails.objects.get(request=self.req).company_name, "Fine Name")

    def test_values_are_normalised(self):
        svc.save_draft(self.req, {"gstin": "29abcde1234f1z5", "contact_phone": "+91 98450-12345"}, Actor.customer())
        details = KycDetails.objects.get(request=self.req)
        self.assertEqual(details.gstin, "29ABCDE1234F1Z5")
        self.assertEqual(details.contact_phone, "+919845012345")

    def test_pan_and_account_number_are_encrypted_at_rest(self):
        svc.save_draft(self.req, {"pan": "ABCDE1234F", "account_number": "123456789012"}, Actor.customer())
        details = KycDetails.objects.get(request=self.req)
        self.assertNotIn("ABCDE1234F", details.pan_encrypted)
        self.assertNotIn("123456789012", details.account_number_encrypted)
        self.assertEqual((details.pan_last4, details.account_last4), ("234F", "9012"))

    def test_aadhaar_numbers_are_refused_in_free_text(self):
        errors = svc.save_draft(self.req, {"company_address": "Aadhaar 1234 5678 9012",
                                           "udyam_aadhar_no": "1234 5678 9012"}, Actor.customer())
        self.assertIn("company_address", errors)
        self.assertIn("Aadhaar", errors["udyam_aadhar_no"])

    def test_paper_form_fields_are_checked(self):
        errors = svc.save_draft(self.req, {"msme_applicable": "maybe", "preferred_transporter": "XYZ",
                                           "delivery_type": "DOOR", "udyam_aadhar_no": "UDYAM-KA-3-12",
                                           "contact_email": "a@x.example, nope"}, Actor.customer())
        self.assertEqual(set(errors), {"msme_applicable", "preferred_transporter", "udyam_aadhar_no", "contact_email"})
        details = KycDetails.objects.get(request=self.req)
        self.assertEqual(details.delivery_type, "DOOR")

    def test_several_contact_emails_are_kept(self):
        svc.save_draft(self.req, {"contact_email": "Mohideen@aysh.example; imran@aysh.example"}, Actor.customer())
        self.assertEqual(KycDetails.objects.get(request=self.req).contact_email,
                         "mohideen@aysh.example, imran@aysh.example")

    def test_udyam_is_required_only_for_msme_and_transporter_name_only_for_other(self):
        actor = Actor.customer()
        svc.save_draft(self.req, {**GOOD, "msme_applicable": "YES", "udyam_aadhar_no": "",
                                  "preferred_transporter": "OTHER"}, actor)
        problems = svc.problems_before_submit(KycRequest.objects.get(pk=self.req.pk))
        self.assertIn("udyam_aadhar_no", problems)
        self.assertIn("other_transporter", problems)
        svc.save_draft(self.req, {"msme_applicable": "NO", "preferred_transporter": "ARC"}, actor)
        problems = svc.problems_before_submit(KycRequest.objects.get(pk=self.req.pk))
        self.assertNotIn("udyam_aadhar_no", problems)
        self.assertNotIn("other_transporter", problems)

    def test_payment_terms_are_set_by_staff_only(self):
        svc.save_draft(self.req, {"payment_terms": "60 days credit"}, Actor.customer())
        self.assertEqual(KycRequest.objects.get(pk=self.req.pk).payment_terms, "")
        with self.assertRaises(KycLinkError):
            svc.set_payment_terms(self.req, "100 % Advance", Actor.staff(self.design))
        svc.set_payment_terms(self.req, "100 %  Advance", Actor.staff(self.sales))
        req = KycRequest.objects.get(pk=self.req.pk)
        self.assertEqual(req.payment_terms, "100 % Advance")
        self.assertEqual(svc.form_data(req, Actor.customer())["paymentTerms"], "100 % Advance")

    def test_submit_needs_every_field_document_and_consent(self):
        problems = svc.submit(self.req, consent=False, actor=Actor.customer())
        self.assertIn("consent", problems)
        self.assertIn("account_number", problems)
        self.assertIn("cancelled_cheque", problems)
        self.assertEqual(KycRequest.objects.get(pk=self.req.pk).status, KycRequest.STATUS_SENT)


class FlowTests(Base):
    def submit_as_customer(self):
        issued = self.create()
        actor = Actor.customer()
        self.fill_everything(issued.request, actor)
        self.assertEqual(svc.submit(issued.request, True, actor), {})
        return issued, KycRequest.objects.get(pk=issued.request.pk)

    def test_customer_flow_through_the_public_endpoints(self):
        issued = self.create()
        token = issued.token
        url = reverse("kyc:link_form", args=[token])
        self.assertContains(self.client.get(url), "Email me the code")
        self.verify_in_browser(token)
        r = self.client.put(url, {"values": GOOD}, content_type="application/json")
        self.assertEqual(r.json()["errors"], {})
        self.assertEqual(r.json()["masked"]["pan"], "XXXXXX234F")
        for doc_type in ("gst_certificate", "pan_card", "cancelled_cheque"):
            r = self.client.post(reverse("kyc:link_documents", args=[token]), {"doc_type": doc_type, "file": pdf()})
            self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post(reverse("kyc:link_submit", args=[token]), {"consent": True},
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        req = KycRequest.objects.get(pk=issued.request.pk)
        self.assertEqual(req.status, KycRequest.STATUS_SUBMITTED)
        self.assertIsNotNone(KycDetails.objects.get(request=req).consent_given_at)
        # Locked: the link now only thanks the customer, and saving is refused.
        page = self.client.get(url)
        self.assertContains(page, "have been submitted")
        self.assertContains(page, "Download KYC form (PDF)")  # same browser: OTP still verified
        self.assertEqual(self.client.put(url, {"values": GOOD}, content_type="application/json").status_code, 410)
        actions = set(KycRequestAuditLog.objects.filter(request=req).values_list("action", flat=True))
        self.assertTrue({"created", "sent", "opened", "otp_sent", "otp_verified", "draft_saved", "submitted",
                         "team_notified"} <= actions)

    def test_approve_fills_only_the_customers_empty_fields(self):
        self.customer.gstin = "29ZZZZZ9999Z1Z5"
        self.customer.save()
        _, req = self.submit_as_customer()
        svc.review(req, "approve", Actor.staff(self.accounts))
        req.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(req.status, KycRequest.STATUS_APPROVED)
        self.assertEqual(req.reviewed_by, self.accounts)
        self.assertEqual(self.customer.gstin, "29ZZZZZ9999Z1Z5")  # already filled: kept
        self.assertEqual(self.customer.pan, "ABCDE1234F")  # empty: filled from the KYC
        self.assertEqual(self.customer.kyc_status, Customer.KYC_VERIFIED)
        self.assertEqual((self.customer.transportation, self.customer.preferred_transporter,
                          self.customer.delivery_type), ("VRL-TO PAY-DOOR", "VRL", "DOOR"))
        self.assertTrue(self.customer.msme_applicable)
        self.assertEqual(self.customer.udyam_aadhar_no, "UDYAM-KA-03-0012345")
        self.assertTrue(self.customer.address.startswith("12 Industrial Area"))
        # Meera is already a contact, so no duplicate is added.
        self.assertEqual(self.customer.contacts.count(), 1)

    def test_approve_adds_a_new_contact_person(self):
        self.customer.contacts.all().delete()
        _, req = self.submit_as_customer()
        svc.review(req, "approve", Actor.staff(self.top))
        contact = self.customer.contacts.get()
        self.assertEqual((contact.name, contact.email), ("Meera Rao", "meera@acme.example, stores@acme.example"))
        self.assertTrue(contact.is_purchaser)

    def test_pdf_follows_the_paper_form_and_leaves_out_pan_and_bank(self):
        from pypdf import PdfReader

        _, req = self.submit_as_customer()
        svc.set_payment_terms(req, "100 % Advance", Actor.staff(self.sales))
        self.client.force_login(self.accounts)
        response = self.client.get(reverse("kyc:link_request_pdf", args=[req.pk]))
        self.assertEqual(response["Content-Type"], "application/pdf")
        text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
        for expected in ("Customer Registration Form", "DELIVERY ADDRESS / CONSIGNEE", "100 % Advance",
                         "VRL-TO PAY-DOOR", "UDYAM-KA-03-0012345", "SIGNATURE OF AUTHORISED SIGNATORY",
                         "Meera Rao", "XXXXXX234F", "FOR AVIGIRI OFFICE USE ONLY"):
            self.assertIn(expected, text)
        self.assertNotIn("DA/AC/01", text)  # the old document-control box is gone
        self.assertNotIn("ABCDE1234F", text.replace("29ABCDE1234F1Z5", ""))  # PAN only masked
        self.assertNotIn("123456789012", text)
        sheet = link_pdf.request_sheet(req)
        self.assertEqual((sheet.delivery, sheet.msme), ("DOOR", "YES"))  # ticked on the form
        self.assertEqual(dict(sheet.contacts)["Purchase *"].name, "Meera Rao")

    def test_customer_downloads_the_pdf_and_uploads_the_signed_copy(self):
        issued, req = self.submit_as_customer()
        pdf_url = reverse("kyc:link_pdf", args=[issued.token])
        signed_url = reverse("kyc:link_signed", args=[issued.token])
        self.assertEqual(self.client.get(pdf_url).status_code, 403)  # OTP first
        self.verify_in_browser(issued.token)
        page = self.client.get(reverse("kyc:link_form", args=[issued.token]))
        self.assertContains(page, "Download KYC form (PDF)")
        self.assertTrue(self.client.get(pdf_url).content.startswith(b"%PDF"))
        r = self.client.post(signed_url, {"file": pdf("signed.pdf")})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(svc.signed_form(req).original_name, "signed.pdf")
        self.client.force_login(self.accounts)
        self.assertContains(self.client.get(reverse("kyc:link_detail", args=[req.pk])), "Signed copy uploaded")

    def test_signed_copy_only_after_submit(self):
        req = self.create().request
        with self.assertRaisesMessage(KycLinkError, "Submit the KYC form first"):
            svc.upload_signed_form(req, pdf(), Actor.customer())

    def test_reject_reopens_only_the_chosen_fields_with_a_new_link(self):
        issued, req = self.submit_as_customer()
        with self.assertRaisesMessage(KycLinkError, "reason"):
            svc.review(req, "reject", Actor.staff(self.top), fields=["ifsc"])
        rejected = svc.review(req, "reject", Actor.staff(self.top), reason="IFSC does not match the cheque",
                              fields=["ifsc", "cancelled_cheque"])
        req.refresh_from_db()
        self.assertEqual(req.status, KycRequest.STATUS_REJECTED)
        self.assertEqual(req.rejected_fields, ["ifsc", "cancelled_cheque"])
        self.assertNotEqual(rejected.token, issued.token)
        self.assertEqual(req.expires_at - req.sent_at, timedelta(hours=72))
        actor = Actor.customer()
        self.assertEqual(svc.form_data(req, actor)["rejectReason"], "IFSC does not match the cheque")
        svc.save_draft(req, {"company_name": "Changed", "ifsc": "ICIC0000001"}, actor)
        details = KycDetails.objects.get(request=req)
        self.assertEqual(details.company_name, "Acme Seals Pvt Ltd")  # not reopened: unchanged
        self.assertEqual(details.ifsc, "ICIC0000001")
        with self.assertRaisesMessage(KycLinkError, "cannot be changed"):
            svc.upload_document(req, "pan_card", pdf(), actor)
        self.assertEqual(svc.submit(req, True, actor), {})
        self.assertEqual(KycRequest.objects.get(pk=req.pk).status, KycRequest.STATUS_SUBMITTED)

    def test_only_a_submitted_form_can_be_reviewed(self):
        req = self.create().request
        with self.assertRaisesMessage(KycLinkError, "Only a submitted form"):
            svc.review(req, "approve", Actor.staff(self.accounts))

    def test_expiry_job_and_reminders(self):
        open_req = self.create().request
        other = Customer.objects.create(code="AV-0002", name="Beta Rubber")
        old = svc.create(other, "b@beta.example", Actor.staff(self.sales)).request
        KycRequest.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        KycRequest.objects.filter(pk=open_req.pk).update(sent_at=timezone.now() - timedelta(hours=49))
        result = svc.run_jobs()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["reminders"], {"sent": 1, "failed": 0})
        self.assertEqual(KycRequest.objects.get(pk=old.pk).status, KycRequest.STATUS_EXPIRED)
        self.assertEqual(svc.run_jobs()["reminders"], {"sent": 0, "failed": 0})  # once per link

    def test_cron_url_only_answers_the_scheduler(self):
        url = reverse("kyc:link_cron")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url, HTTP_X_APPENGINE_CRON="true").status_code, 200)


class StaffFillAndPermissionTests(Base):
    def staff_submit(self):
        issued = self.create()
        actor = Actor.staff(self.accounts)
        req = svc.start_staff_fill(issued.request, actor)
        self.fill_everything(req, actor)
        self.assertEqual(svc.submit(req, True, actor), {})
        return issued, KycRequest.objects.get(pk=req.pk)

    def test_staff_fill_closes_the_customer_link_and_records_who(self):
        issued, req = self.staff_submit()
        self.assertIsNone(svc.find_by_token(issued.token))
        self.assertEqual(req.source, KycRequest.SOURCE_STAFF)
        self.assertEqual(req.filled_by, self.accounts)
        self.assertIsNotNone(req.filled_at)
        self.assertTrue(KycRequestAuditLog.objects.filter(request=req, action="staff_fill_started").exists())
        self.assertTrue(KycRequestAuditLog.objects.filter(request=req, action="revoked").exists())

    def test_the_staff_member_who_filled_it_cannot_approve(self):
        _, req = self.staff_submit()
        self.assertFalse(svc.can_approve(self.accounts, req))
        with self.assertRaisesMessage(KycLinkError, "someone else must approve"):
            svc.review(req, "approve", Actor.staff(self.accounts))
        svc.review(req, "approve", Actor.staff(self.top))
        self.assertEqual(KycRequest.objects.get(pk=req.pk).status, KycRequest.STATUS_APPROVED)

    def test_filled_by_badge_and_approve_button(self):
        _, req = self.staff_submit()
        self.client.force_login(self.accounts)
        page = self.client.get(reverse("kyc:link_detail", args=[req.pk]))
        self.assertContains(page, "Filled by accounts on")
        self.assertContains(page, "another reviewer must approve it")
        self.client.force_login(self.top)
        self.assertContains(self.client.get(reverse("kyc:link_detail", args=[req.pk])), 'value="approve"')

    def test_only_one_staff_member_fills_at_a_time(self):
        req = self.create().request
        svc.start_staff_fill(req, Actor.staff(self.accounts))
        with self.assertRaises(KycLinkError):
            svc.start_staff_fill(req, Actor.staff(self.sales))
        with self.assertRaises(KycLinkError):
            svc.save_draft(req, {"company_name": "X"}, Actor.staff(self.sales))

    def test_customer_confirmation_of_staff_entry(self):
        _, req = self.staff_submit()
        issued = svc.request_confirmation(req, Actor.staff(self.accounts))
        self.assertEqual(svc.link_state(issued.request), "confirm")
        self.verify_in_browser(issued.token)
        page = self.client.get(reverse("kyc:link_form", args=[issued.token]))
        self.assertContains(page, "XXXXXX234F")
        self.assertNotContains(page, ">ABCDE1234F<")  # the GSTIN contains the PAN, so match the PAN cell
        self.assertEqual(self.client.post(reverse("kyc:link_confirm", args=[issued.token])).status_code, 200)
        self.assertIsNotNone(KycRequest.objects.get(pk=req.pk).confirmed_by_customer_at)

    def test_who_may_send_review_and_reveal(self):
        self.assertTrue(svc.can_send(self.sales))
        self.assertFalse(svc.can_send(self.design))
        self.assertFalse(svc.can_review(self.sales))
        self.assertTrue(svc.can_review(self.accounts) and svc.can_review(self.top))
        with self.assertRaises(KycLinkError):
            self.create(user=self.design)
        _, req = FlowTests.submit_as_customer(self)
        with self.assertRaises(KycLinkError):
            svc.review(req, "approve", Actor.staff(self.sales))
        with self.assertRaises(KycLinkError):
            svc.reveal(req, "pan", Actor.staff(self.sales))

    def test_reveal_shows_the_full_number_and_is_logged(self):
        _, req = FlowTests.submit_as_customer(self)
        self.client.force_login(self.sales)
        page = self.client.get(reverse("kyc:link_detail", args=[req.pk]))
        self.assertContains(page, "XXXXXXXX9012")
        self.assertNotContains(page, "123456789012")
        self.assertNotContains(page, "kl-reveal")
        url = reverse("kyc:link_reveal", args=[req.pk])
        self.assertEqual(self.client.post(url, {"field": "account_number"}, content_type="application/json")
                         .status_code, 403)
        self.client.force_login(self.accounts)
        r = self.client.post(url, {"field": "account_number"}, content_type="application/json")
        self.assertEqual(r.json()["value"], "123456789012")
        log = KycRequestAuditLog.objects.get(request=req, action="revealed")
        self.assertEqual((log.actor, log.metadata["field"]), (self.accounts, "account_number"))

    def test_send_button_on_customer_page_and_dashboard(self):
        self.client.force_login(self.sales)
        self.assertContains(self.client.get(reverse("kyc:detail", args=[self.customer.pk])), "Send KYC Link")
        r = self.client.post(reverse("kyc:link_new", args=[self.customer.pk]), {"recipient": "meera@acme.example"})
        req = KycRequest.objects.get(customer=self.customer)
        self.assertRedirects(r, reverse("kyc:link_detail", args=[req.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("kyc:link_list") + "?status=sent")
        self.assertContains(page, "Acme Seals")
        self.assertContains(page, "data-expires")
        self.assertNotContains(self.client.get(reverse("kyc:link_list") + "?status=approved"), "Acme Seals</a>")


class CustomerPdfTests(Base):
    def test_customer_page_opens_the_details_as_a_kyc_form_pdf(self):
        from pypdf import PdfReader

        c = self.customer
        c.address, c.state, c.pincode = "I-FLOOR, 5TH CROSS", "Tamil Nadu", "620018"
        c.gstin, c.pan = "33AKNPT5683R1ZZ", "ABCDE1234F"
        c.transportation, c.preferred_transporter, c.delivery_type = "VRL-TO PAY-DOOR", "VRL", "DOOR"
        c.save()
        c.contacts.update(email="a@aysh.example, b@aysh.example", phone="9884453944")
        self.client.force_login(self.design)  # every department can see customer details
        page = self.client.get(reverse("kyc:detail", args=[c.pk]))
        url = reverse("kyc:customer_pdf", args=[c.pk])
        self.assertContains(page, url)
        response = self.client.get(url)
        self.assertEqual(response["Content-Type"], "application/pdf")
        text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
        for expected in ("Customer Registration Form", "Acme Seals", "I-FLOOR, 5TH CROSS", "33AKNPT5683R1ZZ",
                         "VRL-TO PAY-DOOR", "a@aysh.example,", "b@aysh.example", "9884453944", "620018",
                         "SIGNATURE OF AUTHORISED SIGNATORY"):
            self.assertIn(expected, text)
        self.assertNotIn("ABCDE1234F", text.replace("33AKNPT5683R1ZZ", ""))  # PAN only masked
        sheet = link_pdf.customer_sheet(c)
        self.assertEqual(sheet.delivery, "DOOR")
        self.assertTrue(all(a.same for a in sheet.addresses[1:]))  # one address: "Same as" boxes ticked

    def test_payment_terms_come_from_the_kyc_request(self):
        from pypdf import PdfReader

        req = self.create().request
        svc.set_payment_terms(req, "100 % Advance", Actor.staff(self.sales))
        self.client.force_login(self.sales)
        pages = PdfReader(io.BytesIO(self.client.get(reverse("kyc:customer_pdf", args=[self.customer.pk])).content)
                          ).pages
        self.assertEqual(len(pages), 2)
        self.assertIn("100 % Advance", " ".join(p.extract_text() for p in pages))

    def test_the_pdf_needs_a_signed_in_user(self):
        self.assertNotEqual(self.client.get(reverse("kyc:customer_pdf", args=[self.customer.pk])).status_code, 200)


ACCOUNTS_CONTACT = {"department": "accounts", "name": "Ravi Kumar", "designation": "Accounts Head",
                    "email": "Ravi@Acme.example", "phone": "98450 54321"}


class ExtraContactTests(Base):
    """The ➕ on the contact details step: more contact persons, each from a department."""

    def setUp(self):
        super().setUp()
        self.req = self.create().request
        self.actor = Actor.customer()

    def test_rows_are_tidied_and_saved_and_blank_rows_dropped(self):
        blank = {"department": "", "name": " ", "designation": "", "email": "", "phone": ""}
        errors = svc.save_draft(self.req, {"extra_contacts": [ACCOUNTS_CONTACT, blank]}, self.actor)
        self.assertEqual(errors, {})
        saved = KycDetails.objects.get(request=self.req).extra_contacts
        self.assertEqual(saved, [{"department": "accounts", "name": "Ravi Kumar", "designation": "Accounts Head",
                                  "email": "ravi@acme.example", "phone": "9845054321"}])
        self.assertEqual(svc.form_data(self.req, self.actor)["values"]["extra_contacts"], saved)

    def test_a_row_needs_department_name_email_and_phone_and_is_saved_whole(self):
        svc.save_draft(self.req, {"extra_contacts": [ACCOUNTS_CONTACT]}, self.actor)
        bad = [ACCOUNTS_CONTACT, {"department": "purchase", "name": "Anil", "email": "not-an-email", "phone": "12"}]
        errors = svc.save_draft(self.req, {"extra_contacts": bad, "company_name": "Acme"}, self.actor)
        self.assertEqual(set(errors), {"extra_contacts.1.department", "extra_contacts.1.email",
                                       "extra_contacts.1.phone"})
        details = KycDetails.objects.get(request=self.req)
        self.assertEqual(len(details.extra_contacts), 1)  # the earlier list is kept
        self.assertEqual(details.company_name, "Acme")  # other fields are still saved

    def test_at_most_ten_more_contacts(self):
        rows = [dict(ACCOUNTS_CONTACT, email=f"p{i}@acme.example") for i in range(11)]
        self.assertIn("extra_contacts", svc.save_draft(self.req, {"extra_contacts": rows}, self.actor))

    def test_approve_adds_each_person_in_their_department_once(self):
        self.fill_everything(self.req, self.actor)
        technical = dict(ACCOUNTS_CONTACT, department="technical", name="Asha", email="asha@acme.example")
        same_as_meera = dict(ACCOUNTS_CONTACT, name="Meera again", email="meera@acme.example")
        svc.save_draft(self.req, {"extra_contacts": [ACCOUNTS_CONTACT, technical, same_as_meera]}, self.actor)
        self.assertEqual(svc.submit(self.req, True, self.actor), {})
        svc.review(KycRequest.objects.get(pk=self.req.pk), "approve", Actor.staff(self.accounts))
        got = {(c.category, c.name) for c in self.customer.contacts.all()}
        self.assertEqual(got, {("sales_marketing", "Meera"), ("accounts", "Ravi Kumar"), ("technical", "Asha")})

    def test_extra_contacts_show_on_review_pdf_and_can_be_reopened(self):
        from pypdf import PdfReader

        self.fill_everything(self.req, self.actor)
        svc.save_draft(self.req, {"extra_contacts": [ACCOUNTS_CONTACT]}, self.actor)
        svc.submit(self.req, True, self.actor)
        self.client.force_login(self.accounts)
        page = self.client.get(reverse("kyc:link_detail", args=[self.req.pk]))
        self.assertContains(page, "Other contact persons")
        self.assertContains(page, "Accounts: Ravi Kumar (Accounts Head)")
        self.assertContains(page, 'value="extra_contacts"')
        pages = PdfReader(io.BytesIO(self.client.get(reverse("kyc:link_request_pdf", args=[self.req.pk])).content)
                          ).pages
        self.assertIn("Ravi Kumar", " ".join(p.extract_text() for p in pages))
        svc.review(KycRequest.objects.get(pk=self.req.pk), "reject", Actor.staff(self.accounts),
                   reason="Add the purchase head", fields=["extra_contacts"])
        req = KycRequest.objects.get(pk=self.req.pk)
        self.assertEqual(svc.editable_keys(req, self.actor), ["extra_contacts"])

    def test_the_form_page_offers_the_plus_button(self):
        token = svc.resend(self.req, Actor.staff(self.sales)).token
        self.verify_in_browser(token)
        page = self.client.get(reverse("kyc:link_form", args=[token]))
        self.assertContains(page, "Add contact person")
        self.assertContains(page, '"extraContacts"')
        # The uploaded files must not replace the list of documents to upload (the Documents step needs it).
        self.assertContains(page, '"documents": [{"key": "gst_certificate"')
        self.assertContains(page, '"uploaded": {}')


class OnePageFormTests(Base):
    """The form is one page after the OTP; the old /preview/ address goes to it."""

    def setUp(self):
        super().setUp()
        self.issued = self.create()
        self.url = reverse("kyc:link_form", args=[self.issued.token])

    def test_the_form_needs_the_otp_then_shows_one_page_with_save_and_submit(self):
        self.assertNotContains(self.client.get(self.url), "kycFormConfig")
        self.verify_in_browser(self.issued.token)
        page = self.client.get(self.url)
        for expected in ('id="kfSave"', "Save draft", 'id="kfSubmit"', '"upload": "/kyc/form/', '"submit": "/kyc/form/'):
            self.assertContains(page, expected)
        self.assertNotContains(page, "Save and continue")

    def test_old_preview_address_goes_to_the_form(self):
        self.assertRedirects(self.client.get(reverse("kyc:link_preview", args=[self.issued.token])), self.url,
                             fetch_redirect_response=False)

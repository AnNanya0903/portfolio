"""Plan -> approve -> the CRO emails the quotation in the customer's own thread; the customer's answer; the PI hook.

Gmail is replaced by FakeGmail, which keeps the raw MIME of every email "sent" so the headers can be checked.
"""
import base64
import email
import email.policy
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from enquiries.models import EnquiryContact, FollowUpStart
from quotations import mailer, sending, workflow
from quotations.models import Quotation, QuotationEvent
from quotations.tests.test_quotations import Base, pdf

ENQ = "ENQ-77"


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    """Just enough of GmailService and its googleapiclient `service` for the quotation mailer."""

    def __init__(self, thread=None, messages=None, inbox=None, fail=None):
        self.thread = thread or []
        self.stored = messages or {}
        self.inbox = inbox or []
        self.fail = fail
        self.sent = []  # (payload, parsed MIME)
        self.queries = []
        self.service = self

    # googleapiclient chain: service.users().messages().send(...).execute()
    def users(self):
        return self

    def messages(self):  # noqa: D401 - mirrors the API
        return self

    def send(self, userId, body):
        def run():
            if self.fail:
                raise self.fail
            parsed = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=email.policy.default)
            self.sent.append((body, parsed))
            return {"id": f"g{len(self.sent)}", "threadId": body.get("threadId") or "T-new"}
        return _Call(run)

    def get(self, userId, id, format="full", metadataHeaders=None):
        def run():
            index = int(id[1:]) - 1
            return {"payload": {"headers": [{"name": "Message-ID", "value": self.sent[index][1]["Message-ID"]}]}}
        return _Call(run)

    # GmailService methods used by quotations.threads
    def _local_demo_enabled(self):
        return False

    def authenticate(self):
        return True

    def get_thread_messages(self, thread_id, limit=50):
        return list(self.thread)

    def get_message(self, message_id):
        return self.stored[message_id]

    def search_messages(self, query, limit=20):
        self.queries.append(query)
        return list(self.inbox)

    # helpers for the tests
    @property
    def last(self):
        return self.sent[-1][1]

    def _has_attachment(self, message):
        return any(part.get_filename() for part in message.walk())

    @property
    def quotation_index(self):
        """Index in `sent` of the last quotation email (the one with the PDF; the password email follows it)."""
        for index in range(len(self.sent) - 1, -1, -1):
            if self._has_attachment(self.sent[index][1]):
                return index
        raise AssertionError("no quotation email was sent")

    @property
    def quotation(self):
        return self.sent[self.quotation_index][1]

    @property
    def quotation_payload(self):
        return self.sent[self.quotation_index][0]

    def last_attachment(self):
        for part in self.quotation.walk():
            if part.get_filename():
                return part.get_filename(), part.get_payload(decode=True)
        return None, None


class SendBase(Base):
    def setUp(self):
        super().setUp()
        self.costing.email = "sagar@avigiri.com"
        self.costing.save()
        self.quotation = self._quotation(enquiry_id=ENQ)
        self.gmail = FakeGmail()
        patcher = mock.patch("quotations.mailer.gmail_service", side_effect=lambda: self.gmail)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The enquiry service: ENQ exists, with the customer's original subject.
        self.enquiry = {"enquiry_id": ENQ, "subject": "Need door seals", "customer_name": "Acme Seals"}
        patcher = mock.patch("quotations.enquiry_link.fetch", side_effect=lambda *a, **k: dict(self.enquiry))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _approve(self):
        workflow.approve(self.quotation, user=self.boss)
        self.quotation.refresh_from_db()

    def _contact(self, **fields):
        contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=ENQ)
        for key, value in fields.items():
            setattr(contact, key, value)
        contact.save()
        return contact

    def _link(self, subject="Need door seals", thread_id="T1", message_id="<c1@customer.com>", references=""):
        return self._contact(customer_email="buyer@customer.com", email_subject=subject, gmail_thread_id=thread_id,
                             email_message_id=message_id, email_references=references)

    def _post(self, user, name, **data):
        self.client.force_login(user)
        data.setdefault("to", "buyer@customer.com")
        data.setdefault("body", "Dear buyer, please find our quotation.")
        return self.client.post(reverse(name, args=[self.quotation.pk]), data)


class RoleTests(SendBase):
    def test_only_top_management_or_approvers_may_approve(self):
        self.client.force_login(self.costing)
        response = self.client.post(reverse("quotations:decide", args=[self.quotation.pk]), {"decision": "approve"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "pending")

    def test_only_the_cro_may_send_resend_and_record_the_answer(self):
        self._approve()
        for name in ("quotations:send", "quotations:resend", "quotations:respond", "quotations:mark_sent"):
            self.assertEqual(self._post(self.costing, name, answer="accepted").status_code, 403, name)
            self.assertEqual(self._post(self.boss, name, answer="accepted").status_code, 403, name)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "approved")

    def test_send_button_only_for_the_cro_and_only_when_approved(self):
        self.client.force_login(self.cro)
        url = reverse("quotations:detail", args=[self.quotation.pk])
        self.assertNotContains(self.client.get(url), "data-send-button")
        self._approve()
        self.assertContains(self.client.get(url), "data-send-button")
        self.client.force_login(self.costing)
        self.assertNotContains(self.client.get(url), "data-send-button")


class StatusTests(SendBase):
    def test_wrong_status_is_refused(self):
        response = self._post(self.cro, "quotations:send")  # still pending approval
        self.assertEqual(response.status_code, 400)
        self._approve()
        self.assertEqual(self._post(self.cro, "quotations:resend").status_code, 400)  # not sent yet
        with self.assertRaises(workflow.WorkflowError):
            workflow.accept(self.quotation, user=self.cro)
        self.assertEqual(self.gmail.sent, [])

    def test_draft_then_submit(self):
        draft = self._quotation(submit=False)
        self.assertEqual((draft.status, draft.latest_version.status), ("draft", "draft"))
        with self.assertRaises(workflow.WorkflowError):
            workflow.approve(draft, user=self.boss)
        self.client.force_login(self.costing)
        self.client.post(reverse("quotations:submit", args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "pending")
        self.assertEqual(draft.events.first().action, "submitted")

    def test_an_edit_after_approval_goes_back_to_pending_approval(self):
        self._approve()
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing, submit=False)  # even as a draft
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.status, "pending")
        self.assertEqual(self._post(self.cro, "quotations:send").status_code, 400)

    def test_rejection_needs_a_reason_and_goes_back_to_the_team(self):
        self.client.force_login(self.boss)
        url = reverse("quotations:decide", args=[self.quotation.pk])
        with mock.patch("push_notifications.services.send_push_to_users") as push:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {"decision": "revise"})
            self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "pending")
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(url, {"decision": "revise", "comment": "Price too high"})
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "revision")
        push.assert_called_once()
        self.assertIn(self.costing, push.call_args[0][0])


class UploadTests(SendBase):
    def _upload(self, upload):
        self.client.force_login(self.costing)
        return self.client.post(reverse("quotations:new"), {"customer_name": "Acme", "title": "Seal", "pdf": upload})

    def test_a_non_pdf_is_refused(self):
        response = self._upload(SimpleUploadedFile("quote.docx", b"PK word", content_type="application/pdf"))
        self.assertContains(response, "Please upload the quotation as a PDF file.")
        response = self._upload(SimpleUploadedFile("quote.pdf", b"not a pdf", content_type="application/pdf"))
        self.assertContains(response, "Please upload the quotation as a PDF file.")

    @override_settings(QUOTATION_MAX_PDF_MB=1)
    def test_a_pdf_over_the_limit_is_refused(self):
        big = SimpleUploadedFile("big.pdf", b"%PDF-1.4" + b"0" * (1024 * 1024 + 1), content_type="application/pdf")
        self.assertContains(self._upload(big), "larger than 1 MB")

    def test_approval_freezes_the_pdf_with_its_hash(self):
        self._approve()
        version = self.quotation.active_version
        self.assertTrue(version.frozen_pdf.name.endswith(f"{self.quotation.number}_v1.pdf"))
        self.assertEqual(version.sha256, workflow.sha256_of(b"%PDF-1.4 quote"))
        self.assertEqual(version.decided_by, self.boss)


class SendTests(SendBase):
    def test_a_successful_send(self):
        self._approve()
        self._link()
        response = self._post(self.cro, "quotations:send", cc="sagar@avigiri.com, boss@avigiri.com")
        self.assertEqual(response.status_code, 302)
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.status, "sent")
        self.assertEqual(self.quotation.sent_to, "buyer@customer.com")
        self.assertEqual(self.quotation.sent_cc, "sagar@avigiri.com, boss@avigiri.com")
        self.assertEqual(self.quotation.sent_by, self.cro)
        self.assertEqual(self.quotation.sent_message_id, self.gmail.quotation["Message-ID"])
        self.assertEqual(self.gmail.quotation["From"], "AVIGIRI Customer Service <customerservice@avigiri.com>")
        name, content = self.gmail.last_attachment()
        self.assertEqual((name, content), (f"{self.quotation.number}.pdf", b"%PDF-1.4 quote"))
        event = self.quotation.events.get(action="sent")
        self.assertEqual(event.details["message_id"], self.quotation.sent_message_id)
        self.assertEqual(event.details["version"], 1)
        self.assertEqual(event.enquiry_id, ENQ)
        # The automatic follow-ups start in this thread, as with the Enquiry Board's send.
        self.assertTrue(FollowUpStart.objects.filter(enquiry_id=ENQ, gmail_thread_id="T1").exists())

    def test_preview_defaults(self):
        self._approve()
        draft = sending.preview(self.quotation)
        self.assertEqual((draft.to, draft.cc, draft.linked), ([], [], False))
        self.assertEqual(draft.subject, "Re: Need door seals")  # the enquiry's subject, as a new email
        self.assertIn("Regards,\nAVIGIRI team", draft.body)
        self.assertIn("valid until", draft.body)
        self.assertIn(f"{self.quotation.number} (version 1)", draft.body)
        self.assertIn(sending.LINK_PLACEHOLDER, draft.body)
        self._link(subject="Re: RE: Need door seals")
        self._contact(other_customer_emails="second@customer.com", follow_up_cc="rabin@avigiri.com")
        draft = sending.preview(self.quotation)
        self.assertEqual((draft.to, draft.subject, draft.subject_locked), (["buyer@customer.com"],
                                                                          "Re: Need door seals", True))
        # Customer email 1 in To; email 2 and the enquiry's CCs in CC.
        self.assertEqual(draft.cc, ["second@customer.com", "rabin@avigiri.com"])

    def test_a_missing_email_blocks_the_send_and_the_typed_one_is_saved(self):
        self._approve()
        response = self._post(self.cro, "quotations:send", to="")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Add the customer&#x27;s email address", status_code=400)
        self.assertEqual(self.gmail.sent, [])
        response = self._post(self.cro, "quotations:send", to="not-an-email")
        self.assertEqual(response.status_code, 400)
        self._post(self.cro, "quotations:send", to="Buyer@Customer.com, second@customer.com")
        contact = EnquiryContact.objects.get(enquiry_id=ENQ)
        self.assertEqual(contact.customer_email, "buyer@customer.com")
        self.assertEqual(contact.other_customer_emails, "second@customer.com")

    def test_a_changed_pdf_is_never_sent(self):
        self._approve()
        version = self.quotation.active_version
        storage = version.frozen_pdf.storage
        storage.delete(version.frozen_pdf.name)
        storage.save(version.frozen_pdf.name, ContentFile(b"%PDF-1.4 tampered"))
        response = self._post(self.cro, "quotations:send")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "has changed since it was approved", status_code=400)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "approved")

    def test_a_failed_send_stays_approved_and_is_in_the_timeline(self):
        self._approve()
        self.gmail.fail = RuntimeError("quota exceeded")
        response = self._post(self.cro, "quotations:send")
        self.assertEqual(response.status_code, 502)
        self.assertContains(response, "quota exceeded", status_code=502)
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.status, "approved")
        event = self.quotation.events.first()
        self.assertEqual(event.action, "send_failed")
        self.assertIn("quota exceeded", event.details["error"])

    @override_settings(QUOTATION_MAIL_PROVIDER="SMTP")
    def test_a_provider_not_built_yet_refuses_clearly(self):
        self._approve()
        response = self._post(self.cro, "quotations:send")
        self.assertContains(response, "not set up on this server yet", status_code=502)


class ThreadTests(SendBase):
    def test_the_reply_goes_into_the_customers_thread(self):
        self._approve()
        self._link(subject="Need door seals", references="<c0@customer.com>")
        latest = {"id": "m2", "thread_id": "T1", "message_id_header": "<c2@customer.com>",
                  "references": "<c0@customer.com> <c1@customer.com>", "subject": "RE: Need door seals",
                  "sender": "Buyer <buyer@customer.com>", "date": "Mon, 23 Nov 2026", "body_text": "Any update?"}
        self.gmail.thread = [{"id": "m1", "message_id_header": "<c1@customer.com>"}, latest]
        self._post(self.cro, "quotations:send", subject="Typed subject is ignored")
        payload, sent = self.gmail.quotation_payload, self.gmail.quotation
        self.assertEqual(payload["threadId"], "T1")
        self.assertEqual(sent["Subject"], "Re: Need door seals")
        self.assertEqual(sent["In-Reply-To"], "<c2@customer.com>")
        self.assertEqual(sent["References"], "<c0@customer.com> <c1@customer.com> <c2@customer.com>")
        self.assertIn("> Any update?", sent.get_body(("plain",)).get_content())

    def test_no_double_re(self):
        self.assertEqual(mailer.reply_subject("Re: RE: Fwd: Need seals"), "Re: Need seals")
        self.assertEqual(mailer.reply_subject("Need seals"), "Re: Need seals")

    def test_an_unlinked_enquiry_starts_a_new_thread(self):
        self._approve()
        self._post(self.cro, "quotations:send", subject="Quotation for door seals")
        payload, sent = self.gmail.quotation_payload, self.gmail.quotation
        self.assertNotIn("threadId", payload)
        self.assertIsNone(sent["In-Reply-To"])
        self.assertIsNone(sent["References"])
        self.assertEqual(sent["Subject"], "Quotation for door seals")
        contact = EnquiryContact.objects.get(enquiry_id=ENQ)  # the new thread is now the enquiry's thread
        self.assertEqual((contact.gmail_thread_id, contact.email_subject), ("T-new", "Quotation for door seals"))
        # The password email replied to it, so it is the thread's latest message.
        password_payload, password = self.gmail.sent[-1]
        self.assertEqual(password_payload["threadId"], "T-new")
        self.assertEqual(password["In-Reply-To"], sent["Message-ID"])
        self.assertEqual(contact.email_message_id, password["Message-ID"])

    def test_later_emails_chain_onto_the_latest_message_id(self):
        self._approve()
        self._link(references="<c0@customer.com>")
        self._post(self.cro, "quotations:send")  # Gmail thread unreadable (empty): the stored reference is used
        first, first_password = self.gmail.quotation, self.gmail.last
        self.assertEqual(first["In-Reply-To"], "<c1@customer.com>")
        self.assertEqual(first_password["In-Reply-To"], first["Message-ID"])
        self._post(self.cro, "quotations:resend")
        second = self.gmail.quotation
        self.assertEqual(second["In-Reply-To"], first_password["Message-ID"])
        self.assertEqual(" ".join(str(second["References"]).split()),  # long headers may be folded
                         f"<c0@customer.com> <c1@customer.com> {first['Message-ID']} {first_password['Message-ID']}")
        self.assertEqual(second["Subject"], "Re: Need door seals")
        self.assertEqual([e.action for e in self.quotation.events.order_by("id")][-4:],
                         ["sent", "password_sent", "resent", "password_sent"])
        contact = EnquiryContact.objects.get(enquiry_id=ENQ)
        self.assertEqual(contact.email_message_id, self.gmail.last["Message-ID"])  # where the PI will reply

    def test_link_enquiry_email_from_the_board(self):
        from dashboard.models import UserServiceAccess

        UserServiceAccess.objects.filter(user=self.cro).update(
            allowed_services=["quotations", "quotation_send", "enquiries"])
        self.gmail.inbox = [{"id": "m9", "thread_id": "T9", "sender": "Buyer <buyer@customer.com>",
                             "subject": "Enquiry: door seals", "date": "Mon", "snippet": "Please quote"}]
        self.gmail.stored = {"m9": {"id": "m9", "thread_id": "T9", "message_id_header": "<c9@customer.com>",
                                      "references": "", "subject": "Enquiry: door seals",
                                      "sender": "Buyer <buyer@customer.com>"}}
        self.client.force_login(self.cro)
        url = reverse("enquiries:email_link", args=[ENQ])
        page = self.client.get(url)
        self.assertContains(page, "Enquiry: door seals")
        self.client.post(url, {"message": "m9"})
        contact = EnquiryContact.objects.get(enquiry_id=ENQ)
        self.assertEqual((contact.gmail_thread_id, contact.email_message_id, contact.email_subject),
                         ("T9", "<c9@customer.com>", "Enquiry: door seals"))
        self.assertEqual(contact.customer_email, "buyer@customer.com")
        self.assertTrue(QuotationEvent.objects.filter(enquiry_id=ENQ, action="email_linked",
                                                      quotation__isnull=True).exists())
        self.assertEqual(sending.preview(self.quotation).subject, "Re: Enquiry: door seals")


class ResponseTests(SendBase):
    def _sent(self):
        self._approve()
        self._post(self.cro, "quotations:send")
        self.quotation.refresh_from_db()

    def test_accepted_unlocks_the_proforma_invoice(self):
        self._sent()
        self.assertFalse(self.quotation.proforma_unlocked)
        received = []
        workflow.quotation_accepted.connect(lambda **kw: received.append(kw["quotation"].pk), weak=False,
                                            dispatch_uid="test-pi-hook")
        self.addCleanup(workflow.quotation_accepted.disconnect, dispatch_uid="test-pi-hook")
        with self.captureOnCommitCallbacks(execute=True):
            self._post(self.cro, "quotations:respond", answer="accepted", reference="PO-991", note="Go ahead")
        self.quotation.refresh_from_db()
        self.assertEqual((self.quotation.status, self.quotation.customer_reference), ("accepted", "PO-991"))
        self.assertTrue(self.quotation.proforma_unlocked)
        self.assertEqual(received, [self.quotation.pk])
        page = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]))
        self.assertContains(page, "data-proforma-hook")

    def test_declined_needs_a_reason(self):
        self._sent()
        self._post(self.cro, "quotations:respond", answer="declined", reason="")
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "sent")
        self._post(self.cro, "quotations:respond", answer="declined", reason="Price too high")
        self.quotation.refresh_from_db()
        self.assertEqual((self.quotation.status, self.quotation.customer_response_note), ("declined", "Price too high"))
        self.assertFalse(self.quotation.proforma_unlocked)
        self.assertEqual(self.quotation.events.first().action, "declined")

    def test_the_timeline_shows_on_the_enquiry(self):
        self._sent()
        from quotations import links

        actions = [e.action for e in links.events_for_enquiry(ENQ)]
        self.assertEqual(actions[:3], ["password_sent", "sent", "approved"])

"""Tests for customer verification: the email, test mode, the private link and the customer's answer."""
import base64
from datetime import timedelta
from email import message_from_bytes
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.gmail_service import GmailAPIError
from kyc import verification
from kyc.models import Customer, CustomerContact, KycVerification

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
            KYC_VERIFICATION_DRY_RUN=True, KYC_VERIFICATION_LINK_DAYS=30, FOLLOW_UP_DEFAULT_CC="")
LIVE = dict(BASE, KYC_VERIFICATION_DRY_RUN=False, FOLLOW_UP_DEFAULT_CC="cs@ours.example, meera@acme.example")


def _gmail():
    gmail = MagicMock()
    gmail.authenticate.return_value = True
    gmail.service.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "msg-1"}
    return gmail


def _sent_message(gmail):
    raw = gmail.service.users.return_value.messages.return_value.send.call_args.kwargs["body"]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw))


class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals", city="Bengaluru",
                                                gstin="29ABCDE1234F1Z5")
        self.meera = CustomerContact.objects.create(customer=self.customer, category="sales_marketing", name="Meera",
                                                    email="meera@acme.example", is_purchaser=True)
        self.ravi = CustomerContact.objects.create(customer=self.customer, category="accounts", name="Ravi",
                                                   email="ravi@acme.example")
        CustomerContact.objects.create(customer=self.customer, category="technical", name="Phone Only", phone="98450")
        self.send_url = reverse("kyc:verification_send", args=[self.customer.pk])

    def _send(self, recipients=("meera@acme.example",), gmail=None):
        with patch("kyc.verification.GmailService", return_value=gmail or _gmail()):
            return self.client.post(self.send_url, {"recipients": list(recipients)}, follow=True)

    def _answer(self, token, response, comment=""):
        self.client.logout()  # the customer is not signed in
        return self.client.post(reverse("kyc:verify", args=[token]), {"response": response, "comment": comment})


@override_settings(**BASE)
class SendPageTests(Base):
    def test_page_offers_contacts_with_email_and_preselects_the_purchaser(self):
        response = self.client.get(self.send_url)
        form = response.context["form"]
        self.assertEqual([c[0] for c in form.fields["recipients"].choices],
                         ["meera@acme.example", "ravi@acme.example"])
        self.assertEqual(form.fields["recipients"].initial, ["meera@acme.example"])
        self.assertContains(response, "Test mode is on")
        self.assertNotContains(response, "AV-0001</div>")  # the customer code is not part of the email
        self.assertNotIn("AV-0001", response.context["body"])

    def test_address_that_is_not_a_contact_is_refused(self):
        response = self._send(["stranger@evil.example"])
        self.assertEqual(KycVerification.objects.count(), 0)
        self.assertContains(response, "not one of the available choices")

    def test_test_mode_records_but_sends_nothing_and_the_link_is_only_a_preview(self):
        gmail = _gmail()
        self._send(gmail=gmail)
        gmail.service.users.return_value.messages.return_value.send.assert_not_called()
        row = KycVerification.objects.get()
        self.assertEqual((row.status, row.sent_to), ("dry_run", "meera@acme.example"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, Customer.KYC_DRAFT)

        page = self.client.get(reverse("kyc:verify", args=[row.token]))
        self.assertEqual(page.context["state"], "preview")
        self._answer(row.token, "confirmed")
        row.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual((row.response, self.customer.kyc_status), ("", Customer.KYC_DRAFT))


@override_settings(**LIVE)
class LiveTests(Base):
    def test_email_goes_to_chosen_contacts_with_cc_and_a_working_link(self):
        gmail = _gmail()
        response = self._send(["meera@acme.example", "ravi@acme.example"], gmail)
        self.assertContains(response, "Verification email sent")
        row = KycVerification.objects.get()
        message = _sent_message(gmail)
        self.assertEqual(message["To"], "meera@acme.example, ravi@acme.example")
        self.assertEqual(message["Cc"], "cs@ours.example")  # the CC that is already a recipient is dropped
        body = message.get_payload(decode=True).decode()
        self.assertIn(f"http://testserver/kyc/verify/{row.token}/", body)
        self.assertNotIn("AV-0001", body)
        self.assertEqual((row.status, row.sent_by, row.gmail_message_id), ("sent", self.user, "msg-1"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, Customer.KYC_SENT)

    def test_gmail_error_is_shown_and_leaves_no_open_link(self):
        gmail = _gmail()
        gmail.service.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
            GmailAPIError("quota"))
        response = self._send(gmail=gmail)
        self.assertContains(response, "The email was not sent")
        row = KycVerification.objects.get()
        self.assertEqual(row.status, "failed")
        self.assertEqual(verification.link_state(row), "expired")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, Customer.KYC_DRAFT)

    def test_customer_sees_details_without_signing_in_and_confirms(self):
        self._send()
        row = KycVerification.objects.get()
        self.client.logout()
        page = self.client.get(reverse("kyc:verify", args=[row.token]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Acme Seals")
        self.assertContains(page, "ravi@acme.example")
        self.assertNotContains(page, "AV-0001")
        self.assertEqual(page["Cache-Control"], "no-store")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")

        with patch("kyc.verification._notify") as notify:
            response = self._answer(row.token, "confirmed")
        self.assertRedirects(response, reverse("kyc:verify", args=[row.token]))
        notify.assert_called_once()
        row.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual((row.response, self.customer.kyc_status), ("confirmed", Customer.KYC_VERIFIED))
        self.assertEqual(row.response_snapshot["customer"]["gstin"], "29ABCDE1234F1Z5")
        self.assertEqual(len(row.response_snapshot["contacts"]), 3)
        self.assertContains(self.client.get(reverse("kyc:verify", args=[row.token])), "Thank you")

    def test_changes_need_a_comment_and_are_shown_to_the_team(self):
        self._send()
        row = KycVerification.objects.get()
        response = self._answer(row.token, "changes")
        self.assertContains(response, "Please tell us what needs to change")
        row.refresh_from_db()
        self.assertEqual(row.response, "")

        self._answer(row.token, "changes", "Our GSTIN ends in Z6.")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, Customer.KYC_CHANGES)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("kyc:detail", args=[self.customer.pk])), "Our GSTIN ends in Z6.")

    def test_reject_and_unknown_answer(self):
        self._send()
        row = KycVerification.objects.get()
        self._answer(row.token, "something-else")
        row.refresh_from_db()
        self.assertEqual(row.response, "")
        self._answer(row.token, "rejected")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, Customer.KYC_REJECTED)

    def test_link_can_be_answered_only_once(self):
        self._send()
        row = KycVerification.objects.get()
        self._answer(row.token, "confirmed")
        self._answer(row.token, "rejected", "changed my mind")
        row.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual((row.response, self.customer.kyc_status), ("confirmed", Customer.KYC_VERIFIED))

    def test_expired_and_unknown_links(self):
        self._send()
        row = KycVerification.objects.get()
        KycVerification.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self._answer(row.token, "confirmed")
        row.refresh_from_db()
        self.assertEqual(row.response, "")
        self.assertContains(self.client.get(reverse("kyc:verify", args=[row.token])), "no longer works")
        missing = self.client.get(reverse("kyc:verify", args=["no-such-token"]))
        self.assertEqual(missing.status_code, 404)
        self.assertNotContains(missing, "Acme", status_code=404)

    def test_sending_again_closes_the_earlier_link(self):
        self._send()
        first = KycVerification.objects.get()
        self._send()
        second = KycVerification.objects.exclude(pk=first.pk).get()
        first.refresh_from_db()
        self.assertEqual(verification.link_state(first), "expired")
        self.assertEqual(verification.link_state(second), "open")
        self.assertNotEqual(first.token, second.token)

    def test_send_page_needs_login_but_the_customer_link_does_not_need_kyc_access(self):
        self._send()
        row = KycVerification.objects.get()
        self.client.logout()
        self.assertEqual(self.client.get(self.send_url).status_code, 302)
        self.assertEqual(self.client.get(reverse("kyc:verify", args=[row.token])).status_code, 200)

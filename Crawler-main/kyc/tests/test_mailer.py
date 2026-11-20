"""The one email service (kyc/mailer.py) and the send endpoint: multiple To / CC / BCC, checks, logging,
the Marketing “Email internal” and “Email to customer” templates, and the Email history on the record."""
import base64
import json
from datetime import timedelta
from email import message_from_bytes
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.gmail_service import GmailAPIError
from kyc import mailer
from kyc.models import Customer, CustomerContact, Department, EmailLog, EnquiryCustomer, UserDepartment

User = get_user_model()
SETTINGS = dict(
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7, MARKETING_WAIT_DAYS=30,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True,
)


def gmail_stub(message_id="gm-123", fail=None):
    """A GmailService stand-in that is not local demo. Returns (gmail, send_call): send_call is the
    messages().send mock, whose call holds the raw message."""
    service = mock.MagicMock()
    send_call = service.users.return_value.messages.return_value.send
    if fail:
        send_call.return_value.execute.side_effect = fail
    else:
        send_call.return_value.execute.return_value = {"id": message_id}
    gmail = mock.MagicMock()
    gmail._local_demo_enabled.return_value = False
    gmail.service = service
    return gmail, send_call


def sent_raw(send_call):
    raw = base64.urlsafe_b64decode(send_call.call_args.kwargs["body"]["raw"].encode("ascii"))
    return message_from_bytes(raw)


class CheckTests(TestCase):
    def test_addresses_are_split_cleaned_and_listed_once(self):
        msg, problems = mailer.check(["Meera@Acme.example, ravi@acme.example;ravi@acme.example"],
                                     ["  tej@acme.example  "], ["meera@acme.example"], "Hello", "Body")
        self.assertEqual(problems, [])
        self.assertEqual(msg.to, ["meera@acme.example", "ravi@acme.example"])
        self.assertEqual(msg.cc, ["tej@acme.example"])
        self.assertEqual(msg.bcc, [])  # already in To, so not repeated in BCC

    def test_a_bad_address_is_named_in_the_problems(self):
        _, problems = mailer.check(["meera@acme.example"], ["not-an-address"], [], "Hello", "Body")
        self.assertEqual(problems, ["CC: “not-an-address” is not a valid email address."])

    def test_to_subject_and_body_are_required(self):
        _, problems = mailer.check([], [], [], "", "")
        self.assertIn("Add at least one To address.", problems)
        self.assertIn("Add a subject.", problems)
        self.assertIn("Write the email.", problems)

    def test_the_template_placeholders_must_be_replaced(self):
        _, problems = mailer.check(["a@x.example"], [], [], "Hi", "Action:\n[Write action needed here]")
        self.assertEqual(problems, [mailer.UNFILLED_MESSAGE])

    def test_too_many_addresses_are_refused(self):
        many = [f"user{i}@x.example" for i in range(mailer.MAX_RECIPIENTS + 1)]
        _, problems = mailer.check(many, [], [], "Hi", "Body")
        self.assertTrue(any("addresses in all" in p for p in problems))


@override_settings(**SETTINGS)
class SendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("sender", "sender@avigiri.example", "x", first_name="Asha")
        self.customer = Customer.objects.create(code="AV-0200", name="Send Co")

    def test_the_email_goes_out_with_bcc_hidden_and_is_logged(self):
        gmail, send = gmail_stub()
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            log = mailer.send_email(user=self.user, to=["a@x.example", "b@x.example"], cc=["c@x.example"],
                                    bcc=["d@x.example"], subject="Hi", body="Hello there",
                                    customer=self.customer, enquiry_id="ENQ-9")
        self.assertEqual((log.status, log.gmail_message_id), (EmailLog.STATUS_SENT, "gm-123"))
        self.assertEqual((log.to, log.cc, log.bcc), ("a@x.example, b@x.example", "c@x.example", "d@x.example"))
        self.assertEqual((log.customer, log.enquiry_id, log.sent_by), (self.customer, "ENQ-9", self.user))
        raw = sent_raw(send)
        self.assertEqual(raw["To"], "a@x.example, b@x.example")
        self.assertEqual(raw["Cc"], "c@x.example")
        self.assertEqual(raw["Bcc"], "d@x.example")
        self.assertEqual(raw["Subject"], "Hi")

    def test_a_gmail_refusal_is_logged_as_not_sent_and_reported(self):
        gmail, _ = gmail_stub(fail=GmailAPIError("quota"))
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            with self.assertRaises(mailer.MailError) as caught:
                mailer.send_email(user=self.user, to=["a@x.example"], subject="Hi", body="Hello", customer=self.customer)
        self.assertTrue(caught.exception.gmail)
        log = EmailLog.objects.get()
        self.assertEqual(log.status, EmailLog.STATUS_FAILED)
        self.assertIn("quota", log.error)

    def test_an_invalid_email_is_not_sent_and_not_logged(self):
        with mock.patch("kyc.mailer.GmailService") as service:
            with self.assertRaises(mailer.MailError) as caught:
                mailer.send_email(user=self.user, to=["bad"], subject="Hi", body="Hello")
        service.assert_not_called()
        self.assertFalse(EmailLog.objects.exists())
        self.assertTrue(caught.exception.problems)

    def test_local_demo_mode_sends_nothing_but_still_logs(self):
        gmail, send = gmail_stub()
        gmail._local_demo_enabled.return_value = True
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            log = mailer.send_email(user=self.user, to=["a@x.example"], subject="Hi", body="Hello")
        send.assert_not_called()
        self.assertTrue(log.gmail_message_id.startswith("demo-"))


@override_settings(**SETTINGS)
class SendEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("boss", "boss@avigiri.example", "x")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(code="AV-0201", name="Endpoint Co")
        self.url = reverse("kyc:email_send")

    def post(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_it_needs_a_post_and_a_sign_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.post({"to": ["a@x.example"], "subject": "s", "body": "b"}).status_code, 403)
        self.assertFalse(EmailLog.objects.exists())

    def test_a_valid_email_is_sent_and_logged_against_the_customer(self):
        gmail, _ = gmail_stub()
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            response = self.post({"to": ["a@x.example"], "cc": ["b@x.example", "c@x.example"], "bcc": [],
                                  "subject": "Hi", "body": "Hello", "customer_id": self.customer.pk,
                                  "enquiry_id": "ENQ-1"})
        data = response.json()
        self.assertTrue(data["ok"], data)
        log = EmailLog.objects.get()
        self.assertEqual((log.customer, log.cc), (self.customer, "b@x.example, c@x.example"))

    def test_problems_come_back_with_status_400_and_nothing_is_logged(self):
        response = self.post({"to": ["nope"], "subject": "", "body": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertTrue(response.json()["problems"])
        self.assertFalse(EmailLog.objects.exists())

    def test_an_unknown_customer_is_refused(self):
        response = self.post({"to": ["a@x.example"], "subject": "s", "body": "b", "customer_id": 999999})
        self.assertEqual(response.status_code, 400)

    def test_a_gmail_failure_is_a_502(self):
        gmail, _ = gmail_stub(fail=GmailAPIError("down"))
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            response = self.post({"to": ["a@x.example"], "subject": "s", "body": "b"})
        self.assertEqual(response.status_code, 502)


@override_settings(**SETTINGS)
class TemplateTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.user = User.objects.create_user("asha", "asha@avigiri.example", "x", first_name="Asha", last_name="Rao")
        self.acme = Customer.objects.create(code="AV-0300", name="Acme Seals", customer_code="AVG-26-1001")
        CustomerContact.objects.create(customer=self.acme, category="technical", name="Tej", designation="Director",
                                       email="tej@acme.example", phone="9845012345", is_purchaser=True)
        CustomerContact.objects.create(customer=self.acme, category="accounts", name="Ravi",
                                       email="ravi@acme.example", phone="9845012346")
        self.enquiry = {"enquiry_id": "ENQ-7", "status": "rejected", "customer_name": "Acme Seals",
                        "remarks": "Need 40 gaskets by May", "customer_email": ""}

    def test_internal_email_is_filled_from_the_customer_and_enquiry(self):
        UserDepartment.objects.create(user=self.user, department=Department.objects.get(name="Sales"))
        t = mailer.internal_template(user=self.user, enquiry=self.enquiry, customer=self.acme)
        self.assertEqual(t["subject"], "Internal Update – Acme Seals (AVG-26-1001)")
        for line in ["Customer Name : Acme Seals", "Customer Code : AVG-26-1001", "Contact Person: Tej – Director",
                     "Enquiry / Ref : ENQ-7", "Status        : Rejected", "Need 40 gaskets by May",
                     "Asha Rao", "Sales – AVIGIRI", "[Write action needed here]", "Hi Team,"]:
            self.assertIn(line, t["body"])
        self.assertEqual(t["to"], [])  # internal To is chosen by the person

    def test_internal_email_says_avigiri_when_the_user_has_no_department(self):
        t = mailer.internal_template(user=self.user, enquiry=self.enquiry, customer=self.acme)
        self.assertTrue(t["body"].rstrip().endswith("AVIGIRI"))

    def test_customer_email_goes_to_the_primary_contact_with_the_others_in_cc(self):
        t = mailer.customer_template(enquiry=self.enquiry, customer=self.acme)
        self.assertEqual(t["subject"], "Regarding Your Enquiry – ENQ-7 (AVG-26-1001)")
        self.assertEqual(t["to"], ["tej@acme.example"])
        self.assertEqual(t["cc"], ["ravi@acme.example"])
        self.assertIn('With reference to your comment: "Need 40 gaskets by May"', t["body"])
        self.assertTrue(t["body"].startswith("Dear Sir/Madam,"))
        self.assertTrue(t["body"].endswith("AVIGIRI Team"))

    def test_without_a_kyc_customer_the_enquiry_email_is_used(self):
        self.enquiry["customer_email"] = "buyer@elsewhere.example"
        t = mailer.customer_template(enquiry=self.enquiry, customer=None)
        self.assertEqual(t["to"], ["buyer@elsewhere.example"])
        self.assertIn("Not in KYC yet", t["subject"])

    def test_suggestions_offer_internal_users_departments_and_customer_contacts(self):
        items = mailer.suggestions(self.acme)
        emails = {i["email"] for i in items}
        self.assertIn("asha@avigiri.example", emails)
        self.assertIn("kumar@avigiri.com", emails)  # a Marketing department address from the seed
        self.assertIn("tej@acme.example", emails)
        groups = {i["email"]: i["group"] for i in items}
        self.assertEqual(groups["tej@acme.example"], "Customer")
        self.assertEqual(groups["asha@avigiri.example"], "Internal")


@override_settings(**SETTINGS)
class MarketingAndHistoryPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("mkt", "mkt@avigiri.example", "x")
        self.client.force_login(self.user)
        self.acme = Customer.objects.create(code="AV-0400", name="Page Co", customer_code="AVG-26-1002")
        CustomerContact.objects.create(customer=self.acme, category="sales_marketing", name="Meera",
                                       designation="Owner / Proprietor", email="meera@page.example",
                                       phone="9845012347", is_purchaser=True)
        EnquiryCustomer.objects.create(enquiry_id="ENQ-8", customer=self.acme)
        self.enquiry = {"enquiry_id": "ENQ-8", "status": "rejected", "customer_name": "Page Co", "subject": "Gaskets",
                        "remarks": "Price too high", "received_at_dt": timezone.now() - timedelta(days=90),
                        "customer_reference": "", "customer_email": ""}

    def test_the_marketing_record_has_both_email_buttons_and_the_filled_templates(self):
        with mock.patch("marketing.views._api_root", return_value="http://svc"), \
                mock.patch("marketing.views.services.fetch_enquiry", return_value=dict(self.enquiry)):
            page = self.client.get(reverse("marketing:detail", args=["ENQ-8"]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-email-template="internal"')
        self.assertContains(page, 'data-email-template="customer"')
        self.assertContains(page, "emailComposeData")
        self.assertContains(page, "Page Co (AVG-26-1002)")  # the template is JSON-escaped, so match the plain part
        self.assertContains(page, "Internal Update")
        self.assertContains(page, "meera@page.example")

    def test_sending_from_marketing_shows_in_the_customer_email_history(self):
        gmail, _ = gmail_stub()
        with mock.patch("kyc.mailer.GmailService", return_value=gmail):
            self.client.post(reverse("kyc:email_send"), json.dumps({
                "to": ["meera@page.example"], "subject": "Regarding Your Enquiry – ENQ-8 (AVG-26-1002)",
                "body": "Dear Sir/Madam,\n\nThanks.", "customer_id": self.acme.pk, "enquiry_id": "ENQ-8",
            }), content_type="application/json")
        self.assertEqual(mailer.history_for_customer(self.acme)[0].subject,
                         "Regarding Your Enquiry – ENQ-8 (AVG-26-1002)")
        page = self.client.get(reverse("kyc:detail", args=[self.acme.pk]))
        self.assertContains(page, "Regarding Your Enquiry – ENQ-8 (AVG-26-1002)")
        self.assertContains(page, "Email history")
        with mock.patch("marketing.views._api_root", return_value="http://svc"), \
                mock.patch("marketing.views.services.fetch_enquiry", return_value=dict(self.enquiry)):
            marketing_page = self.client.get(reverse("marketing:detail", args=["ENQ-8"]))
        self.assertContains(marketing_page, "Emails sent from here")
        self.assertContains(marketing_page, "Regarding Your Enquiry – ENQ-8 (AVG-26-1002)")

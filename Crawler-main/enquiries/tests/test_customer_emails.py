"""Several customer emails per enquiry: saving them, and copying them (CC) on every email to the customer."""
from datetime import datetime
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries import customer_emails, payment_terms, status_emails
from enquiries.models import EnquiryContact
from enquiries.tests.test_order_tracking import AUTO_ON, ENQUIRY, _conversation, _gmail

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


class ParseAndMergeTests(TestCase):
    def test_parse_cleans_and_checks(self):
        self.assertEqual(customer_emails.parse(["Two@Acme.example", " three@acme.example, two@acme.example", ""],
                                               main="one@acme.example"),
                         (["two@acme.example", "three@acme.example"], ""))
        self.assertEqual(customer_emails.parse(["one@acme.example"], main="ONE@acme.example"), ([], ""))
        addresses, error = customer_emails.parse(["not-an-email"])
        self.assertEqual(addresses, [])
        self.assertIn("not a valid email", error)
        _, error = customer_emails.parse([f"p{n}@acme.example" for n in range(6)])
        self.assertIn("at most 5", error)

    def test_with_customer_cc_puts_customer_first_and_skips_to_and_own(self):
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="one@acme.example",
                                      other_customer_emails="two@acme.example, three@acme.example")
        merged = customer_emails.with_customer_cc(
            "E1", ["manager@ours.example", "two@acme.example", "one@acme.example"],
            to="one@acme.example", own={"manager@ours.example"})
        self.assertEqual(merged, ["two@acme.example", "three@acme.example"])
        self.assertEqual(customer_emails.with_customer_cc("E1", ["x@ours.example"], limit=2),
                         ["two@acme.example", "three@acme.example"])
        self.assertEqual(customer_emails.with_customer_cc("NONE", ["x@ours.example"]), ["x@ours.example"])

    @override_settings(FOLLOW_UP_DEFAULT_CC="manager@ours.example")
    def test_follow_up_screen_prefills_cc_with_other_customer_emails(self):
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="one@acme.example",
                                      other_customer_emails="two@acme.example")
        self.assertEqual(payment_terms.default_cc("E1"), "two@acme.example, manager@ours.example")
        EnquiryContact.objects.filter(enquiry_id="E1").update(follow_up_cc="boss@ours.example")
        self.assertEqual(payment_terms.default_cc("E1"), "two@acme.example, boss@ours.example")
        self.assertEqual(payment_terms.default_cc("E2"), "manager@ours.example")


@override_settings(**STATIC)
class BoardCustomerEmailsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("boss", "b@example.com", "x")
        self.client.force_login(self.user)
        self.rows = [{"enquiry_id": "N1", "status": "open", "subject": "Valve RFQ", "customer_name": "Beta",
                      "received_at_dt": datetime(2026, 5, 2)}]
        patch("enquiries.views.services.list_enquiries",
              side_effect=lambda *a, **k: [dict(r) for r in self.rows]).start()
        patch("enquiries.views.services.update_enquiry", return_value={}).start()
        self.addCleanup(patch.stopall)

    def _save(self, **data):
        return self.client.post(reverse("enquiries:board"),
                                {"action": "update_customer", "enquiry_id": "N1", "customer_name": "Beta", **data},
                                follow=True)

    def test_edit_customer_saves_two_or_more_emails(self):
        response = self._save(customer_email="one@beta.example", other_customer_emails_sent="1",
                              other_customer_email=["two@beta.example", "three@beta.example", ""])
        contact = EnquiryContact.objects.get(enquiry_id="N1")
        self.assertEqual(contact.customer_email, "one@beta.example")
        self.assertEqual(contact.other_customer_emails, "two@beta.example, three@beta.example")
        self.assertContains(response, "one@beta.example, two@beta.example, three@beta.example")
        page = self.client.get(reverse("enquiries:board"))
        self.assertContains(page, "CC: two@beta.example")
        self.assertContains(page, 'value="three@beta.example"')
        self.assertContains(page, "+ Add another email")

    def test_second_email_alone_becomes_the_main_one(self):
        self._save(customer_email="", other_customer_emails_sent="1", other_customer_email=["two@beta.example"])
        contact = EnquiryContact.objects.get(enquiry_id="N1")
        self.assertEqual((contact.customer_email, contact.other_customer_emails), ("two@beta.example", ""))

    def test_clearing_the_boxes_removes_other_emails(self):
        EnquiryContact.objects.create(enquiry_id="N1", customer_email="one@beta.example",
                                      other_customer_emails="two@beta.example")
        self._save(customer_email="one@beta.example", other_customer_emails_sent="1", other_customer_email=[""])
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="N1").other_customer_emails, "")

    def test_forms_without_the_boxes_keep_other_emails(self):
        EnquiryContact.objects.create(enquiry_id="N1", customer_email="one@beta.example",
                                      other_customer_emails="two@beta.example")
        self._save(customer_email="one@beta.example")  # e.g. the "Link to customer code" button
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="N1").other_customer_emails, "two@beta.example")

    def test_bad_second_email_is_refused(self):
        response = self._save(customer_email="one@beta.example", other_customer_emails_sent="1",
                              other_customer_email=["nope"])
        self.assertContains(response, "is not a valid email address")
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="N1").exclude(other_customer_emails="").exists())

    def test_create_forms_have_a_second_email_box(self):
        page = self.client.get(reverse("enquiries:board"))
        self.assertContains(page, 'placeholder="Customer email 2 (optional, CC)"', count=2)


@override_settings(**AUTO_ON)
class StatusEmailCcTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        self.gmail = _gmail()
        patch("enquiries.status_emails.GmailService", MagicMock(return_value=self.gmail)).start()
        patch("enquiries.views._resolve_follow_up_conversation", return_value=_conversation()).start()
        self.addCleanup(patch.stopall)

    def test_automatic_customer_email_copies_other_customer_emails(self):
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example",
                                      other_customer_emails="boss@acme.example, sales@ourcompany.example")
        self.assertEqual(status_emails.send_status_email("rejection", "E1", ENQUIRY)["result"], "sent")
        kwargs = self.gmail.send_reply.call_args.kwargs
        self.assertEqual(kwargs["to"], "buyer@acme.example")
        self.assertEqual(kwargs["cc"], ["boss@acme.example"])  # never our own address

    def test_no_cc_when_there_is_only_one_email(self):
        status_emails.send_status_email("rejection", "E1", ENQUIRY)
        self.assertIsNone(self.gmail.send_reply.call_args.kwargs["cc"])



@override_settings(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False,
                   FOLLOW_UP_AUTO_SENDER="sales", FOLLOW_UP_AUTO_GAP_DAYS=[4, 7, 7], FOLLOW_UP_MAX_COUNT=4)
class AutoFollowUpCcTests(TestCase):
    def setUp(self):
        from enquiries import auto_follow_up

        self.auto = auto_follow_up
        self.sales = get_user_model().objects.create_user("sales", "sales@ourcompany.example", "x")
        enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gasket quote", "customer_name": "Acme"}
        patch.object(auto_follow_up.services, "list_enquiries", return_value=[enquiry]).start()
        patch.object(auto_follow_up.services, "fetch_enquiry", return_value=enquiry).start()
        self.addCleanup(patch.stopall)

    def test_automatic_follow_up_copies_other_customer_emails(self):
        from django.utils import timezone
        from datetime import timedelta
        from enquiries.models import FollowUpSent
        from enquiries.tests.test_auto_follow_up import _gmail as auto_gmail

        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example",
                                      other_customer_emails="boss@acme.example")
        FollowUpSent.objects.create(
            enquiry_id="E1", customer_email="buyer@acme.example", sent_by=self.sales, cc="manager@ours.example",
            status=FollowUpSent.STATUS_SENT, gmail_thread_id="t1", stage="waiting_po",
            next_follow_up_on=timezone.localdate(), sent_at=timezone.now() - timedelta(days=4))
        gmail = auto_gmail()
        with patch.object(self.auto, "GmailService", return_value=gmail):
            outcome = self.auto.run_auto_follow_up("E1")
        self.assertEqual(outcome["result"], "sent")
        self.assertEqual(gmail.send_reply.call_args.kwargs["cc"], ["boss@acme.example", "manager@ours.example"])

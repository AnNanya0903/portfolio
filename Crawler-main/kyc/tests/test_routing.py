"""Tests for routing follow-up emails to the customer's teams and copying top management after reminders."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from enquiries import auto_follow_up
from enquiries.models import FollowUpSent
from kyc import links, routing
from kyc.models import Customer, CustomerContact

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
AUTO_ON = dict(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
               FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_DEFAULT_CC="", **STATIC)
OURS = "sales@ourcompany.example"


def _customer():
    acme = Customer.objects.create(code="AV-0001", name="Acme Seals")
    for category, name, email, purchaser in (
            ("sales_marketing", "Meera", "meera@acme.example", False),
            ("technical", "Dev", "dev@acme.example", True),
            ("accounts", "Ravi", "ravi@acme.example", False),
            ("accounts", "Asha", "Asha@Acme.example", False),
            ("accounts", "No Email", "", False),
            ("top_management", "MD", "md@acme.example", False)):
        CustomerContact.objects.create(customer=acme, category=category, name=name, email=email,
                                       phone="" if email else "99999", is_purchaser=purchaser)
    return acme


class RoutingTests(TestCase):
    def setUp(self):
        self.acme = _customer()
        links.set_link("E1", self.acme)

    def test_payment_goes_to_accounts_and_the_rest_to_purchaser_and_sales(self):
        self.assertEqual(routing.team_emails(self.acme, "accounts"), ["asha@acme.example", "ravi@acme.example"])
        self.assertEqual(routing.route(self.acme, "payment_pending")["team"], "accounts")
        for stage in ("general", "waiting_po", "", "something_new"):
            plan = routing.route(self.acme, stage)
            self.assertEqual(plan["team_emails"], ["dev@acme.example", "meera@acme.example"], stage)
            self.assertEqual(plan["escalation_emails"], [])

    def test_top_management_is_copied_after_two_follow_ups(self):
        self.assertEqual([routing.is_escalated(n) for n in (1, 2, 3, 4)], [False, False, True, True])
        self.assertEqual(routing.route(self.acme, "general", 3)["escalation_emails"], ["md@acme.example"])
        with override_settings(KYC_ESCALATE_AFTER_REMINDERS=0):
            self.assertFalse(routing.is_escalated(9))
            self.assertEqual(routing.route(self.acme, "general", 9)["escalation_emails"], [])

    def test_add_cc_skips_the_recipient_our_addresses_and_duplicates(self):
        cc = routing.add_cc(["boss@avigiri.com", "RAVI@acme.example", OURS], "E1", "payment_pending", 3,
                            to="asha@acme.example", own=[OURS])
        self.assertEqual(cc, ["boss@avigiri.com", "ravi@acme.example", "md@acme.example"])

    def test_add_cc_keeps_the_routed_contacts_when_there_are_too_many(self):
        base = [f"u{n}@x.example" for n in range(10)]
        cc = routing.add_cc(base, "E1", "payment_pending", 3, limit=10)
        self.assertEqual(len(cc), 10)
        self.assertEqual(cc[-3:], ["asha@acme.example", "ravi@acme.example", "md@acme.example"])

    def test_an_unlinked_enquiry_is_left_alone(self):
        self.assertEqual(routing.add_cc(["boss@avigiri.com"], "E9", "payment_pending", 4), ["boss@avigiri.com"])
        self.assertEqual(routing.describe("E9", "payment_pending", 4), "")

    def test_the_note_says_who_is_copied(self):
        self.assertEqual(routing.describe("E1", "payment_pending", 1),
                         "Accounts contacts of AV-0001 are copied. Top management is copied from follow-up #3.")
        self.assertIn("Top management is copied too (follow-up #3)", routing.describe("E1", "general", 3))
        self.acme.contacts.filter(category="accounts").delete()
        self.assertIn("has no Accounts contact with an email", routing.describe("E1", "payment_pending", 1))

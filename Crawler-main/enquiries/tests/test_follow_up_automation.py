"""The fully automatic follow-ups: thread check, CC lists, reply rules, enquiry -> Marketing, safety rules."""
import itertools
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from dashboard.follow_up_templates import (
    MARKETING_STAGE, QUOTATION_STAGE, build_follow_up_body, enquiry_closing_body, marketing_subject,
)
from enquiries import auto_follow_up, scheduler, status_emails, thread_check
from enquiries.models import (
    CustomerStatusEmail, EnquiryContact, EnquiryFollowup, FollowUpNote, FollowUpSent, FollowUpStart,
)
from marketing import lost
from marketing.models import MarketingActivity, MarketingLead

OURS = "sales@avigiri.com"
CUSTOMER = "buyer@acme.example"
ENQUIRY_CC = ["customerservice@avigiri.com", "rabin@avigiri.com", "apai@avigiri.com"]
MARKETING_CC = ["kumar@avigiri.com", "manju@avigiri.com", "apai@avigiri.com"]
SUBJECT = "Quotation Q-77 - Door seals"
SETTINGS = dict(
    FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_CLOSE_AFTER_DAYS=2,
    FOLLOW_UP_MIN_GAP_HOURS=0, FOLLOW_UP_SEND_RETRIES=3, FOLLOW_UP_SEND_CAP_PER_RUN=20,
    ENQUIRY_FOLLOW_UP_CC=",".join(ENQUIRY_CC), MARKETING_FOLLOW_UP_CC=",".join(MARKETING_CC),
    MARKETING_AUTO_FOLLOW_UP=True, MARKETING_FOLLOW_UP_COUNT=4, MARKETING_NO_RESPONSE_FIRST_DELAY_DAYS=2,
    FOLLOW_UP_CONTACT_PHONE="", MARKETING_REVISED_QUOTE_DAYS="",
)
_ids = itertools.count(1)


def msg(thread, sender, *, to="", cc="", subject=SUBJECT, body="", when=None, headers=(), attachments=()):
    n = next(_ids)
    return {"id": f"m{n}", "thread_id": thread, "sender": sender, "to": to, "cc": cc, "subject": subject,
            "body_text": body, "snippet": body[:100], "attachments": list(attachments),
            "headers": [{"name": k, "value": v} for k, v in headers],
            "parsed_date": when or datetime(2026, 11, 1, 10, n % 60, tzinfo=dt_timezone.utc)}


class FakeGmail:
    """A mailbox: threads of messages; sending adds to them like Gmail does."""

    def __init__(self):
        self.threads = {}
        self.sent = []

    def add(self, message):
        self.threads.setdefault(message["thread_id"], []).append(message)
        return message

    def own_addresses(self):
        return {OURS}

    def get_thread_messages(self, thread_id, limit=20):
        return list(self.threads.get(thread_id, []))[-limit:]

    def get_message(self, message_id):
        for messages in self.threads.values():
            for message in messages:
                if message["id"] == message_id:
                    return dict(message)
        return None

    def search_messages(self, query, limit=20):
        return [m for messages in self.threads.values() for m in messages if m["sender"] == OURS][:limit]

    def send_email(self, to, subject, body, cc=None, html_body=None, **kwargs):
        thread = f"t-new{next(_ids)}"
        sent = self.add(msg(thread, OURS, to=to, cc=", ".join(cc or []), subject=subject, body=body))
        self.sent.append({"kind": "new", "to": to, "cc": list(cc or []), "subject": subject, "body": body,
                          "html": html_body, "thread": thread})
        return {"id": sent["id"], "threadId": thread}

    def send_reply(self, original_message, body, subject=None, to=None, cc=None, html_body=None, **kwargs):
        thread = original_message["thread_id"]
        line = subject if (subject or "").lower().startswith("re:") else f"Re: {subject}"
        sent = self.add(msg(thread, OURS, to=to, cc=", ".join(cc or []), subject=line, body=body))
        self.sent.append({"kind": "reply", "to": to, "cc": list(cc or []), "subject": line, "body": body,
                          "html": html_body, "thread": thread})
        return {"id": sent["id"], "threadId": thread}


@override_settings(**SETTINGS)
class Base(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", OURS, "x")
        self.gmail = FakeGmail()
        self.enquiries = {"ENQ-1": {"enquiry_id": "ENQ-1", "status": "open", "subject": "Door seals",
                                    "customer_name": "Acme Seals", "assigned_to": ""}}
        self.updates = []
        for target, kwargs in (
                ("enquiries.auto_follow_up.GmailService", {"return_value": self.gmail}),
                ("enquiries.auto_follow_up.services.fetch_enquiry",
                 {"side_effect": lambda root, eid, **k: dict(self.enquiries[eid])}),
                ("enquiries.auto_follow_up.services.list_enquiries",
                 {"side_effect": lambda root, **k: [dict(e) for e in self.enquiries.values()]}),
                ("enquiries.auto_follow_up.services.update_enquiry", {"side_effect": self._update}),
                ("enquiries.auto_follow_up.follow_up_links.for_follow_up",
                 {"return_value": ("https://portal.example/share/magicwand/ENQ-1/abc/", "AG-Pass1234")}),
        ):
            patch(target, **kwargs).start()
        self.addCleanup(patch.stopall)
        self.day = timezone.localdate()

    def _update(self, root, enquiry_id, payload, **kwargs):
        self.updates.append((enquiry_id, payload))
        self.enquiries[enquiry_id].update(payload)

    def quote(self, enquiry_id="ENQ-1", thread="t1", subject=SUBJECT, customer=CUSTOMER):
        """The quotation emailed from the portal: the sequence starts in its thread."""
        sent = self.gmail.add(msg(thread, OURS, to=customer, subject=subject, body=f"Our quotation for {enquiry_id}"))
        return auto_follow_up.record_start(enquiry_id, QUOTATION_STAGE, sent={"threadId": thread, "id": sent["id"]},
                                           customer_email=customer, cc=[], subject=subject, user=self.user)

    def go(self, days, enquiry_id="ENQ-1"):
        return auto_follow_up.run_auto_follow_up(enquiry_id, self.day + timedelta(days=days))


class ThreadCheckTests(TestCase):
    def setUp(self):
        self.quote = msg("t1", OURS, to=CUSTOMER, subject=SUBJECT, body="Quotation for ENQ-1")
        self.thread = [self.quote, msg("t1", CUSTOMER, to=OURS, subject=f"RE: {SUBJECT}")]

    def check(self, messages, **kw):
        args = dict(customer_email=CUSTOMER, subject=SUBJECT, own={OURS}, numbers=["ENQ-1"])
        args.update(kw)
        return thread_check.check(messages, **args)[0]

    def test_passes_only_with_participant_subject_and_quote(self):
        self.assertTrue(self.check(self.thread))
        self.assertTrue(self.check(self.thread, subject=f"Fwd:  re: {SUBJECT} "))  # prefixes and spaces ignored
        self.assertFalse(self.check(self.thread, subject="Quotation Q-78 - Door seals"))
        self.assertFalse(self.check(self.thread, customer_email="other@acme.example"))
        self.assertFalse(self.check([msg("t2", OURS, to=CUSTOMER, subject=SUBJECT, body="no number")]))
        self.assertTrue(self.check([msg("t2", OURS, to=CUSTOMER, subject=SUBJECT, attachments=[
            {"filename": "Quotation-ENQ-1.pdf"}])]))  # the enquiry number in an attachment name counts

    def test_what_counts_as_a_customer_reply(self):
        thread = [self.quote,
                  msg("t1", "rabin@avigiri.com", to=CUSTOMER, cc=OURS),  # staff reply-all
                  msg("t1", CUSTOMER, subject="Automatic reply: away", headers=[("Auto-Submitted", "auto-replied")]),
                  msg("t1", "boss@acme.example", body="other person at the customer")]
        self.assertEqual(thread_check.replies(thread, customer_email=CUSTOMER, own={OURS})["replied"], [])
        self.assertEqual(len(thread_check.replies(thread, customer_email=CUSTOMER, others=["boss@acme.example"],
                                                  own={OURS})["replied"]), 1)
        bounce = msg("t1", "mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)")
        self.assertTrue(thread_check.replies([self.quote, bounce], customer_email=CUSTOMER, own={OURS})["bounced"])


class EnquirySequenceTests(Base):
    def test_quote_from_the_portal_starts_follow_ups_in_its_thread_with_the_enquiry_cc(self):
        self.quote()
        self.assertEqual(self.go(1)["reason"], f"not due until {(self.day + timedelta(days=2)).isoformat()}")
        result = self.go(2)
        self.assertEqual(result["result"], "sent", result)
        sent = self.gmail.sent[-1]
        self.assertEqual((sent["kind"], sent["thread"], sent["to"]), ("reply", "t1", CUSTOMER))
        self.assertEqual(sent["cc"], ENQUIRY_CC)
        self.assertEqual(sent["subject"], f"Re: {SUBJECT}")
        self.assertIn("https://portal.example/share/magicwand/ENQ-1/abc/", sent["body"])
        self.assertIn("Password: AG-Pass1234", sent["body"])
        self.assertIn('<a href="https://portal.example/share/magicwand/ENQ-1/abc/">', sent["html"])
        self.assertEqual(FollowUpSent.objects.get().attempt, 1)

    def test_four_follow_ups_then_the_closing_email_then_marketing(self):
        self.quote()
        for n, days in enumerate((2, 4, 6, 8), start=1):
            self.assertEqual(self.go(days)["result"], "sent")
            self.assertEqual(self.go(days)["result"], "skipped")  # one per due date
        self.assertEqual(self.go(9)["reason"], f"not due until {(self.day + timedelta(days=10)).isoformat()}")
        closing = self.go(10)
        self.assertEqual(closing["result"], "sent", closing)
        body = self.gmail.sent[-1]["body"]
        self.assertIn("we'll close this enquiry for now", body)
        self.assertNotIn("http", body)  # no link in the closing email
        self.assertEqual(len(self.gmail.sent), 5)  # no acknowledgement: 4 follow-ups + closing
        self.assertIn(("ENQ-1", {"status": "closed"}), self.updates)
        contact = EnquiryContact.objects.get(enquiry_id="ENQ-1")
        self.assertEqual(contact.close_reason, "no_response")
        self.assertEqual(MarketingLead.objects.filter(enquiry_id="ENQ-1").count(), 1)
        lead = MarketingLead.objects.get(enquiry_id="ENQ-1")
        self.assertEqual(lead.lead_source, "no_response")
        start = FollowUpStart.objects.get(enquiry_id="ENQ-1", stage=MARKETING_STAGE)
        self.assertEqual(timezone.localdate(start.started_at), self.day + timedelta(days=2))
        self.assertTrue(FollowUpNote.objects.filter(enquiry_id="ENQ-1", kind="moved_to_marketing").exists())
        # The enquiry is Closed: the next email is Marketing Follow-up 1, in a new thread with the Marketing CC.
        self.assertEqual(self.go(12)["stage"], MARKETING_STAGE)
        self.assertEqual((self.gmail.sent[-1]["kind"], self.gmail.sent[-1]["cc"]), ("new", MARKETING_CC))
        self.assertEqual(self.gmail.sent[-1]["subject"], "Is Door seals still on your plans, Acme Seals?")

    def test_wrong_subject_sends_nothing_and_flags_then_resumes_without_a_burst(self):
        self.quote(subject="Quotation Q-77 - Door seals")
        self.gmail.threads["t1"][0]["subject"] = "Something else"  # the thread no longer matches
        result = self.go(2)
        self.assertEqual(result["reason"], auto_follow_up.THREAD_NOT_FOUND)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-1").follow_up_flag, "thread_not_found")
        self.assertTrue(FollowUpNote.objects.filter(enquiry_id="ENQ-1", kind="thread_not_found").exists())
        self.assertEqual(self.go(5)["reason"], auto_follow_up.THREAD_NOT_FOUND)  # re-checked each run
        self.gmail.threads["t1"][0]["subject"] = SUBJECT  # found again
        self.assertEqual(self.go(6)["result"], "sent")
        self.assertEqual(self.go(6)["result"], "skipped")  # one email, not the missed ones in a burst
        self.assertEqual(FollowUpSent.objects.get().attempt, 1)
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-1").follow_up_flag, "")

    def test_right_subject_but_a_different_customer_sends_nothing(self):
        self.quote(customer="someone@else.example")
        EnquiryContact.objects.update_or_create(enquiry_id="ENQ-1", defaults={"customer_email": CUSTOMER})
        self.assertEqual(self.go(2)["reason"], auto_follow_up.THREAD_NOT_FOUND)
        self.assertEqual(self.gmail.sent, [])

    def test_staff_replies_do_not_stop_but_a_customer_reply_does(self):
        self.quote()
        self.gmail.add(msg("t1", "rabin@avigiri.com", to=CUSTOMER, subject=f"Re: {SUBJECT}"))
        self.assertEqual(self.go(2)["result"], "sent")
        self.gmail.add(msg("t1", CUSTOMER, to=OURS, subject=f"Re: {SUBJECT}", body="We will confirm"))
        self.assertEqual(self.go(4)["reason"], "customer replied; answer them manually")
        self.assertEqual(self.go(10)["result"], "skipped")
        self.assertFalse(MarketingLead.objects.exists())

    def test_a_bounce_stops_and_flags(self):
        self.quote()
        self.gmail.add(msg("t1", "mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)"))
        self.assertEqual(self.go(2)["reason"], "the customer's email bounced")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="ENQ-1").follow_up_flag, "bounced")
        self.assertEqual(self.gmail.sent, [])

    def test_a_logged_call_stops_the_sequence_and_nothing_moves(self):
        self.quote()
        EnquiryFollowup.objects.create(enquiry_id="ENQ-1", followup_type="CALL", note="spoke to buyer", user=self.user)
        self.assertIn("logged", self.go(2)["reason"])
        self.assertEqual(self.gmail.sent, [])
        self.assertFalse(MarketingLead.objects.exists())

    def test_share_link_answer_stops_the_sequence(self):
        self.quote()
        self.enquiries["ENQ-1"]["status"] = "accepted"
        self.assertIn("enquiry follow-ups are sent only while it is Open", self.go(2)["reason"])

    def test_a_proforma_invoice_follows_the_same_rhythm_with_its_own_wording(self):
        sent = self.gmail.add(msg("tp", OURS, to=CUSTOMER, subject="Proforma Invoice PI-9", body="PI for ENQ-1"))
        auto_follow_up.record_start("ENQ-1", "payment_pending", sent={"threadId": "tp", "id": sent["id"]},
                                    customer_email=CUSTOMER, cc=[], subject="Proforma Invoice PI-9")
        self.assertIn("not due until", self.go(1)["reason"])
        self.assertEqual(self.go(2)["result"], "sent")
        self.assertIn("proforma invoice", self.gmail.sent[-1]["body"])
        self.assertEqual(self.gmail.sent[-1]["thread"], "tp")

    def test_revised_quotation_does_not_restart(self):
        first = self.quote()
        self.assertEqual(self.go(2)["result"], "sent")
        again = auto_follow_up.record_start("ENQ-1", QUOTATION_STAGE, sent={"threadId": "t1", "id": "x"},
                                            customer_email=CUSTOMER, cc=[], subject=SUBJECT)
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(auto_follow_up.cycles(["ENQ-1"])["ENQ-1"].count, 1)

    def test_thread_not_found_is_never_moved_to_marketing(self):
        self.quote()
        self.gmail.threads["t1"][0]["subject"] = "Something else"
        for days in range(2, 30, 2):
            self.go(days)
        self.assertFalse(MarketingLead.objects.exists())
        self.assertEqual(self.updates, [])

    def test_no_two_emails_to_one_customer_within_24_hours(self):
        self.quote()
        FollowUpSent.objects.create(enquiry_id="ENQ-9", customer_email=CUSTOMER, sent_by=self.user,
                                    status=FollowUpSent.STATUS_SENT, stage=MARKETING_STAGE)
        with override_settings(FOLLOW_UP_MIN_GAP_HOURS=24):
            self.assertIn("last 24 hours", self.go(2)["reason"])
        self.assertEqual(self.gmail.sent, [])

    def test_a_failed_send_is_retried_three_times_then_logged(self):
        self.quote()
        with patch.object(self.gmail, "send_reply", side_effect=RuntimeError("Gmail down")):
            for _ in range(3):
                self.assertEqual(self.go(2)["result"], "failed")
        self.assertIn("could not send this email 3 times", self.go(2)["reason"])
        self.assertTrue(FollowUpNote.objects.filter(enquiry_id="ENQ-1", kind="error").exists())

    def test_quote_sent_straight_from_gmail_is_found_and_started(self):
        EnquiryContact.objects.create(enquiry_id="ENQ-1", customer_email=CUSTOMER)
        self.gmail.add(msg("tg", OURS, to=CUSTOMER, subject="Offer for door seals", body="Ref ENQ-1, please find"))
        found = auto_follow_up.detect_gmail_quotes(list(self.enquiries.values()))
        self.assertEqual(found, [{"enquiry_id": "ENQ-1", "result": "started", "stage": QUOTATION_STAGE}])
        start = FollowUpStart.objects.get(enquiry_id="ENQ-1")
        self.assertEqual((start.gmail_thread_id, start.subject), ("tg", "Offer for door seals"))
        self.assertEqual(auto_follow_up.detect_gmail_quotes(list(self.enquiries.values())), [])  # once

    def test_manual_send_counts_as_the_next_number_and_needs_the_verified_thread(self):
        self.quote()
        self.assertEqual(self.go(2)["result"], "sent")
        plan = auto_follow_up.manual_send_plan(self.gmail, "ENQ-1", thread_id="t1", recipient=CUSTOMER, stage="general")
        self.assertEqual((plan["stage"], plan["attempt"], plan["cc"], plan["error"]), (QUOTATION_STAGE, 2, ENQUIRY_CC, ""))
        wrong = auto_follow_up.manual_send_plan(self.gmail, "ENQ-1", thread_id="nope", recipient=CUSTOMER,
                                                stage="general")
        self.assertIn("Gmail thread not found", wrong["error"])
        other = auto_follow_up.manual_send_plan(self.gmail, "ENQ-1", thread_id="t1", recipient="x@y.example",
                                                stage="general")
        self.assertIn("only to the verified customer email", other["error"])
        FollowUpSent.objects.create(enquiry_id="ENQ-1", attempt=2, customer_email=CUSTOMER, sent_by=self.user,
                                    status=FollowUpSent.STATUS_SENT, stage=QUOTATION_STAGE, gmail_thread_id="t1",
                                    next_follow_up_on=self.day + timedelta(days=6))
        self.go(4)  # not due: the manual Follow-up 2 moved the next date
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.go(6)["attempt"], 3)

    def test_dry_run_sends_nothing_but_moves_on_and_still_closes(self):
        self.quote()
        with override_settings(FOLLOW_UP_AUTO_DRY_RUN=True), self.assertLogs("enquiries.auto_follow_up", "INFO") as logs:
            for days in (2, 4, 6, 8, 10):
                self.assertEqual(self.go(days)["result"], "dry_run")
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(FollowUpSent.objects.filter(status=FollowUpSent.STATUS_DRY_RUN).count(), 5)
        self.assertTrue(any(f"To: {CUSTOMER}" in line and "CC: customerservice@avigiri.com" in line
                            and "Thread: t1" in line for line in logs.output))
        self.assertEqual(MarketingLead.objects.get(enquiry_id="ENQ-1").lead_source, "no_response")
        self.assertEqual(CustomerStatusEmail.objects.get(enquiry_id="ENQ-1").status, CustomerStatusEmail.STATUS_DRY_RUN)


class MarketingSequenceTests(Base):
    def setUp(self):
        super().setUp()
        self.enquiries["ENQ-1"]["status"] = "rejected"
        EnquiryContact.objects.create(enquiry_id="ENQ-1", customer_email=CUSTOMER)

    def test_rejection_makes_a_ready_lead_and_follow_up_1_goes_on_the_next_run(self):
        status_emails.mark_marketing_ready("ENQ-1", self.enquiries["ENQ-1"])
        lead = MarketingLead.objects.get(enquiry_id="ENQ-1")
        self.assertEqual(lead.lead_source, "rejected")
        self.assertIsNotNone(lead.ready_at)
        result = self.go(0)
        self.assertEqual(result["result"], "sent", result)
        sent = self.gmail.sent[-1]
        self.assertEqual(sent["kind"], "new")  # a new thread, never the enquiry's
        self.assertEqual(sent["subject"], "Thank you for considering AVIGIRI, Acme Seals")
        self.assertEqual(sent["cc"], MARKETING_CC)
        self.assertTrue(sent["body"].endswith("Reply STOP if you'd prefer not to hear from us."))
        # Follow-up 2 replies in Follow-up 1's thread, with the CC set again.
        result = self.go(2)
        self.assertEqual(result["result"], "sent", result)
        self.assertEqual((self.gmail.sent[-1]["kind"], self.gmail.sent[-1]["thread"]), ("reply", sent["thread"]))
        self.assertEqual(self.gmail.sent[-1]["subject"], f"Re: {sent['subject']}")
        self.assertEqual(self.gmail.sent[-1]["cc"], MARKETING_CC)
        status_emails.mark_marketing_ready("ENQ-1", self.enquiries["ENQ-1"])  # again: no second lead or start
        self.assertEqual(MarketingLead.objects.count(), 1)
        self.assertEqual(FollowUpStart.objects.filter(stage=MARKETING_STAGE).count(), 1)

    def test_no_response_lead_uses_its_own_follow_up_1_after_the_delay(self):
        lost.create_lead("ENQ-1", lost.NO_RESPONSE, customer_email=CUSTOMER,
                         first_due=timezone.now() + timedelta(days=2))
        self.assertIn("not due until", self.go(1)["reason"])
        self.assertEqual(self.go(2)["result"], "sent")
        self.assertEqual(self.gmail.sent[-1]["subject"], "Is Door seals still on your plans, Acme Seals?")

    def test_later_emails_only_in_the_verified_marketing_thread(self):
        lost.create_lead("ENQ-1", lost.REJECTED, customer_email=CUSTOMER, first_due=timezone.now())
        self.go(0)
        thread = self.gmail.sent[-1]["thread"]
        self.gmail.threads[thread][0]["subject"] = "changed"
        self.assertEqual(self.go(2)["reason"], auto_follow_up.THREAD_NOT_FOUND)
        self.assertEqual(MarketingLead.objects.get().flag, "thread_not_found")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_stop_reply_marks_do_not_contact(self):
        lost.create_lead("ENQ-1", lost.REJECTED, customer_email=CUSTOMER, first_due=timezone.now())
        self.go(0)
        thread = self.gmail.sent[-1]["thread"]
        self.gmail.add(msg(thread, CUSTOMER, subject="Re: x", body="STOP please"))
        self.assertEqual(self.go(2)["reason"], "customer replied; answer them manually")
        lead = MarketingLead.objects.get()
        self.assertTrue(lead.do_not_contact)
        self.assertEqual(lead.flag, "replied")
        self.assertEqual(self.go(4)["result"], "skipped")

    def test_reply_in_the_enquiry_thread_after_the_move_stops_marketing(self):
        self.gmail.add(msg("t1", OURS, to=CUSTOMER, subject=SUBJECT, body="ENQ-1"))
        FollowUpStart.objects.create(enquiry_id="ENQ-1", stage=QUOTATION_STAGE, gmail_thread_id="t1",
                                     customer_email=CUSTOMER, subject=SUBJECT,
                                     started_at=timezone.now() - timedelta(days=20))
        lead = lost.create_lead("ENQ-1", lost.NO_RESPONSE, customer_email=CUSTOMER, first_due=timezone.now())
        self.gmail.add(msg("t1", CUSTOMER, subject=f"Re: {SUBJECT}", body="still interested",
                           when=lead.ready_at + timedelta(hours=1)))
        self.assertIn("Marketing stopped", self.go(0)["reason"])
        self.assertEqual(MarketingLead.objects.get().flag, "replied")
        self.assertEqual(self.gmail.sent, [])


@override_settings(**SETTINGS)
class SchedulerAndMigrationTests(Base):
    def test_per_run_cap_spreads_the_emails(self):
        for n in range(1, 4):
            eid = f"ENQ-{n}"
            self.enquiries[eid] = {"enquiry_id": eid, "status": "open", "subject": "Door seals"}
            self.quote(eid, thread=f"t{n}", customer=f"buyer{n}@acme.example")
            FollowUpStart.objects.filter(enquiry_id=eid).update(started_at=timezone.now() - timedelta(days=3))
        with override_settings(FOLLOW_UP_SEND_CAP_PER_RUN=2):
            result = scheduler.follow_up_step()
        self.assertEqual(len(self.gmail.sent), 2)
        self.assertIn("FOLLOW_UP_SEND_CAP_PER_RUN", result["stopped"])

    def test_lead_catch_up_is_idempotent(self):
        self.enquiries["ENQ-2"] = {"enquiry_id": "ENQ-2", "status": "closed", "subject": "Gaskets"}
        EnquiryContact.objects.create(enquiry_id="ENQ-2", customer_email=CUSTOMER, close_reason="no_response")
        self.enquiries["ENQ-3"] = {"enquiry_id": "ENQ-3", "status": "rejected", "subject": "Rings"}
        EnquiryContact.objects.create(enquiry_id="ENQ-3", customer_email="b3@acme.example")
        enquiries = list(self.enquiries.values())
        first = scheduler.marketing_lead_step(enquiries)
        scheduler.marketing_lead_step(enquiries)
        self.assertEqual(sorted(first["created"]), ["ENQ-2", "ENQ-3"])
        self.assertEqual(MarketingLead.objects.count(), 2)
        self.assertEqual(dict(MarketingLead.objects.values_list("enquiry_id", "lead_source")),
                         {"ENQ-2": "no_response", "ENQ-3": "rejected"})
        self.assertEqual(FollowUpStart.objects.filter(stage=MARKETING_STAGE).count(), 2)


class TemplateAndUiTests(TestCase):
    @override_settings(FOLLOW_UP_CONTACT_PHONE="", MARKETING_REVISED_QUOTE_DAYS="", MARKETING_FOLLOW_UP_COUNT=4)
    def test_no_unfilled_placeholders(self):
        bodies = [build_follow_up_body(QUOTATION_STAGE, attempt=n) for n in range(1, 5)]
        bodies += [build_follow_up_body(MARKETING_STAGE, attempt=n, lead_source=s)
                   for n in range(1, 6) for s in ("rejected", "no_response")]
        bodies += [enquiry_closing_body(), marketing_subject(1, lead_source="no_response")]
        for body in bodies:
            self.assertNotIn("{", body)
            self.assertNotIn("}", body)

    def test_no_start_or_send_now_buttons_remain(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for template in (root / "enquiries/templates/enquiries/board.html",
                         root / "marketing/templates/marketing/detail.html",
                         root / "marketing/templates/marketing/board.html"):
            text = template.read_text(encoding="utf-8")
            self.assertNotIn("Start follow-ups", text)
            self.assertNotIn("Send first follow-up now", text)

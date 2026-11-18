"""The whole schedule day by day with a fake clock, for both portals:
quotation / PI: follow-ups on day 2, 4, 6, 8, then the closing email on day 10 (one email thread);
rejection (Marketing): its own new thread, follow-ups on day 2, 4, 6, 8, then Marketing's closing email on day 10."""
from datetime import datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from enquiries import auto_follow_up, status_emails
from enquiries.models import FollowUpSent, FollowUpStart

OURS = "sales@ourcompany.example"
SETTINGS = dict(
    FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False, FOLLOW_UP_AUTO_SENDER="sales",
    FOLLOW_UP_AUTO_GAP_DAYS=[2], FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=2,
    MARKETING_AUTO_FOLLOW_UP=True, MARKETING_FOLLOW_UP_COUNT=4,
)
DAY0 = datetime(2026, 11, 16, 10, 0, tzinfo=dt_timezone.utc)


class Clock:
    def __init__(self):
        self.now = DAY0

    def __call__(self):
        return self.now


@override_settings(**SETTINGS)
class TimelineTests(TestCase):
    def setUp(self):
        self.sales = get_user_model().objects.create_user("sales", OURS, "x")
        self.clock = Clock()
        patch("django.utils.timezone.now", self.clock).start()
        self.enquiry = {"enquiry_id": "E1", "status": "open", "subject": "Gaskets", "customer_name": "Acme"}
        patch.object(auto_follow_up.services, "fetch_enquiry", side_effect=lambda *a, **k: self.enquiry).start()
        patch.object(auto_follow_up.services, "list_enquiries", side_effect=lambda *a, **k: [self.enquiry]).start()
        self.gmail = MagicMock()
        self.gmail.own_addresses.return_value = {OURS}
        self.thread_messages = [{"id": "m1", "sender": OURS}]
        self.last_thread = ""

        def thread(thread_id, limit=50):
            self.last_thread = thread_id
            return list(self.thread_messages)

        self.gmail.get_thread_messages.side_effect = thread
        # Like Gmail: a message and its replies belong to the thread they were read from.
        self.gmail.get_message.side_effect = lambda message_id: {
            "id": message_id, "subject": "Re: Gaskets", "thread_id": self.last_thread}
        self.gmail.send_reply.side_effect = lambda **kw: {"id": "r", "threadId": kw["original_message"]["thread_id"]}
        self.gmail.send_email.return_value = {"id": "n", "threadId": "T-MKT"}
        patch.object(auto_follow_up, "GmailService", return_value=self.gmail).start()

        def update(api_root, enquiry_id, values, *a, **k):
            self.enquiry.update(values)
            return {}
        patch("enquiries.services.update_enquiry", side_effect=update).start()
        self.closings = patch.object(status_emails, "send_status_email", return_value={"result": "sent"}).start()
        self.addCleanup(patch.stopall)

    def _day(self, day):
        """What the hourly scheduled run does on this day: due follow-ups, then the closing email. Returns what went out."""
        self.clock.now = DAY0.replace(day=DAY0.day + day)
        today = timezone.localdate()
        sent = []
        for item in auto_follow_up.due_follow_ups(today):
            before = (self.gmail.send_reply.call_count, self.gmail.send_email.call_count)
            result = auto_follow_up.run_auto_follow_up(item["enquiry_id"], today)
            if result["result"] == "sent":
                how = "new email" if self.gmail.send_email.call_count > before[1] else "reply"
                sent.append((item["stage"], item["attempt"], how))
        for enquiry in status_emails.closing_due([self.enquiry], now=self.clock.now):
            outcome = status_emails.send_closing_email(enquiry, "http://api")
            if outcome.get("result") == "sent":
                sent.append(("closing", 0, "enquiry closed" if self.enquiry["status"] == "closed" else "?"))
        return sent

    def _run(self, days=13):
        return {day: events for day in range(days) if (events := self._day(day))}

    def test_quotation_follow_ups_every_two_days_then_the_closing_email(self):
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T-Q",
                                     customer_email="buyer@acme.example", started_at=DAY0)
        timeline = self._run()
        self.assertEqual(timeline, {
            2: [("waiting_po", 1, "reply")], 4: [("waiting_po", 2, "reply")],
            6: [("waiting_po", 3, "reply")], 8: [("waiting_po", 4, "reply")],
            10: [("closing", 0, "enquiry closed")],
        })
        self.assertEqual(self.enquiry["status"], "closed")
        self.assertEqual(set(FollowUpSent.objects.values_list("gmail_thread_id", flat=True)), {"T-Q"})  # one thread
        self.gmail.send_email.assert_not_called()

    def test_proforma_invoice_follow_ups_use_the_same_rhythm(self):
        FollowUpStart.objects.create(enquiry_id="E1", stage="payment_pending", gmail_thread_id="T-Q",
                                     customer_email="buyer@acme.example", started_at=DAY0)
        self.enquiry["status"] = "open"  # PI follow-ups are sent only while the enquiry is Open
        timeline = self._run()
        self.assertEqual([day for day, events in timeline.items() if events[0][0] == "payment_pending"], [2, 4, 6, 8])
        self.assertEqual(timeline[10], [("closing", 0, "enquiry closed")])  # an unpaid PI is closed too

    def test_rejection_marketing_gets_its_own_thread_and_its_own_closing_email(self):
        self.enquiry["status"] = "rejected"
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T-Q",
                                     customer_email="buyer@acme.example", started_at=DAY0.replace(day=1))
        FollowUpStart.objects.create(enquiry_id="E1", stage="marketing", gmail_thread_id="",
                                     customer_email="buyer@acme.example", started_at=DAY0)
        timeline = self._run()
        self.assertEqual(timeline, {
            2: [("marketing", 1, "new email")], 4: [("marketing", 2, "reply")],
            6: [("marketing", 3, "reply")], 8: [("marketing", 4, "reply")],
            10: [("marketing", 5, "reply")],  # the closing email, in Marketing's thread
        })
        threads = set(FollowUpSent.objects.filter(stage="marketing").values_list("gmail_thread_id", flat=True))
        self.assertEqual(threads, {"T-MKT"})  # never the enquiry's T-Q
        self.assertEqual(self.gmail.send_email.call_count, 1)
        self.assertEqual(self.enquiry["status"], "rejected")  # the enquiry stays rejected; nothing else changes
        self.closings.assert_not_called()  # the enquiry-side closing email is not used for Marketing

    def test_a_customer_reply_stops_the_whole_sequence(self):
        FollowUpStart.objects.create(enquiry_id="E1", stage="waiting_po", gmail_thread_id="T-Q",
                                     customer_email="buyer@acme.example", started_at=DAY0)
        self.assertEqual(self._day(2), [("waiting_po", 1, "reply")])
        self.thread_messages = [{"id": "m1", "sender": OURS}, {"id": "m2", "sender": "Buyer <buyer@acme.example>"}]
        self.assertEqual([self._day(d) for d in (4, 6, 8, 10)], [[], [], [], []])

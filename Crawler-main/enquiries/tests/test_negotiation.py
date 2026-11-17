"""Negotiation status, forwarding an enquiry to a department, the task queue and the timeline."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from enquiries import negotiation
from enquiries.models import EnquiryAssignment, EnquiryNegotiation, EnquiryStatusLog
from kyc.models import Department, UserDepartment

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
User = get_user_model()
ENQUIRY = {"enquiry_id": "ENQ-1", "customer_name": "Manu Traders", "status": "open"}


def user(name, email):
    return User.objects.create_user(name, email, "x")


@override_settings(**BASE)
@mock.patch("enquiries.views._get_api_root", return_value="http://svc")
@mock.patch("enquiries.negotiation_views.services.fetch_enquiry", return_value=ENQUIRY)
class NegotiationTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.marketing = Department.objects.get(name="Marketing")
        self.sales = Department.objects.get(name="Sales")
        self.manager = user("apai", "apai@avigiri.com")           # Top Management (and Marketing)
        self.rep = user("rabin", "rabin@avigiri.com")             # Sales
        self.mkt = user("kumar", "kumar@avigiri.com")             # Marketing
        self.mkt2 = user("manju", "manju@avigiri.com")            # Marketing
        self.acct = user("amy", "accounts@avigiri.com")           # Accounts
        self.admin = User.objects.create_user("boss", "boss@example.com", "x", is_superuser=True)
        self.forward_url = reverse("enquiries:negotiation_forward", args=["ENQ-1"])
        self.good = {"department": self.marketing.pk, "comment": "Prepare a detailed cost breakup sheet.",
                     "due_date": str(timezone.localdate() + timedelta(days=2)), "priority": "HIGH"}

    def test_stage_is_set_logged_and_shown_on_the_timeline(self, *_):
        self.client.force_login(self.rep)
        url = reverse("enquiries:negotiation_stage", args=["ENQ-1"])
        self.client.post(url, {"stage": "under_negotiation", "note": "Customer wants a cost breakup"})
        self.assertEqual(negotiation.current_stage("ENQ-1"), "under_negotiation")
        log = EnquiryStatusLog.objects.get()
        self.assertEqual((log.username, log.old_status, log.new_status), ("rabin", "No negotiation", "Under negotiation"))
        self.client.post(url, {"stage": "under_negotiation"})  # unchanged: nothing more is logged
        self.client.post(url, {"stage": "revised_quotation_sent"})
        self.assertEqual(EnquiryStatusLog.objects.count(), 2)
        page = self.client.get(reverse("enquiries:negotiation", args=["ENQ-1"]))
        self.assertContains(page, "Under negotiation to Revised quotation sent".replace("to", "to"))
        self.assertContains(page, "Customer wants a cost breakup")

    def test_a_bad_stage_is_refused(self, *_):
        self.client.force_login(self.rep)
        self.client.post(reverse("enquiries:negotiation_stage", args=["ENQ-1"]), {"stage": "won"})
        self.assertFalse(EnquiryNegotiation.objects.exists())

    def test_only_a_manager_or_top_management_can_forward(self, *_):
        for who in (self.rep, self.mkt, self.acct):
            self.client.force_login(who)
            self.assertEqual(self.client.post(self.forward_url, self.good).status_code, 403)
        self.assertFalse(EnquiryAssignment.objects.exists())
        for who in (self.manager, self.admin):
            self.client.force_login(who)
            self.assertEqual(self.client.post(self.forward_url, self.good).status_code, 302)
        self.assertEqual(EnquiryAssignment.objects.count(), 2)

    def test_the_forward_button_shows_only_for_those_who_can_forward(self, *_):
        url = reverse("enquiries:negotiation", args=["ENQ-1"])
        self.client.force_login(self.rep)
        self.assertNotContains(self.client.get(url), "Forward to Department")
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(url), "Forward to Department")

    def test_forwarding_needs_a_comment_and_a_real_department(self, *_):
        self.client.force_login(self.manager)
        self.client.post(self.forward_url, {**self.good, "comment": "   "})
        self.client.post(self.forward_url, {**self.good, "department": 9999})
        self.client.post(self.forward_url, {**self.good, "due_date": "not-a-date"})
        self.assertFalse(EnquiryAssignment.objects.exists())

    def test_forwarding_records_the_task_without_changing_the_enquiry(self, fetch, _root):
        self.client.force_login(self.manager)
        self.client.post(self.forward_url, self.good)
        task = EnquiryAssignment.objects.get()
        self.assertEqual((task.to_department, task.assigned_by, task.priority, task.status),
                         (self.marketing, self.manager, "HIGH", "PENDING"))
        self.assertEqual(task.customer_name, "Manu Traders")
        self.assertIsNone(task.assigned_to)
        self.assertFalse(EnquiryStatusLog.objects.exists())  # the enquiry's own status is untouched

    def test_assignee_must_be_in_the_department(self, *_):
        self.client.force_login(self.manager)
        self.client.post(self.forward_url, {**self.good, "assigned_to": self.acct.pk})
        self.assertFalse(EnquiryAssignment.objects.exists())
        self.client.post(self.forward_url, {**self.good, "assigned_to": self.mkt.pk})
        self.assertEqual(EnquiryAssignment.objects.get().assigned_to, self.mkt)

    def test_the_queue_shows_a_department_only_its_own_tasks(self, *_):
        EnquiryAssignment.objects.create(enquiry_id="ENQ-1", to_department=self.marketing, comment="m", assigned_by=self.manager)
        EnquiryAssignment.objects.create(enquiry_id="ENQ-2", to_department=self.sales, comment="s", assigned_by=self.manager)
        url = reverse("enquiries:tasks")
        self.client.force_login(self.mkt)
        page = self.client.get(url)
        self.assertContains(page, "ENQ-1")
        self.assertNotContains(page, "ENQ-2")
        self.client.force_login(self.acct)
        self.assertNotContains(self.client.get(url), "ENQ-1")
        self.client.force_login(self.manager)  # Top Management sees every department
        page = self.client.get(url)
        self.assertContains(page, "ENQ-1")
        self.assertContains(page, "ENQ-2")

    def test_overdue_tasks_are_flagged_and_listed_first(self, *_):
        today = timezone.localdate()
        EnquiryAssignment.objects.create(enquiry_id="ENQ-NEW", to_department=self.marketing, comment="c", due_date=today + timedelta(days=3))
        old = EnquiryAssignment.objects.create(enquiry_id="ENQ-OLD", to_department=self.marketing, comment="c", due_date=today - timedelta(days=1))
        done = EnquiryAssignment.objects.create(enquiry_id="ENQ-DONE", to_department=self.marketing, comment="c",
                                                due_date=today - timedelta(days=5), status="COMPLETED")
        self.assertTrue(old.is_overdue)
        self.assertFalse(done.is_overdue)  # a completed task is never overdue
        self.client.force_login(self.mkt)
        page = self.client.get(reverse("enquiries:tasks")).content.decode()
        self.assertIn("Overdue", page)
        self.assertLess(page.index("ENQ-OLD"), page.index("ENQ-NEW"))
        self.assertNotIn("ENQ-DONE", page)  # open tasks only by default

    def test_the_department_can_start_and_complete_a_task_but_others_cannot(self, *_):
        task = EnquiryAssignment.objects.create(enquiry_id="ENQ-1", to_department=self.marketing, comment="c", assigned_by=self.manager)
        url = reverse("enquiries:task_update", args=[task.pk])
        self.client.force_login(self.acct)
        self.assertEqual(self.client.post(url, {"status": "COMPLETED"}).status_code, 403)
        self.client.force_login(self.mkt)
        self.client.post(url, {"status": "IN_PROGRESS"})
        task.refresh_from_db()
        self.assertEqual((task.status, task.assigned_to), ("IN_PROGRESS", self.mkt))  # starting picks it up
        self.client.post(url, {"status": "COMPLETED"})
        task.refresh_from_db()
        self.assertEqual(task.status, "COMPLETED")
        self.assertIsNotNone(task.completed_at)
        self.client.post(url, {"status": "PENDING"})
        task.refresh_from_db()
        self.assertIsNone(task.completed_at)

    def test_the_menu_badge_counts_open_tasks_for_the_users_department(self, *_):
        EnquiryAssignment.objects.create(enquiry_id="ENQ-1", to_department=self.marketing, comment="c")
        EnquiryAssignment.objects.create(enquiry_id="ENQ-2", to_department=self.marketing, comment="c", status="COMPLETED")
        EnquiryAssignment.objects.create(enquiry_id="ENQ-3", to_department=self.sales, comment="c")
        self.assertEqual(negotiation.pending_task_count(self.mkt), 1)
        self.assertEqual(negotiation.pending_task_count(self.rep), 1)
        self.assertEqual(negotiation.pending_task_count(self.acct), 0)
        self.client.force_login(self.mkt)
        self.assertContains(self.client.get(reverse("enquiries:tasks")), "Pending Tasks")

    def test_the_timeline_is_merged_and_newest_first(self, *_):
        negotiation.log_status_change(self.rep, "ENQ-1", "open", "accepted")
        task = EnquiryAssignment.objects.create(enquiry_id="ENQ-1", to_department=self.marketing, comment="Prepare breakup",
                                                assigned_by=self.manager, completed_at=timezone.now() + timedelta(hours=1))
        items = negotiation.timeline("ENQ-1")
        self.assertEqual([i["kind"] for i in items], ["assignment_done", "assignment", "status"])
        self.assertIn("Forwarded to Marketing", items[1]["text"])

    def test_a_status_change_on_the_board_is_logged(self, *_):
        from enquiries import views as enquiry_views
        request = RequestFactory().post("/")
        request.user = self.rep
        with mock.patch.object(enquiry_views.status_emails, "send_status_email", return_value={}):
            enquiry_views._after_status_change(request, "ENQ-9", {"status": "open"}, "accepted")
        log = EnquiryStatusLog.objects.get(enquiry_id="ENQ-9")
        self.assertEqual((log.old_status, log.new_status, log.username), ("open", "accepted", "rabin"))

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

    def test_only_the_cro_rabin_and_apai_can_forward(self, *_):
        cro = user("cro", "CustomerService@avigiri.com")
        for who in (self.mkt, self.acct, self.admin):
            self.client.force_login(who)
            self.assertEqual(self.client.post(self.forward_url, self.good).status_code, 403)
        self.assertFalse(EnquiryAssignment.objects.exists())
        for who in (self.rep, cro, self.manager):
            self.client.force_login(who)
            self.assertEqual(self.client.post(self.forward_url, self.good).status_code, 302)
        self.assertEqual(EnquiryAssignment.objects.count(), 3)

    def test_the_forward_button_shows_only_for_those_who_can_forward(self, *_):
        url = reverse("enquiries:negotiation", args=["ENQ-1"])
        self.client.force_login(self.mkt)
        self.assertNotContains(self.client.get(url), "Forward to Department")
        for who in (self.rep, user("cro", "customerservice@avigiri.com"), self.manager):
            self.client.force_login(who)
            self.assertContains(self.client.get(url), "Forward to Department")

    def test_forwarding_rights_do_not_widen_the_task_queue(self, *_):
        EnquiryAssignment.objects.create(enquiry_id="ENQ-2", to_department=self.marketing, comment="m", assigned_by=self.manager)
        self.assertTrue(negotiation.can_forward(self.rep))
        self.assertFalse(negotiation.tasks_for(self.rep).exists())

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


@override_settings(**BASE)
@mock.patch("enquiries.views._get_api_root", return_value="http://svc")
@mock.patch("enquiries.negotiation_views.services.fetch_enquiry", return_value=ENQUIRY)
class FollowupAndAttachmentTests(TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from django.core.files.storage import FileSystemStorage
        from enquiries.models import EnquiryAttachment
        # The attachment storage is fixed when the model loads, so point it at a temporary folder for the test.
        folder = tempfile.mkdtemp(prefix="enq-attach-test-")
        field = EnquiryAttachment._meta.get_field("file")
        original = field.storage
        field.storage = FileSystemStorage(location=folder)
        self.addCleanup(lambda: (setattr(field, "storage", original), shutil.rmtree(folder, ignore_errors=True)))
        call_command("seed_departments")
        self.marketing = Department.objects.get(name="Marketing")
        self.manager = user("apai", "apai@avigiri.com")
        self.mkt = user("kumar", "kumar@avigiri.com")
        self.acct = user("amy", "accounts@avigiri.com")
        self.task = EnquiryAssignment.objects.create(enquiry_id="ENQ-1", to_department=self.marketing,
                                                     comment="Prepare breakup", assigned_by=self.manager)
        self.followup_url = reverse("enquiries:followup_add", args=["ENQ-1"])

    def _pdf(self, name="breakup.pdf", content=b"%PDF-1.4 test"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def test_a_followup_with_next_date_and_attachment_is_saved_and_on_the_timeline(self, *_):
        from enquiries.models import EnquiryAttachment, EnquiryFollowup
        self.client.force_login(self.mkt)
        nxt = timezone.localdate() + timedelta(days=3)
        self.client.post(self.followup_url, {"note": "Sent breakup sheet via email", "followup_type": "EMAIL",
                                             "next_followup_date": str(nxt), "assignment": self.task.pk,
                                             "category": "COST_BREAKUP", "file": self._pdf()})
        f = EnquiryFollowup.objects.get()
        self.assertEqual((f.user, f.department, f.assignment, f.next_followup_date), (self.mkt, self.marketing, self.task, nxt))
        a = EnquiryAttachment.objects.get()
        self.assertEqual((a.followup, a.category, a.file_name, a.uploaded_by), (f, "COST_BREAKUP", "breakup.pdf", self.mkt))
        kinds = [i["kind"] for i in negotiation.timeline("ENQ-1")]
        self.assertIn("followup", kinds)
        self.assertIn("attachment", kinds)

    def test_a_followup_needs_a_note_and_a_valid_date(self, *_):
        from enquiries.models import EnquiryFollowup
        self.client.force_login(self.mkt)
        self.client.post(self.followup_url, {"note": "  "})
        self.client.post(self.followup_url, {"note": "x", "next_followup_date": "soon"})
        self.assertFalse(EnquiryFollowup.objects.exists())

    def test_a_followup_is_due_from_its_date_until_done_or_replaced(self, *_):
        from enquiries.models import EnquiryFollowup
        today = timezone.localdate()
        soon = EnquiryFollowup.objects.create(enquiry_id="ENQ-1", department=self.marketing, user=self.mkt, note="a",
                                              next_followup_date=today + timedelta(days=1))
        late = EnquiryFollowup.objects.create(enquiry_id="ENQ-2", department=self.marketing, user=self.mkt, note="b",
                                              next_followup_date=today)
        self.assertFalse(soon.is_due)
        self.assertTrue(late.is_due)
        self.assertEqual([f.enquiry_id for f in negotiation.due_followups(self.mkt)], ["ENQ-2"])
        self.assertEqual(negotiation.due_followups(self.acct), [])  # another department does not see it
        self.client.force_login(self.mkt)
        self.assertContains(self.client.get(reverse("enquiries:tasks")), "Follow-ups due (1)")
        self.client.post(reverse("enquiries:followup_done", args=[late.pk]))
        self.assertEqual(negotiation.due_followups(self.mkt), [])

    def test_a_new_followup_clears_the_earlier_one_that_was_waiting_for_it(self, *_):
        from enquiries.models import EnquiryFollowup
        old = EnquiryFollowup.objects.create(enquiry_id="ENQ-1", user=self.mkt, note="a",
                                             next_followup_date=timezone.localdate() - timedelta(days=1))
        self.client.force_login(self.mkt)
        self.client.post(self.followup_url, {"note": "Called the customer"})
        old.refresh_from_db()
        self.assertTrue(old.next_done)

    def test_attachment_type_and_size_are_checked(self, *_):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from enquiries import attachments
        from enquiries.models import EnquiryAttachment
        self.client.force_login(self.mkt)
        url = reverse("enquiries:attachment_add", args=["ENQ-1"])
        self.client.post(url, {"file": SimpleUploadedFile("run.exe", b"MZ")})
        self.client.post(url, {"file": SimpleUploadedFile("empty.pdf", b"")})
        with mock.patch.object(attachments, "MAX_BYTES", 5):
            self.client.post(url, {"file": SimpleUploadedFile("big.pdf", b"123456789")})
        self.assertFalse(EnquiryAttachment.objects.exists())
        self.client.post(url, {"file": self._pdf("email.pdf"), "category": "CUSTOMER_EMAIL"})
        self.assertEqual(EnquiryAttachment.objects.get().category, "CUSTOMER_EMAIL")

    def test_the_attached_file_downloads_for_signed_in_users_only(self, *_):
        from enquiries.models import EnquiryAttachment
        self.client.force_login(self.mkt)
        self.client.post(reverse("enquiries:attachment_add", args=["ENQ-1"]), {"file": self._pdf("email.pdf")})
        att = EnquiryAttachment.objects.get()
        url = reverse("enquiries:attachment_download", args=[att.pk])
        response = self.client.get(url)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 test")
        self.assertIn("email.pdf", response["Content-Disposition"])
        self.client.logout()
        self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_forwarding_can_carry_a_file(self, *_):
        from enquiries.models import EnquiryAttachment
        self.client.force_login(self.manager)
        self.client.post(reverse("enquiries:negotiation_forward", args=["ENQ-2"]), {
            "department": self.marketing.pk, "comment": "Please follow up", "file": self._pdf("customer-mail.pdf"),
            "category": "CUSTOMER_EMAIL"})
        task = EnquiryAssignment.objects.get(enquiry_id="ENQ-2")
        self.assertEqual(EnquiryAttachment.objects.get().assignment, task)

    def test_the_negotiation_page_shows_the_forms(self, *_):
        self.client.force_login(self.mkt)
        page = self.client.get(reverse("enquiries:negotiation", args=["ENQ-1"]))
        for text in ("Add Follow-up", "Attach file", "Attachments", "Follow-ups"):
            self.assertContains(page, text)


@override_settings(**BASE)
class LostReasonTests(TestCase):
    def test_reason_parsing(self):
        from enquiries.views import _is_new_rejection, _lost_reason_from
        self.assertEqual(_lost_reason_from({"lost_reason": "Price too high"}), "Price too high")
        self.assertEqual(_lost_reason_from({"lost_reason": "Other", "lost_reason_note": " bought   elsewhere "}), "Other: bought elsewhere")
        self.assertEqual(_lost_reason_from({"lost_reason": "Because"}), "")
        self.assertEqual(_lost_reason_from({}), "")
        self.assertTrue(_is_new_rejection("open", "rejected"))
        self.assertFalse(_is_new_rejection("rejected", "rejected"))
        self.assertFalse(_is_new_rejection("open", "accepted"))

    def test_the_reason_is_logged_with_the_rejection_and_shown_on_the_timeline(self):
        from enquiries import views as enquiry_views
        who = user("rabin", "rabin@avigiri.com")
        request = RequestFactory().post("/")
        request.user = who
        with mock.patch.object(enquiry_views.status_emails, "send_status_email", return_value={}):
            enquiry_views._after_status_change(request, "ENQ-5", {"status": "open"}, "rejected", lost_reason="Price too high")
        log = EnquiryStatusLog.objects.get(enquiry_id="ENQ-5")
        self.assertEqual((log.new_status, log.lost_reason), ("rejected", "Price too high"))
        self.assertEqual(negotiation.timeline("ENQ-5")[0]["detail"], "Reason: Price too high")

    @mock.patch("enquiries.views._get_api_root", return_value="http://svc")
    @mock.patch("enquiries.views.services.fetch_subdivisions", return_value=[])
    @mock.patch("enquiries.views.services.fetch_enquiry", return_value={
        "enquiry_id": "ENQ-1", "status": "open", "priority": "high", "subject": "S", "description": "D",
        "detail_items": [], "attachments": []})
    def test_the_enquiry_page_asks_for_the_reason_when_status_is_rejected(self, *_):
        self.client.force_login(user("rabin", "rabin@avigiri.com"))
        page = self.client.get(reverse("enquiries:detail", args=["ENQ-1"]))
        self.assertContains(page, 'id="lostReasonBox"')
        self.assertContains(page, "Went with competitor")

    @mock.patch("enquiries.views._get_api_root", return_value="http://svc")
    @mock.patch("enquiries.views.services.fetch_subdivisions", return_value=[])
    @mock.patch("enquiries.views.services.fetch_enquiry", return_value={"enquiry_id": "ENQ-1", "status": "open"})
    def test_rejecting_without_a_reason_is_refused_and_with_one_goes_through(self, *_):
        from enquiries import views as enquiry_views

        class FakeForm:
            fields = mock.MagicMock()
            errors = {}

            def __init__(self, *a, **k):
                self.cleaned_data = {"status": "rejected"}

            def is_valid(self):
                return True

            def add_error(self, *a, **k):
                pass

            def cleaned_payload(self):
                return {"status": "rejected"}

        self.client.raise_request_exception = False
        self.client.force_login(user("rabin", "rabin@avigiri.com"))
        url = reverse("enquiries:detail", args=["ENQ-1"])
        with mock.patch.object(enquiry_views.forms, "EnquiryUpdateForm", FakeForm), \
                mock.patch.object(enquiry_views.services, "update_enquiry") as update, \
                mock.patch.object(enquiry_views.status_emails, "send_status_email", return_value={}):
            self.client.post(url, {"status": "rejected"})
            update.assert_not_called()
            response = self.client.post(url, {"status": "rejected", "lost_reason": "No response"})
            update.assert_called_once()
            self.assertEqual(response.status_code, 302)
        log = EnquiryStatusLog.objects.get(enquiry_id="ENQ-1")
        self.assertEqual(log.lost_reason, "No response")

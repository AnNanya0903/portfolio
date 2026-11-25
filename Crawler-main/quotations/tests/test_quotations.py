"""Tests for the quotation and feasibility portal: versions, approval, the tracker and who sees what."""
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from kyc.models import Customer
from quotations import workflow
from quotations.models import Quotation, QuotationEvent, QuotationVersion

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


def pdf(name="quote.pdf", body=b"%PDF-1.4 quote"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def excel(name="working.xlsx"):
    return SimpleUploadedFile(name, b"PK excel", content_type="application/vnd.ms-excel")


@override_settings(**STATIC)
class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Uploaded files go to a temporary folder, not the real uploads.
        cls._dir = tempfile.mkdtemp()
        cls._old = {}
        storage = FileSystemStorage(location=cls._dir)
        for model, names in ((QuotationVersion, ("pdf", "excel")), (QuotationEvent, ("file",))):
            for name in names:
                field = model._meta.get_field(name)
                cls._old[field] = field.storage
                field.storage = storage

    @classmethod
    def tearDownClass(cls):
        for field, storage in cls._old.items():
            field.storage = storage
        shutil.rmtree(cls._dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        users = get_user_model().objects
        self.costing = users.create_user("sagar", password="x")
        self.boss = users.create_user("boss", password="x")
        self.cro = users.create_user("naveen", password="x")
        self._allow(self.costing, ["quotations", "quotation_costing"])
        self._allow(self.boss, ["quotations", "quotation_approval"])
        self._allow(self.cro, ["quotations"])
        self.acme = Customer.objects.create(code="AV-0001", name="Acme Seals")

    def _allow(self, user, services):
        UserServiceAccess.objects.create(user=user, restrict=True, allowed_services=services)

    def _quotation(self, **extra):
        data = dict(customer=self.acme, customer_name="Acme Seals", title="Door seal", pdf=pdf(), excel=excel(),
                    price_note="Rs 42 each", note="first price", user=self.costing)
        data.update(extra)
        return workflow.create_quotation(**data)


class WorkflowTests(Base):
    def test_a_new_quotation_is_numbered_and_waits_for_approval(self):
        quotation = self._quotation()
        self.assertEqual(quotation.number, f"QT-{quotation.pk:05d}")
        self.assertEqual(quotation.status, "pending")
        self.assertIsNone(quotation.active_version)
        self.assertEqual([e.action for e in quotation.events.all()], ["uploaded"])

    def test_approval_makes_the_latest_version_the_pdf_to_send(self):
        quotation = self._quotation()
        version = workflow.approve(quotation, user=self.boss, comment="ok")
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "approved")
        self.assertEqual(quotation.active_version, version)
        self.assertEqual(version.decided_by, self.boss)
        workflow.mark_sent(quotation, user=self.cro)
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "sent")
        self.assertEqual(quotation.events.first().action, "sent")

    def test_revision_then_a_new_version_then_approval(self):
        quotation = self._quotation()
        workflow.request_revision(quotation, user=self.boss, comment="too high")
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "revision")
        with self.assertRaises(workflow.WorkflowError):
            workflow.approve(quotation, user=self.boss)  # nothing is waiting any more
        second = workflow.add_version(quotation, pdf=pdf("v2.pdf"), price_note="Rs 39", user=self.costing)
        self.assertEqual(second.number, 2)
        self.assertEqual(quotation.versions.get(number=1).status, "superseded")
        workflow.approve(quotation, user=self.boss)
        self.assertEqual(Quotation.objects.get(pk=quotation.pk).active_version, second)
        self.assertEqual([e.action for e in quotation.events.order_by("id")],
                         ["uploaded", "revision", "uploaded", "approved"])

    def test_a_corrected_excel_is_tracked_as_price_modified(self):
        quotation = self._quotation()
        workflow.request_revision(quotation, user=self.boss, corrected_excel=excel("boss.xlsx"))
        event = quotation.events.first()
        self.assertEqual((event.action, event.file_name), ("price_modified", "boss.xlsx"))

    def test_a_new_version_after_approval_takes_the_old_pdf_away(self):
        quotation = self._quotation()
        workflow.approve(quotation, user=self.boss)
        workflow.add_version(quotation, pdf=pdf("v2.pdf"), user=self.costing)
        quotation.refresh_from_db()
        self.assertEqual(quotation.status, "pending")
        self.assertIsNone(quotation.active_version)
        with self.assertRaises(workflow.WorkflowError):
            workflow.mark_sent(quotation, user=self.cro)

    def test_a_decision_on_an_older_version_is_refused(self):
        quotation = self._quotation()
        first = quotation.latest_version
        workflow.add_version(quotation, pdf=pdf("v2.pdf"), user=self.costing)
        with self.assertRaises(workflow.WorkflowError):
            workflow.approve(quotation, user=self.boss, version_id=first.pk)


class UploadTests(Base):
    def _post(self, **data):
        payload = {"customer_code": "av-0001", "title": "Door seal", "pdf": pdf(), "excel": excel(),
                   "price_note": "Rs 42", "note": "first"}
        payload.update(data)
        return self.client.post(reverse("quotations:new"), {k: v for k, v in payload.items() if v is not None})

    def test_costing_uploads_a_quotation_for_a_kyc_customer(self):
        self.client.force_login(self.costing)
        response = self._post(enquiry_id="E77")
        quotation = Quotation.objects.get()
        self.assertRedirects(response, reverse("quotations:detail", args=[quotation.pk]))
        self.assertEqual((quotation.customer, quotation.customer_name, quotation.enquiry_id),
                         (self.acme, "Acme Seals", "E77"))
        version = quotation.latest_version
        self.assertEqual((version.pdf_name, version.excel_name, version.uploaded_by),
                         ("quote.pdf", "working.xlsx", self.costing))

    def test_bad_input_is_refused(self):
        self.client.force_login(self.costing)
        for data, message in (
                ({"customer_code": "NOPE"}, "NOPE is not in KYC"),
                ({"customer_code": ""}, "Enter the customer name or pick a customer code"),
                ({"pdf": SimpleUploadedFile("quote.pdf", b"not a pdf")}, "as a PDF file"),
                ({"pdf": pdf("quote.docx")}, "as a PDF file"),
                ({"excel": SimpleUploadedFile("working.exe", b"MZ")}, "upload an Excel file"),
                ({"pdf": None}, "This field is required")):
            self.assertContains(self._post(**data), message)
        self.assertFalse(Quotation.objects.exists())

    def test_a_customer_without_a_code_needs_only_a_name(self):
        self.client.force_login(self.costing)
        self._post(customer_code="", customer_name="  New   Customer ", excel=None)
        quotation = Quotation.objects.get()
        self.assertEqual((quotation.customer, quotation.customer_name), (None, "New Customer"))

    def test_only_costing_may_upload(self):
        for user in (self.cro, self.boss):
            self.client.force_login(user)
            self.assertRedirects(self._post(), reverse("quotations:list"))
            quotation = self._quotation()
            self.client.post(reverse("quotations:version_new", args=[quotation.pk]), {"pdf": pdf("v2.pdf")})
            self.assertEqual(quotation.versions.count(), 1)
        self.assertEqual(Quotation.objects.count(), 2)

    def test_costing_uploads_a_new_version(self):
        quotation = self._quotation()
        self.client.force_login(self.costing)
        self.client.post(reverse("quotations:version_new", args=[quotation.pk]),
                         {"pdf": pdf("v2.pdf"), "price_note": "Rs 39"})
        self.assertEqual(quotation.latest_version.price_note, "Rs 39")
        self.assertEqual(quotation.versions.count(), 2)


class DecisionTests(Base):
    def setUp(self):
        super().setUp()
        self.quotation = self._quotation()
        self.url = reverse("quotations:decide", args=[self.quotation.pk])

    def _decide(self, user, **data):
        self.client.force_login(user)
        response = self.client.post(self.url, data, follow=True)
        self.quotation.refresh_from_db()
        return response

    def test_management_approves(self):
        response = self._decide(self.boss, decision="approve", version=self.quotation.latest_version.pk)
        self.assertEqual(self.quotation.status, "approved")
        self.assertContains(response, "approved. It is now the PDF to send")

    def test_a_revision_needs_a_comment_or_a_corrected_excel(self):
        response = self._decide(self.boss, decision="revise")
        self.assertContains(response, "Say what should change")
        self.assertEqual(self.quotation.status, "pending")
        self._decide(self.boss, decision="revise", comment="Reduce by 5%")
        self.assertEqual(self.quotation.status, "revision")
        self.assertEqual(self.quotation.latest_version.decision_comment, "Reduce by 5%")

    def test_costing_and_customer_relations_cannot_decide(self):
        for user in (self.costing, self.cro):
            self._decide(user, decision="approve")
            self.assertEqual(self.quotation.status, "pending")

    def test_a_stale_decision_is_refused(self):
        first = self.quotation.latest_version
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing)
        response = self._decide(self.boss, decision="approve", version=first.pk)
        self.assertContains(response, "A newer version was uploaded")
        self.assertEqual(self.quotation.status, "pending")

    def test_tracker_times_are_indian_time(self):
        from datetime import datetime, timezone as dt_timezone
        self.quotation.events.update(created_at=datetime(2026, 11, 25, 10, 39, tzinfo=dt_timezone.utc))
        self.client.force_login(self.boss)
        response = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]))
        self.assertContains(response, "25 Nov 2026, 16:09")  # 10:39 UTC
        self.assertNotContains(response, "25 Nov 2026, 10:39")


class VisibilityTests(Base):
    def setUp(self):
        super().setUp()
        self.quotation = self._quotation()
        self.first = self.quotation.latest_version

    def _get(self, user, name, *args):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args))

    def test_customer_relations_sees_no_pdf_and_no_workings_before_approval(self):
        page = self._get(self.cro, "quotations:detail", self.quotation.pk)
        self.assertContains(page, "data-no-active-pdf")
        for hidden in ("data-tracker", "data-decision-card", "data-upload-card", "Rs 42 each", "first price",
                       "working.xlsx"):
            self.assertNotContains(page, hidden)
        self.assertEqual(self._get(self.cro, "quotations:version_pdf", self.first.pk).status_code, 404)
        self.assertEqual(self._get(self.cro, "quotations:version_excel", self.first.pk).status_code, 404)

    def test_customer_relations_gets_only_the_approved_pdf(self):
        workflow.approve(self.quotation, user=self.boss, comment="margin is fine")
        page = self._get(self.cro, "quotations:detail", self.quotation.pk)
        self.assertContains(page, "data-active-pdf")
        self.assertNotContains(page, "margin is fine")
        response = self._get(self.cro, "quotations:version_pdf", self.first.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 quote")
        self.assertEqual(self._get(self.cro, "quotations:version_excel", self.first.pk).status_code, 404)
        # A newer version takes the old PDF away again.
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing)
        self.assertEqual(self._get(self.cro, "quotations:version_pdf", self.first.pk).status_code, 404)

    def test_costing_and_management_see_the_workings(self):
        workflow.request_revision(self.quotation, user=self.boss, comment="too high",
                                  corrected_excel=excel("boss.xlsx"))
        event = self.quotation.events.first()
        for user in (self.costing, self.boss):
            page = self._get(user, "quotations:detail", self.quotation.pk)
            for shown in ("data-tracker", "Rs 42 each", "too high", "boss.xlsx",
                          "Price has been modified by management"):
                self.assertContains(page, shown)
            self.assertEqual(self._get(user, "quotations:version_excel", self.first.pk).status_code, 200)
            self.assertEqual(self._get(user, "quotations:event_file", event.pk).status_code, 200)
        self.assertEqual(self._get(self.cro, "quotations:event_file", event.pk).status_code, 404)

    def test_the_decision_card_is_only_for_management_and_upload_only_for_costing(self):
        boss_page = self._get(self.boss, "quotations:detail", self.quotation.pk)
        self.assertContains(boss_page, "data-decision-card")
        self.assertNotContains(boss_page, "data-upload-card")
        costing_page = self._get(self.costing, "quotations:detail", self.quotation.pk)
        self.assertContains(costing_page, "data-upload-card")
        self.assertNotContains(costing_page, "data-decision-card")

    def test_the_portal_needs_the_quotations_module(self):
        outsider = get_user_model().objects.create_user("other", password="x")
        self._allow(outsider, ["kyc"])
        self.client.force_login(outsider)
        for name, args in (("quotations:list", []), ("quotations:detail", [self.quotation.pk]),
                           ("quotations:version_pdf", [self.first.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("quotations:list")).status_code, 302)

    def test_top_management_opens_the_pdf_and_decides_even_when_the_module_is_not_ticked(self):
        from kyc.departments import seed_departments
        from kyc.models import Department, UserDepartment
        seed_departments()
        md = get_user_model().objects.create_user("md", password="x")
        self._allow(md, ["kyc"])  # restricted, and Quotations is not among their sections
        self.client.force_login(md)
        self.assertEqual(self.client.get(reverse("quotations:list")).status_code, 302)
        UserDepartment.objects.create(user=md, department=Department.objects.get(name="Top Management"))
        self.assertEqual(self.client.get(reverse("quotations:list")).status_code, 200)
        self.assertContains(self.client.get(reverse("kyc:list")), 'href="/quotations/"')
        page = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]))
        self.assertContains(page, "data-decision-card")
        self.assertContains(page, reverse("quotations:version_pdf", args=[self.first.pk]))
        self.assertEqual(self.client.get(reverse("quotations:version_pdf", args=[self.first.pk])).status_code, 200)
        self.client.post(reverse("quotations:decide", args=[self.quotation.pk]), {"decision": "approve"})
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "approved")

    def test_mark_as_sent(self):
        self.client.force_login(self.cro)
        url = reverse("quotations:mark_sent", args=[self.quotation.pk])
        self.assertContains(self.client.post(url, follow=True), "no approved PDF to send yet")
        workflow.approve(self.quotation, user=self.boss)
        self.client.post(url)
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "sent")


class ListTests(Base):
    def test_search_tabs_and_customer_filter(self):
        first = self._quotation()
        second = self._quotation(customer=None, customer_name="Bolt Works", title="Oil seal", enquiry_id="E9")
        workflow.approve(second, user=self.boss)
        self.client.force_login(self.cro)
        url = reverse("quotations:list")

        def numbers(**params):
            return {row.number for row in self.client.get(url, params).context["page"]}

        self.assertEqual(numbers(), {first.number, second.number})
        self.assertEqual(numbers(status="pending"), {first.number})
        self.assertEqual(numbers(status="approved"), {second.number})
        self.assertEqual(numbers(q="bolt"), {second.number})
        self.assertEqual(numbers(q="E9"), {second.number})
        self.assertEqual(numbers(q="av-0001"), {first.number})
        self.assertEqual(numbers(customer="av-0001"), {first.number})
        page = self.client.get(url)
        self.assertNotContains(page, "Upload quotation")
        self.client.force_login(self.costing)
        self.assertContains(self.client.get(url), "Upload quotation")


class DeleteTests(Base):
    """Deleting a quotation: only costing users, the number must be typed, and the stored files go too."""

    def test_costing_deletes_it_with_its_versions_and_files(self):
        quotation = self._quotation()
        version = quotation.versions.get()
        stored = [version.pdf.name, version.excel.name]
        self.client.force_login(self.costing)
        response = self.client.post(reverse("quotations:delete", args=[quotation.pk]),
                                    {"confirm_number": f" {quotation.number.lower()} "})
        self.assertRedirects(response, reverse("quotations:list"))
        self.assertFalse(Quotation.objects.filter(pk=quotation.pk).exists())
        self.assertFalse(QuotationVersion.objects.filter(quotation_id=quotation.pk).exists())
        self.assertFalse(QuotationEvent.objects.filter(quotation_id=quotation.pk).exists())
        for name in stored:
            self.assertFalse(default_storage_exists(name), name)

    def test_the_number_must_be_typed(self):
        quotation = self._quotation()
        self.client.force_login(self.costing)
        self.client.post(reverse("quotations:delete", args=[quotation.pk]), {"confirm_number": "QT-99999"})
        self.client.post(reverse("quotations:delete", args=[quotation.pk]), {})
        self.assertTrue(Quotation.objects.filter(pk=quotation.pk).exists())

    def test_only_costing_users_may_delete(self):
        quotation = self._quotation()
        url = reverse("quotations:delete", args=[quotation.pk])
        for user in (self.boss, self.cro):
            self.client.force_login(user)
            self.client.post(url, {"confirm_number": quotation.number})
        self.assertTrue(Quotation.objects.filter(pk=quotation.pk).exists())

    def test_get_does_not_delete_and_the_card_shows_only_to_costing(self):
        quotation = self._quotation()
        self.client.force_login(self.costing)
        self.assertEqual(self.client.get(reverse("quotations:delete", args=[quotation.pk])).status_code, 405)
        self.assertContains(self.client.get(reverse("quotations:detail", args=[quotation.pk])), "data-delete-quotation")
        self.client.force_login(self.cro)
        self.assertNotContains(self.client.get(reverse("quotations:detail", args=[quotation.pk])), "data-delete-quotation")


def default_storage_exists(name):
    from django.core.files.storage import default_storage  # noqa: F401  (the upload storage is replaced in tests)
    from docscanner.storage import document_scan_storage
    return document_scan_storage.exists(name)

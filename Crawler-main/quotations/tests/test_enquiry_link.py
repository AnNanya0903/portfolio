"""Quotations linked to real enquiries (ENQ-NNNN), revisions as new versions, and the CRO's send of the approved
version (share link tied to the version, password in a separate email, follow-ups restarted)."""
import importlib
from io import StringIO
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess

from enquiries import follow_up_links
from enquiries.models import EnquiryContact, FollowUpStart, ShareLink
from enquiries.services import EnquiryServiceError
from quotations import enquiry_link, links, mailer, sending, share_links, workflow
from quotations.models import EnquiryLinkSuggestion, Quotation, QuotationEvent
from quotations.tests.test_quotations import Base, pdf
from quotations.tests.test_send_flow import ENQ, SendBase

ENQUIRIES = {
    "ENQ-77": {"enquiry_id": "ENQ-77", "customer_name": "Acme Seals", "subject": "Need door seals"},
    "ENQ-78": {"enquiry_id": "ENQ-78", "customer_name": "Acme Seals", "subject": "Window gaskets"},
}


def fake_fetch(api_root, enquiry_id, request=None):
    if enquiry_id in ENQUIRIES:
        return dict(ENQUIRIES[enquiry_id])
    raise EnquiryServiceError("Service returned 404: not found")


class LinkingTests(Base):
    """Part 1: one enquiry has many quotations; a quotation has one (checked) enquiry, or none."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("enquiries.services.fetch_enquiry", side_effect=fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _upload(self, **data):
        self.client.force_login(self.costing)
        payload = {"customer_name": "Acme Seals", "title": "Door seal", "pdf": pdf()}
        payload.update(data)
        return self.client.post(reverse("quotations:new"), payload)

    def test_a_quotation_saves_with_and_without_an_enquiry(self):
        self._upload(title="No enquiry")
        self._upload(title="With enquiry", enquiry_id="enq-77", enquiry_reference="AG-QR-ALSTOM-235")
        without, linked = Quotation.objects.get(title="No enquiry"), Quotation.objects.get(title="With enquiry")
        self.assertEqual((without.enquiry_id, without.enquiry_reference), ("", ""))
        self.assertEqual((linked.enquiry_id, linked.enquiry_reference), ("ENQ-77", "AG-QR-ALSTOM-235"))

    def test_the_enquiry_lists_all_its_quotations(self):
        first = self._quotation(enquiry_id="ENQ-77", title="Door seal")
        second = self._quotation(enquiry_id="ENQ-77", title="Another item")  # another item: a new QT, same ENQ
        self._quotation(enquiry_id="ENQ-78", title="Other enquiry")
        rows = links.summaries_for_enquiries(["ENQ-77"])["ENQ-77"]
        self.assertEqual({r["number"] for r in rows}, {first.number, second.number})
        self.assertTrue(all(r["version"] == 1 and r["status"] == "pending" for r in rows))
        self.assertEqual(rows[0]["label"], "Waiting for management approval (v1)")

    def test_an_unknown_or_made_up_enquiry_is_refused(self):
        response = self._upload(enquiry_id="ENQ-999")
        self.assertContains(response, "There is no enquiry ENQ-999.")
        response = self._upload(enquiry_id="AG-QR-ALSTOM-235")
        self.assertContains(response, "is not an enquiry number")
        self.assertFalse(Quotation.objects.exists())

    def test_the_enquiry_service_being_down_is_said_clearly(self):
        with mock.patch("enquiries.services.fetch_enquiry", side_effect=EnquiryServiceError("timed out")):
            response = self._upload(enquiry_id="ENQ-77")
        self.assertContains(response, "could not be reached")
        self.assertFalse(Quotation.objects.exists())

    def test_the_picker_value_with_customer_and_subject_keeps_only_the_number(self):
        self._upload(enquiry_id="ENQ-77 · Acme Seals · Need door seals")
        self.assertEqual(Quotation.objects.get().enquiry_id, "ENQ-77")

    def test_the_enquiry_is_preselected_from_the_link(self):
        self.client.force_login(self.costing)
        page = self.client.get(reverse("quotations:new") + "?enquiry=enq-77")
        self.assertContains(page, 'value="ENQ-77"')
        self.assertContains(page, 'id="enquiryOptions"')

    def test_enquiry_search_for_the_picker(self):
        rows = [{"enquiry_id": "ENQ-77", "customer_name": "Acme Seals", "subject": "Need door seals"},
                {"enquiry_id": "ENQ-80", "customer_name": "Other Co", "subject": "Hoses"}]
        self.client.force_login(self.costing)
        with mock.patch("enquiries.services.list_enquiries", return_value=rows):
            data = self.client.get(reverse("quotations:enquiry_search"), {"q": "acme door"}).json()
        self.assertEqual([e["enquiry_id"] for e in data["enquiries"]], ["ENQ-77"])
        self.assertEqual(data["enquiries"][0]["label"], "ENQ-77 · Acme Seals · Need door seals")

    def test_the_cro_changes_the_enquiry_and_the_tracker_says_who_and_old_to_new(self):
        quotation = self._quotation(enquiry_id="ENQ-77")
        self.client.force_login(self.costing)
        url = reverse("quotations:change_enquiry", args=[quotation.pk])
        self.assertEqual(self.client.post(url, {"enquiry_id": "ENQ-78"}).status_code, 403)
        self.client.force_login(self.cro)
        self.client.post(url, {"enquiry_id": "ENQ-78"})
        quotation.refresh_from_db()
        self.assertEqual(quotation.enquiry_id, "ENQ-78")
        event = quotation.events.get(action="enquiry_changed")
        self.assertEqual((event.user, event.details["old"], event.details["new"]), (self.cro, "ENQ-77", "ENQ-78"))
        self.assertEqual(event.comment, "ENQ-77 → ENQ-78")
        # The quotation's whole history now shows on the new enquiry.
        self.assertFalse(QuotationEvent.objects.filter(quotation=quotation, enquiry_id="ENQ-77").exists())
        self.client.post(url, {"enquiry_id": "ENQ-999"})
        self.assertEqual(Quotation.objects.get(pk=quotation.pk).enquiry_id, "ENQ-78")

    def test_list_and_detail_show_the_enquiry_link_and_the_reference(self):
        quotation = self._quotation(enquiry_id="ENQ-77", enquiry_reference="AG-QR-ALSTOM-235")
        UserServiceAccess.objects.filter(user=self.cro).update(
            allowed_services=["quotations", "quotation_send", "enquiries"])
        self.client.force_login(self.cro)
        enquiry_url = reverse("enquiries:detail", args=["ENQ-77"])
        for page in (self.client.get(reverse("quotations:list")),
                     self.client.get(reverse("quotations:detail", args=[quotation.pk]))):
            self.assertContains(page, f'href="{enquiry_url}"')
            self.assertContains(page, "Ref. AG-QR-ALSTOM-235")
        self.assertContains(self.client.get(reverse("quotations:detail", args=[quotation.pk])), "data-change-enquiry")


class MigrationTests(Base):
    def test_free_text_moves_to_the_reference_and_enq_numbers_stay(self):
        text = Quotation.objects.create(customer_name="Alstom", title="Seal", enquiry_id="AG-QR-ALSTOM-235")
        real = Quotation.objects.create(customer_name="Acme", title="Seal", enquiry_id="enq-0012")
        QuotationEvent.objects.create(quotation=text, action="uploaded")
        migration = importlib.import_module("quotations.migrations.0004_move_enquiry_text_to_reference")
        migration.forwards(django_apps, None)
        migration.forwards(django_apps, None)  # safe to run again
        text.refresh_from_db()
        real.refresh_from_db()
        self.assertEqual((text.enquiry_id, text.enquiry_reference), ("", "AG-QR-ALSTOM-235"))
        self.assertEqual((real.enquiry_id, real.enquiry_reference), ("ENQ-0012", ""))
        self.assertEqual(QuotationEvent.objects.get(quotation=text).enquiry_id, "")


class SuggestionTests(Base):
    """Part 1.4: suggestions for older quotations; nothing is linked without a review."""

    def setUp(self):
        super().setUp()
        self.old = Quotation.objects.create(customer_name="Alstom Transport India Pvt Ltd",
                                            title="Re: Fwd: Door seal gasket 12/11/2026",
                                            enquiry_reference="AG-QR-ALSTOM-235")
        self.enquiries = [
            {"enquiry_id": "ENQ-10", "customer_name": "Alstom Transport", "subject": "Door seal gasket",
             "received_at": "2025-01-01T00:00:00+00:00"},
            {"enquiry_id": "ENQ-11", "customer_name": "Alstom Transport", "subject": "RE: door seal gasket!",
             "received_at": self.old.created_at.isoformat()},
            {"enquiry_id": "ENQ-12", "customer_name": "Other Company", "subject": "Door seal gasket",
             "received_at": self.old.created_at.isoformat()},
            {"enquiry_id": "ENQ-13", "customer_name": "Alstom Transport", "subject": "Hoses",
             "received_at": self.old.created_at.isoformat()},
        ]

    def test_cleaning(self):
        self.assertEqual(enquiry_link.clean_subject("RE: Fw: AG-QR-ALSTOM-235 Door seal, 12/11/2026 (urgent)"),
                         "door seal urgent")
        self.assertEqual(enquiry_link.clean_subject("Fwd: AG-RQ-7 gasket 3rd Nov 2026"), "gasket")
        self.assertEqual(enquiry_link.clean_customer("M/s. Acme Seals Pvt. Ltd."), "acme seals")

    def test_same_customer_and_subject_closest_date_wins(self):
        result = enquiry_link.suggest_for(self.old, self.enquiries, {})
        self.assertEqual(result.enquiry["enquiry_id"], "ENQ-11")  # not ENQ-12 (other customer) or ENQ-13
        self.assertIn("2 enquiries matched", result.reason)
        # Same customer and subject would be high, but two enquiries match as well: only the date decided.
        self.assertEqual(result.confidence, "medium")

    def test_same_email_domain_counts_as_the_same_customer(self):
        from kyc.models import CustomerContact

        customer = self.acme
        CustomerContact.objects.create(customer=customer, name="Buyer", email="buyer@alstomgroup.com")
        quotation = Quotation.objects.create(customer=customer, customer_name="Acme Seals", title="Hoses")
        result = enquiry_link.suggest_for(quotation, self.enquiries, {"ENQ-13": ["ravi@alstomgroup.com"]})
        self.assertEqual(result.enquiry["enquiry_id"], "ENQ-13")
        self.assertIn("same email domain", result.reason)

    def test_the_command_lists_suggestions_and_links_nothing(self):
        linked = self._quotation(enquiry_id="ENQ-77")
        out = StringIO()
        with mock.patch("enquiries.services.list_enquiries", return_value=self.enquiries):
            call_command("suggest_quotation_enquiries", stdout=out)
            call_command("suggest_quotation_enquiries", stdout=StringIO())  # safe to run again
        self.assertIn(self.old.number, out.getvalue())
        self.assertIn("ENQ-11", out.getvalue())
        self.assertIn("Nothing was linked", out.getvalue())
        self.assertEqual(Quotation.objects.get(pk=self.old.pk).enquiry_id, "")
        self.assertEqual(EnquiryLinkSuggestion.objects.count(), 1)
        self.assertFalse(EnquiryLinkSuggestion.objects.filter(quotation=linked).exists())

    def _suggest(self):
        with mock.patch("enquiries.services.list_enquiries", return_value=self.enquiries):
            call_command("suggest_quotation_enquiries", stdout=StringIO())
        return EnquiryLinkSuggestion.objects.get(quotation=self.old)

    def test_only_an_administrator_reviews_and_confirming_links_with_a_tracker_line(self):
        suggestion = self._suggest()
        url = reverse("quotations:enquiry_suggestions")
        self.client.force_login(self.cro)
        self.assertEqual(self.client.get(url).status_code, 302)
        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(admin)
        self.assertContains(self.client.get(url), self.old.number)
        with mock.patch("enquiries.services.fetch_enquiry", return_value={"enquiry_id": "ENQ-11"}):
            self.client.post(url, {"suggestion": suggestion.pk, "action": "confirm"})
        self.old.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual((self.old.enquiry_id, suggestion.status, suggestion.decided_by), ("ENQ-11", "confirmed", admin))
        self.assertTrue(self.old.events.filter(action="enquiry_changed", details__new="ENQ-11").exists())

    def test_the_administrator_can_pick_another_enquiry_or_reject(self):
        suggestion = self._suggest()
        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "x")
        self.client.force_login(admin)
        url = reverse("quotations:enquiry_suggestions")
        self.client.post(url, {"suggestion": suggestion.pk, "action": "reject"})
        self.assertEqual(EnquiryLinkSuggestion.objects.get(pk=suggestion.pk).status, "rejected")
        self.assertEqual(self._suggest().status, "rejected")  # a re-run does not bring back the same answer
        with mock.patch("enquiries.services.fetch_enquiry", return_value={"enquiry_id": "ENQ-10"}):
            self.client.post(url, {"suggestion": suggestion.pk, "action": "pick", "enquiry_id": "ENQ-10"})
        self.assertEqual(Quotation.objects.get(pk=self.old.pk).enquiry_id, "ENQ-10")


class RevisionAndEnquiryTests(Base):
    """Part 2: a revision is a new version of the same quotation on the same enquiry."""

    def setUp(self):
        super().setUp()
        self.quotation = self._quotation(enquiry_id="ENQ-77")
        self.admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "x")

    def test_a_revision_is_a_new_version_of_the_same_quotation_and_enquiry(self):
        workflow.request_revision(self.quotation, user=self.boss, comment="Too high")
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing)
        self.assertEqual(Quotation.objects.count(), 1)
        self.quotation.refresh_from_db()
        self.assertEqual((self.quotation.enquiry_id, self.quotation.latest_version.number), ("ENQ-77", 2))

    def test_an_enquiry_with_a_quotation_cannot_be_deleted(self):
        self.client.force_login(self.admin)
        with mock.patch("enquiries.views.services.delete_enquiry") as delete:
            response = self.client.post(reverse("enquiries:delete_enquiry", args=["ENQ-77"]))
            self.assertEqual(response.status_code, 409)
            self.assertIn(self.quotation.number, response.json()["error"])
            delete.assert_not_called()
            self.client.post(reverse("enquiries:delete_enquiry", args=["ENQ-55"]))
            delete.assert_called_once()

    def test_only_an_administrator_merges_a_duplicate(self):
        EnquiryContact.objects.create(enquiry_id="ENQ-77", customer_email="buyer@acme.example",
                                      gmail_thread_id="T1", email_message_id="<c1@acme>", email_subject="Seals")
        EnquiryContact.objects.create(enquiry_id="ENQ-78", customer_email="other@acme.example")
        url = reverse("enquiries:merge", args=["ENQ-77"])
        self.client.force_login(self.cro)
        self.client.post(url, {"keep": "ENQ-78"})
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).enquiry_id, "ENQ-77")
        self.client.force_login(self.admin)
        with mock.patch("enquiries.services.fetch_enquiry", side_effect=fake_fetch), \
                mock.patch("enquiries.services.delete_enquiry") as delete:
            self.client.post(url, {"keep": "ENQ-78"})
        delete.assert_called_once()
        self.assertEqual(delete.call_args[0][1], "ENQ-77")
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.enquiry_id, "ENQ-78")
        self.assertFalse(QuotationEvent.objects.filter(enquiry_id="ENQ-77").exists())
        kept = EnquiryContact.objects.get(enquiry_id="ENQ-78")
        self.assertEqual((kept.customer_email, kept.gmail_thread_id), ("other@acme.example", "T1"))
        self.assertIn("buyer@acme.example", kept.other_customer_emails)
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="ENQ-77").exists())
        self.assertTrue(QuotationEvent.objects.filter(enquiry_id="ENQ-78", action="enquiry_merged").exists())

    def _link(self, version):
        q = Quotation.objects.get(pk=self.quotation.pk)
        _, data = workflow.approved_pdf(q)
        return share_links.create_for_version(q, version, data, base_url="https://portal.example")

    def test_approving_a_new_version_revokes_the_old_links(self):
        workflow.approve(self.quotation, user=self.boss)
        v1 = self.quotation.versions.get(number=1)
        record, url, password = self._link(v1)
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf", b"%PDF-1.4 v2"), user=self.costing)
        self.assertIsNone(ShareLink.objects.get(pk=record.pk).revoked_at)  # v2 not approved yet: v1 still stands
        workflow.approve(self.quotation, user=self.boss)
        self.assertIsNotNone(ShareLink.objects.get(pk=record.pk).revoked_at)
        self.assertTrue(self.quotation.events.filter(action="links_revoked").exists())
        page = self.client.get(url.replace("https://portal.example", ""))
        self.assertEqual(page.status_code, 410)
        self.assertContains(page, "This quotation has been replaced.", status_code=410)
        self.assertContains(page, "Please use the latest link sent to you.", status_code=410)
        answer = self.client.post(reverse("enquiries:share_action"), {"token": record.token, "action": "accept"})
        self.assertEqual(answer.status_code, 410)
        # Follow-ups never repeat a replaced link.
        self.assertIsNone(follow_up_links.latest_revision("ENQ-77"))

    def test_a_link_never_saved_is_also_replaced(self):
        workflow.approve(self.quotation, user=self.boss)
        v1 = self.quotation.versions.get(number=1)
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing)
        workflow.approve(self.quotation, user=self.boss)
        self.assertTrue(share_links.is_replaced(None, {"quotation_version_id": v1.pk}))
        self.assertFalse(share_links.is_replaced(None, {"quotation_version_id": self.quotation.latest_version.pk}))
        self.assertFalse(share_links.is_replaced(None, {"enquiry_id": "ENQ-77"}))  # older links: unchanged

    def test_the_enquiry_label_and_version_history(self):
        workflow.approve(self.quotation, user=self.boss)
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf"), user=self.costing)
        self.assertEqual(links.summaries_for_enquiries(["ENQ-77"])["ENQ-77"][0]["label"],
                         "Waiting for management approval (v2)")
        workflow.request_revision(self.quotation, user=self.boss, comment="Discount for 5,000 pieces")
        workflow.add_version(self.quotation, pdf=pdf("v3.pdf"), user=self.costing)
        workflow.approve(self.quotation, user=self.boss)
        self.assertEqual(links.summaries_for_enquiries(["ENQ-77"])["ENQ-77"][0]["label"],
                         "Revised quotation ready to send (v3)")
        history = {row["version"].number: row for row in links.version_history("ENQ-77")}
        self.assertEqual(history[1]["approved_by"], self.boss)
        self.assertEqual(history[2]["revision_reason"], "Discount for 5,000 pieces")
        self.assertIsNotNone(history[3]["approved_at"])


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class GenerateLinkTests(Base):
    """Part 2.3: Generate Link always shares the latest APPROVED version."""

    ENQUIRY = {"enquiry_id": "ENQ-77", "subject": "Need door seals", "customer_name": "Acme Seals",
               "general_attachments": [{"attachment_id": "a1", "file_name": "rfq.pdf",
                                        "content_type": "application/pdf"}]}

    def setUp(self):
        super().setUp()
        self.quotation = self._quotation(enquiry_id="ENQ-77")
        self.client.force_login(get_user_model().objects.create_superuser("sales", "s@example.com", "x"))
        patcher = mock.patch("enquiries.views.services.fetch_enquiry", return_value=dict(self.ENQUIRY))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _generate(self, **data):
        payload = {"mode": "update", "enquiry_id": "ENQ-77", "subject": "Need door seals", "customer_name": "Acme"}
        payload.update(data)
        with mock.patch("enquiries.views.services.download_attachment") as download:
            response = self.client.post(reverse("enquiries:generate_link"), payload)
        download.assert_not_called()
        return response

    def test_waiting_for_approval_blocks_the_link(self):
        page = self.client.get(reverse("enquiries:preview", args=["ENQ-77"]))
        self.assertContains(page, "Waiting for management approval – do not send an older version.")
        self.assertNotContains(page, 'name="share_pdf"')
        self._generate()
        self.assertFalse(ShareLink.objects.exists())

    def test_the_approved_version_is_shared_and_tied_to_the_link(self):
        workflow.approve(self.quotation, user=self.boss)
        self.assertEqual(self._generate().status_code, 200)
        record = ShareLink.objects.get()
        version = self.quotation.latest_version
        self.assertEqual(record.quotation_version_id, version.pk)
        data = signing.loads(record.token, salt="enquiry-share")
        self.assertEqual((data["pdf_filename"], data["quotation_version"]), (f"{self.quotation.number}.pdf", 1))

    def test_with_several_quotations_the_cro_chooses(self):
        workflow.approve(self.quotation, user=self.boss)
        other = self._quotation(enquiry_id="ENQ-77", title="Window gasket")
        workflow.approve(other, user=self.boss)
        self.assertContains(self._generate(), "Choose which quotation to share.")
        self.assertFalse(ShareLink.objects.exists())
        self._generate(share_quotation=str(other.pk))
        self.assertEqual(ShareLink.objects.get().quotation_version_id, other.latest_version.pk)


class NotifyTests(SendBase):
    """Part 3.1: on approval the CRO is told the version is ready to send."""

    @override_settings(QUOTATION_READY_NOTIFY_DEPARTMENTS="")
    def test_approval_notifies_the_cro(self):
        self.client.force_login(self.boss)
        with mock.patch("push_notifications.services.send_push_to_users") as push:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("quotations:decide", args=[self.quotation.pk]), {"decision": "approve"})
        push.assert_called_once()
        self.assertEqual(list(push.call_args[0][0]), [self.cro])
        self.assertEqual(push.call_args[1]["body"],
                         f"{self.quotation.number} ({ENQ}, Acme Seals) v1 approved – ready to send")
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).get_status_display(), "Approved, OK to send")


class CroSendTests(SendBase):
    """Part 3: the CRO sends the latest approved version."""

    def _bodies(self, message):
        return message.get_body(("plain",)).get_content()

    def test_the_ready_to_send_list_and_the_enquiry_banner(self):
        self._approve()
        self.client.force_login(self.cro)
        page = self.client.get(reverse("quotations:list"), {"status": "approved"})
        self.assertContains(page, "data-ready-to-send")
        self.assertContains(page, f'{reverse("quotations:detail", args=[self.quotation.pk])}?send=1')
        detail = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]), {"send": "1"})
        self.assertContains(detail, "getOrCreateInstance(document.getElementById('sendModal'))")
        self.assertEqual([q.pk for q in links.ready_to_send(ENQ)], [self.quotation.pk])

    def test_without_an_enquiry_the_cro_picks_one_first(self):
        Quotation.objects.filter(pk=self.quotation.pk).update(enquiry_id="")
        self._approve()
        self.client.force_login(self.cro)
        page = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]))
        self.assertContains(page, "data-needs-enquiry")
        self.assertNotContains(page, "data-send-modal")
        response = self._post(self.cro, "quotations:send")
        self.assertContains(response, "Link this quotation to its enquiry first", status_code=400)
        self.assertEqual(self.gmail.sent, [])

    def test_the_link_is_in_the_email_and_the_password_follows_separately(self):
        self._approve()
        self._link()
        with override_settings(FOLLOW_UP_AUTO_ENABLED=False):  # manual send works with automatic emails off
            self._post(self.cro, "quotations:send", body=sending.default_body(self.quotation,
                                                                              self.quotation.active_version))
        self.assertEqual(len(self.gmail.sent), 2)
        record = ShareLink.objects.get()
        self.assertEqual(record.quotation_version_id, self.quotation.active_version.pk)
        quotation_email, password_email = self.gmail.quotation, self.gmail.last
        body = self._bodies(quotation_email)
        self.assertIn(f"/enquiries/share/magicwand/{ENQ}/{record.id}/", body)
        self.assertNotIn(sending.LINK_PLACEHOLDER, body)
        self.assertIn(sending.PASSWORD_NOTE, body)
        password = self._bodies(password_email).split("is:\n\n", 1)[1].split("\n", 1)[0].strip()
        self.assertTrue(password.startswith("AG-"))
        self.assertNotIn(password, body)  # never in the same email as the link
        self.assertNotIn(record.id, self._bodies(password_email))
        data = signing.loads(record.token, salt="enquiry-share")
        from django.contrib.auth.hashers import check_password

        self.assertTrue(check_password(password, data["password_hash"]))
        # The password email is a reply in the same thread, to the quotation email.
        self.assertEqual(self.gmail.sent[-1][0]["threadId"], "T1")
        self.assertEqual(password_email["In-Reply-To"], quotation_email["Message-ID"])
        self.assertEqual(password_email["Subject"], "Re: Need door seals")
        # The tracker: sender, recipients, Gmail ids; the password itself is never in the tracker.
        sent = self.quotation.events.get(action="sent")
        self.assertEqual((sent.user, sent.details["gmail_id"], sent.details["version"]), (self.cro, "g1", 1))
        self.assertFalse(QuotationEvent.objects.filter(comment__contains=password).exists())
        start = FollowUpStart.objects.get(enquiry_id=ENQ)
        self.assertEqual((start.share_url.endswith(f"/{record.id}/"), start.share_password), (True, password))

    def test_recipients_default_to_email_1_with_email_2_and_ccs_in_cc(self):
        self._approve()
        self._contact(customer_email="buyer@customer.com", other_customer_emails="second@customer.com",
                      follow_up_cc="rabin@avigiri.com, customerservice@avigiri.com")
        draft = sending.preview(self.quotation)
        self.assertEqual(draft.to, ["buyer@customer.com"])
        self.assertEqual(draft.cc, ["second@customer.com", "rabin@avigiri.com"])  # never our own mailbox

    def test_a_revised_version_restarts_the_follow_ups_and_says_revised_on_a_new_thread(self):
        self._approve()
        self._post(self.cro, "quotations:send")
        self.assertEqual(FollowUpStart.objects.filter(enquiry_id=ENQ).count(), 1)
        workflow.add_version(self.quotation, pdf=pdf("v2.pdf", b"%PDF-1.4 v2"), user=self.costing)
        self._approve()
        first_link = ShareLink.objects.order_by("created_at").first()
        self.assertIsNotNone(first_link.revoked_at)  # v1's link was replaced on approval of v2
        # The first email started the thread, so the revision replies in it with the same subject.
        draft = sending.preview(self.quotation)
        self.assertEqual((draft.subject, draft.subject_locked), ("Re: Need door seals", True))
        self.assertIn("revised quotation", draft.body)
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.status, "approved")
        self._post(self.cro, "quotations:send")
        self.assertEqual(FollowUpStart.objects.filter(enquiry_id=ENQ).count(), 2)  # restarted from this email
        self.assertEqual(self.quotation.events.filter(action="sent").first().details["version"], 2)
        label = links.summaries_for_enquiries([ENQ])[ENQ][0]["label"]
        self.assertTrue(label.startswith("Quotation sent ") and label.endswith("(v2)"), label)

    def test_the_new_thread_subject_says_revised_from_version_2(self):
        self._approve()
        version = self.quotation.active_version
        self.assertEqual(sending.new_thread_subject(self.quotation, version, "Re: Need door seals"),
                         "Re: Need door seals")
        version.number = 2
        self.assertEqual(sending.new_thread_subject(self.quotation, version, "Need door seals"),
                         "Re: Need door seals (Revised)")

    def test_a_failed_password_email_keeps_the_send_and_can_be_retried(self):
        self._approve()
        self._link()
        real = mailer.send_email
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise mailer.MailerError("Gmail quota")
            return real(*args, **kwargs)

        with mock.patch("quotations.mailer.send_email", side_effect=flaky):
            response = self._post(self.cro, "quotations:send")
        self.assertEqual(response.status_code, 302)
        self.quotation.refresh_from_db()
        self.assertEqual(self.quotation.status, "sent")
        self.assertTrue(self.quotation.events.filter(action="password_failed").exists())
        page = self.client.get(reverse("quotations:detail", args=[self.quotation.pk]))
        self.assertContains(page, "data-password-retry")
        token_before = ShareLink.objects.get().token
        self.client.post(reverse("quotations:send_password", args=[self.quotation.pk]))
        self.assertNotEqual(ShareLink.objects.get().token, token_before)  # a new password for the same link
        self.assertEqual(self.quotation.events.first().action, "password_sent")
        self.assertNotContains(self.client.get(reverse("quotations:detail", args=[self.quotation.pk])),
                               "data-password-retry")

    def test_a_failed_send_removes_the_link_and_can_be_retried(self):
        self._approve()
        self.gmail.fail = RuntimeError("network down")
        self.assertEqual(self._post(self.cro, "quotations:send").status_code, 502)
        self.assertFalse(ShareLink.objects.exists())
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "approved")
        self.gmail.fail = None
        self.assertEqual(self._post(self.cro, "quotations:send").status_code, 302)
        self.assertEqual(Quotation.objects.get(pk=self.quotation.pk).status, "sent")

    def test_only_the_cro_sends_or_resends_the_password(self):
        self._approve()
        self._post(self.cro, "quotations:send")
        for user in (self.costing, self.boss):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse("quotations:send_password",
                                                      args=[self.quotation.pk])).status_code, 403)

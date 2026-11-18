"""Tests for the Marketing portal: which enquiries are lost, the list page, and the actions on a lost enquiry."""
from datetime import date, timedelta
from unittest.mock import patch


from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import UserServiceAccess
from enquiries.models import CustomerStatusEmail, EnquiryContact, FollowUpSent
from marketing import lost
from marketing.models import MarketingActivity, MarketingLead

SETTINGS = dict(
    FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7, MARKETING_WAIT_DAYS=30,
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
)


def _enquiry(enquiry_id, status, **extra):
    data = {"enquiry_id": enquiry_id, "status": status, "customer_name": f"Customer {enquiry_id}",
            "subject": f"Gasket {enquiry_id}", "customer_reference": "", "remarks": "",
            "received_at_dt": timezone.now() - timedelta(days=90)}
    data.update(extra)
    return data


@override_settings(**SETTINGS)
class LostRuleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", "sales@example.com", "x")

    def _follow_ups(self, enquiry_id, count, last_days_ago):
        for n in range(count):
            FollowUpSent.objects.create(
                enquiry_id=enquiry_id, sent_by=self.user, status=FollowUpSent.STATUS_SENT,
                sent_at=timezone.now() - timedelta(days=last_days_ago + (count - 1 - n) * 7),
            )

    def test_rejected_is_lost(self):
        rows = lost.build_lost_leads([_enquiry("E1", "rejected")])
        self.assertEqual([(r["enquiry_id"], r["kind"]) for r in rows], [("E1", lost.REJECTED)])

    def test_closing_email_sent_is_no_response(self):
        CustomerStatusEmail.objects.create(enquiry_id="E2", kind=CustomerStatusEmail.KIND_CLOSING,
                                           event_key="closing", status=CustomerStatusEmail.STATUS_SENT,
                                           sent_at=timezone.now() - timedelta(days=3))
        rows = lost.build_lost_leads([_enquiry("E2", "closed")])
        self.assertEqual(rows[0]["kind"], lost.NO_RESPONSE)
        self.assertEqual(rows[0]["reason_label"], "Never replied")
        self.assertEqual(rows[0]["lost_on"], timezone.localdate() - timedelta(days=3))

    def test_all_follow_ups_sent_and_silent_is_no_response(self):
        self._follow_ups("E3", 4, last_days_ago=10)
        rows = lost.build_lost_leads([_enquiry("E3", "open")])
        self.assertEqual(rows[0]["kind"], lost.NO_RESPONSE)
        self.assertEqual(rows[0]["follow_up_count"], 4)

    def test_not_lost_yet(self):
        self._follow_ups("E4", 4, last_days_ago=2)   # still inside the 7 silent days
        self._follow_ups("E5", 2, last_days_ago=30)  # follow-ups not finished
        enquiries = [_enquiry("E4", "open"), _enquiry("E5", "open"), _enquiry("E6", "accepted"),
                     _enquiry("E7", "converted"), _enquiry("E8", "closed")]
        self.assertEqual(lost.build_lost_leads(enquiries), [])

    def test_payment_reminders_do_not_count(self):
        for n in range(4):
            FollowUpSent.objects.create(enquiry_id="E9", sent_by=self.user, status=FollowUpSent.STATUS_SENT,
                                        stage="payment_pending", sent_at=timezone.now() - timedelta(days=20 + n))
        self.assertEqual(lost.build_lost_leads([_enquiry("E9", "closed")]), [])

    def test_stages(self):
        today = timezone.localdate()
        for eid, days in (("R1", 5), ("R2", 40), ("R3", 40), ("R4", 40)):
            row = CustomerStatusEmail.objects.create(enquiry_id=eid, kind=CustomerStatusEmail.KIND_REJECTION,
                                                     event_key="rejection")
            CustomerStatusEmail.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=days))
        MarketingLead.objects.create(enquiry_id="R3", do_not_contact=True)
        MarketingLead.objects.create(enquiry_id="R4", revived_enquiry_id="NEW1")
        rows = {r["enquiry_id"]: r for r in lost.build_lost_leads(
            [_enquiry(e, "rejected") for e in ("R1", "R2", "R3", "R4")], today=today)}
        self.assertEqual(rows["R1"]["stage"], lost.STAGE_WAITING)
        self.assertEqual(rows["R1"]["ready_on"], today - timedelta(days=5) + timedelta(days=30))
        self.assertEqual(rows["R2"]["stage"], lost.STAGE_READY)
        self.assertEqual(rows["R3"]["stage"], lost.STAGE_DO_NOT_CONTACT)
        self.assertEqual(rows["R4"]["stage"], lost.STAGE_REVIVED)

    def test_financial_year(self):
        self.assertEqual(lost.financial_year_label(date(2026, 3, 31)), "2025-26")
        self.assertEqual(lost.financial_year_label(date(2026, 4, 1)), "2026-27")


@override_settings(**SETTINGS)
class MarketingViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("mk", "mk@example.com", "x")
        self.client.force_login(self.user)
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="buyer@acme.example")
        self.rejected = _enquiry("E1", "rejected", customer_name="Acme", assigned_to="Priya",
                                 remarks=("Internal note\nRejection reason (via share link): Too expensive\n"
                                          "compared with our current supplier"),
                                 final_price_per_piece=12.5, currency="USD")
        self.accepted = _enquiry("E2", "accepted", customer_name="Beta")

    def _list(self):
        return patch("enquiries.services.list_enquiries", return_value=[self.rejected, self.accepted])

    def _fetch(self, enquiry=None):
        return patch("enquiries.services.fetch_enquiry", return_value=enquiry or self.rejected)

    def test_board_lists_only_lost_enquiries(self):
        with self._list():
            response = self.client.get(reverse("marketing:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acme")
        self.assertContains(response, "buyer@acme.example")
        self.assertContains(response, "USD 12.50/pc")
        self.assertContains(response, "Too expensive compared with our current supplier")  # whole reason
        self.assertNotContains(response, "Beta")
        self.assertEqual(response.context["summary"]["rejected"], 1)

    def test_board_filters(self):
        with self._list():
            response = self.client.get(reverse("marketing:board"), {"kind": "no_response"})
        self.assertEqual(response.context["rows"], [])
        with self._list():
            response = self.client.get(reverse("marketing:board"), {"q": "acme"})
        self.assertEqual(len(response.context["rows"]), 1)

    def test_board_shows_service_error(self):
        from enquiries.services import EnquiryServiceError
        with patch("enquiries.services.list_enquiries", side_effect=EnquiryServiceError("down")):
            response = self.client.get(reverse("marketing:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Could not load enquiries")

    def test_the_marketing_page_links_to_the_enquirys_quotations(self):
        from quotations.models import Quotation
        quotation = Quotation.objects.create(customer_name="Customer E1", enquiry_id="E1", title="Gasket quote")
        with self._fetch():
            page = self.client.get(reverse("marketing:detail", args=["E1"]))
        self.assertContains(page, reverse("quotations:detail", args=[quotation.pk]))

    def test_open_enquiry_buttons_open_the_generated_pdf(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            page = self.client.get(url)
        self.assertContains(page, reverse("enquiries:export_pdf", args=["E1"]) + "?inline=1")
        self.assertContains(page, "Open original enquiry (PDF)")
        self.assertNotContains(page, 'href="' + reverse("enquiries:detail", args=["E1"]) + '"')  # no link to the page itself

    def test_the_enquiry_pdf_opens_in_the_browser_when_asked_and_downloads_otherwise(self):
        url = reverse("enquiries:export_pdf", args=["E1"])
        with patch("enquiries.views.services.export_enquiry_pdf", return_value=(b"%PDF-1.4 x", "Enquiry-E1.pdf")),                 patch("enquiries.views._get_api_root", return_value="http://svc"):
            inline = self.client.get(url + "?inline=1")
            download = self.client.get(url)
        self.assertEqual(inline["Content-Disposition"], 'inline; filename="Enquiry-E1.pdf"')
        self.assertEqual(download["Content-Disposition"], 'attachment; filename="Enquiry-E1.pdf"')
        self.assertEqual(inline["Content-Type"], "application/pdf")

    def test_detail_and_note(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            self.assertEqual(self.client.get(url).status_code, 200)
            response = self.client.post(url, {"action": "add_note", "kind": "call", "text": "Called Rahul"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        activity = MarketingActivity.objects.get()
        self.assertEqual((activity.kind, activity.text, activity.created_by), ("call", "Called Rahul", self.user))

    def test_set_reason_and_do_not_contact(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            self.client.post(url, {"action": "set_reason", "lost_reason": "price"})
            self.client.post(url, {"action": "toggle_do_not_contact"})
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertEqual(lead.lost_reason, "price")
        self.assertTrue(lead.do_not_contact)
        with self._fetch():
            self.client.post(url, {"action": "set_reason", "lost_reason": "made-up"})
        self.assertEqual(MarketingLead.objects.get(enquiry_id="E1").lost_reason, "price")

    def test_alternative_gasket_is_a_reason_in_the_drop_down(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            page = self.client.get(url)
            self.assertContains(page, '<option value="alternative_gasket">Alternative gasket purchased from AVIGIRI</option>')
            self.client.post(url, {"action": "set_reason", "lost_reason": "alternative_gasket"})
        self.assertEqual(MarketingLead.objects.get(enquiry_id="E1").lost_reason, "alternative_gasket")

    def test_other_reason_has_a_text_box_and_the_note_is_saved(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            page = self.client.get(url)
            self.assertContains(page, 'name="lost_reason_note"')
            self.client.post(url, {"action": "set_reason", "lost_reason": "other",
                                   "lost_reason_note": "  Bought  a   rubber   seal elsewhere  "})
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertEqual((lead.lost_reason, lead.lost_reason_note), ("other", "Bought a rubber seal elsewhere"))
        with self._fetch():
            page = self.client.get(url)
        self.assertContains(page, "Reason written: Bought a rubber seal elsewhere")
        self.assertContains(page, 'value="Bought a rubber seal elsewhere"')
        self.assertTrue(MarketingActivity.objects.filter(
            lead=lead, text="Reason set to: Other: Bought a rubber seal elsewhere.").exists())

    def test_other_needs_a_written_reason(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            self.client.post(url, {"action": "set_reason", "lost_reason": "other", "lost_reason_note": "   "})
        self.assertFalse(MarketingLead.objects.filter(enquiry_id="E1", lost_reason="other").exists())

    def test_the_note_is_dropped_when_another_reason_is_chosen(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch():
            self.client.post(url, {"action": "set_reason", "lost_reason": "other", "lost_reason_note": "Some text"})
            self.client.post(url, {"action": "set_reason", "lost_reason": "price", "lost_reason_note": "ignored"})
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertEqual((lead.lost_reason, lead.lost_reason_note), ("price", ""))

    def test_the_written_reason_shows_in_the_lost_list_label(self):
        lead = MarketingLead.objects.create(enquiry_id="E9", lost_reason="other", lost_reason_note="Cheaper local supplier")
        rows = lost.build_lost_leads([_enquiry("E9", "rejected")])
        self.assertEqual(rows[0]["reason_label"], "Other: Cheaper local supplier")

    def test_revive_creates_linked_enquiry(self):
        url = reverse("marketing:detail", args=["E1"])
        with self._fetch(), patch("enquiries.services.create_enquiry", return_value={"enquiry_id": "E50"}) as create:
            self.client.post(url, {"action": "revive", "revive_note": "Wants a new quote"})
            self.client.post(url, {"action": "revive"})  # second click does nothing
        self.assertEqual(create.call_count, 1)
        values = create.call_args.args[1]
        self.assertEqual(values["customer_name"], "Acme")
        self.assertEqual(values["assigned_to"], "Priya")
        self.assertIn("E1", values["description"])
        self.assertIn("Wants a new quote", values["description"])
        lead = MarketingLead.objects.get(enquiry_id="E1")
        self.assertEqual((lead.revived_enquiry_id, lead.revived_by), ("E50", self.user))
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="E50").customer_email, "buyer@acme.example")

    def test_revive_blocked_for_do_not_contact(self):
        MarketingLead.objects.create(enquiry_id="E1", do_not_contact=True)
        with self._fetch(), patch("enquiries.services.create_enquiry") as create:
            self.client.post(reverse("marketing:detail", args=["E1"]), {"action": "revive"})
        create.assert_not_called()

    def test_enquiry_that_is_not_lost_redirects(self):
        with self._fetch(self.accepted):
            response = self.client.get(reverse("marketing:detail", args=["E2"]))
        self.assertRedirects(response, reverse("marketing:board"), fetch_redirect_response=False)

    def test_users_without_marketing_access_are_sent_away(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["enquiries"])
        with self._list():
            response = self.client.get(reverse("marketing:board"))
        self.assertEqual(response.status_code, 302)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("marketing:board"))
        self.assertEqual(response.status_code, 302)


@override_settings(**SETTINGS)
class CustomerNameAndReasonTests(TestCase):
    def test_reason_keeps_every_line_after_the_marker(self):
        remarks = ("Sales note\nRejection reason (via share link): Dear Naveen,\n\n"
                   "Pricing is higher than our current supplier.\nRegards")
        self.assertEqual(lost.customer_reason_from_remarks(remarks),
                         "Dear Naveen, Pricing is higher than our current supplier. Regards")
        self.assertEqual(lost.customer_reason_from_remarks("Rejection reason (via share link): old\n"
                                                           "Rejection reason (via share link): new"), "new")
        self.assertEqual(lost.customer_reason_from_remarks("no reason here"), "")

    def test_subject_like_names(self):
        for name in ("Fwd: PI required - ABHINANDAN SKYLINE SYSTEMS", "Re: RFQ-EPDM_Lip Damping Profiles",
                     "RFQ-123 seals", "PO#4411", "x" * 90):
            self.assertTrue(lost.looks_like_subject(name), name)
        for name in ("Acme Rubber Pvt Ltd", "Jäger Gummi und Kunststoff GmbH", "PO Industries", "Reliance", ""):
            self.assertFalse(lost.looks_like_subject(name), name)
        self.assertTrue(lost.looks_like_subject("Seal profile enquiry for new project", "Seal profile enquiry for new project"))

    def test_display_name_order(self):
        show = lost.customer_display
        self.assertEqual(show("Acme", "S", "a@acme.example", "Acme Rubber Ltd")["name"], "Acme Rubber Ltd")
        self.assertEqual(show("Acme", "S", "a@acme.example")["greeting"], "Acme")
        subject_name = show("Fwd: PI required", "Fwd: PI required", "buyer@skyline.co.in")
        self.assertEqual((subject_name["name"], subject_name["source"], subject_name["greeting"]),
                         ("skyline.co.in", "email", ""))
        self.assertEqual(show("Fwd: PI required", "S", "someone@gmail.com")["name"], "")

    def test_marketing_can_correct_the_name(self):
        user = get_user_model().objects.create_user("mk", "mk@example.com", "x")
        self.client.force_login(user)
        enquiry = _enquiry("E9", "rejected", customer_name="Fwd: PI required - SKYLINE", subject="Fwd: PI required - SKYLINE")
        url = reverse("marketing:detail", args=["E9"])
        with patch("enquiries.services.fetch_enquiry", return_value=enquiry):
            page = self.client.get(url)
            self.assertContains(page, "Name not recorded")
            self.client.post(url, {"action": "set_customer_name", "customer_name": "  Abhinandan  Skyline Systems "})
        self.assertEqual(MarketingLead.objects.get(enquiry_id="E9").customer_name, "Abhinandan Skyline Systems")
        row = lost.build_lost_leads([enquiry])[0]
        self.assertEqual((row["customer_name"], row["greeting_name"]),
                         ("Abhinandan Skyline Systems", "Abhinandan Skyline Systems"))
        with patch("enquiries.services.fetch_enquiry", return_value=enquiry), \
                patch("enquiries.services.create_enquiry", return_value={"enquiry_id": "E10"}) as create:
            self.client.post(url, {"action": "revive"})
        self.assertEqual(create.call_args.args[1]["customer_name"], "Abhinandan Skyline Systems")


@override_settings(**SETTINGS)
class ReviveCopiesEmailsTests(TestCase):
    def test_revive_copies_other_customer_emails(self):
        user = get_user_model().objects.create_user("mk2", "mk2@example.com", "x")
        self.client.force_login(user)
        EnquiryContact.objects.create(enquiry_id="E1", customer_email="one@acme.example",
                                      other_customer_emails="two@acme.example")
        enquiry = _enquiry("E1", "rejected", customer_name="Acme")
        with patch("enquiries.services.fetch_enquiry", return_value=enquiry), \
                patch("enquiries.services.create_enquiry", return_value={"enquiry_id": "E70"}):
            self.client.post(reverse("marketing:detail", args=["E1"]), {"action": "revive"})
        new = EnquiryContact.objects.get(enquiry_id="E70")
        self.assertEqual((new.customer_email, new.other_customer_emails), ("one@acme.example", "two@acme.example"))


@override_settings(**SETTINGS)
class DoNotContactCardTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("mkcard", "mkcard@example.com", "x"))
        self.rows = [_enquiry(f"D{n}", "rejected", customer_name=f"Customer {n}") for n in range(1, 5)]
        MarketingLead.objects.create(enquiry_id="D1", do_not_contact=True)
        MarketingLead.objects.create(enquiry_id="D2", do_not_contact=True)
        MarketingLead.objects.create(enquiry_id="D3", do_not_contact=True, revived_enquiry_id="NEW")  # counted as revived

    def _board(self, **params):
        with patch("enquiries.services.list_enquiries", return_value=self.rows):
            return self.client.get(reverse("marketing:board"), params)

    def test_card_counts_do_not_contact_and_links_to_the_filter(self):
        response = self._board()
        self.assertEqual(response.context["summary"]["do_not_contact"], 2)
        self.assertContains(response, "Do not contact")
        self.assertContains(response, 'href="?stage=do_not_contact"')

    def test_the_filtered_list_matches_the_card(self):
        response = self._board(stage="do_not_contact")
        self.assertEqual([r["enquiry_id"] for r in response.context["rows"]], ["D2", "D1"])
        self.assertEqual(len(response.context["rows"]), response.context["summary"]["do_not_contact"])

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from dashboard.gmail_service import GmailAPIError
from django.core.files.uploadedfile import SimpleUploadedFile

from enquiries import services
from enquiries.models import EnquiryContact, GmailLeadAnalysis, ShareLink


@override_settings(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
class EnquiryViewRenderingTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="boardtester",
            email="boardtester@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.services.list_enquiries", return_value=[])
    def test_board_template_renders_without_error(self, _mock_list):
        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/board.html")
        self.assertContains(response, "Create enquiry")

    @patch("enquiries.views.services.fetch_subdivisions", return_value=[])
    @patch(
        "enquiries.views.services.fetch_enquiry",
        return_value={
            "enquiry_id": "ENQ-1",
            "status": "open",
            "priority": "high",
            "subject": "Example enquiry",
            "description": "A sample description",
            "detail_items": [],
            "attachments": [],
        },
    )
    def test_detail_template_renders_without_error(self, _mock_fetch_enquiry, _mock_fetch_subdivisions):
        response = self.client.get(reverse("enquiries:detail", args=["ENQ-1"]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/detail.html")
        self.assertContains(response, "Enquiry ENQ-1")
        self.assertContains(response, "Update enquiry")

    @patch("enquiries.views.services.fetch_subdivisions", return_value=[])
    @patch(
        "enquiries.views.services.fetch_enquiry",
        return_value={
            "enquiry_id": "ENQ-1",
            "status": "open",
            "priority": "high",
            "subject": "Example enquiry",
            "description": "A sample description",
            "detail_items": [],
            "attachments": [],
        },
    )
    def test_preview_template_renders_share_link_form(self, _mock_fetch_enquiry, _mock_fetch_subdivisions):
        response = self.client.get(reverse("enquiries:preview", args=["ENQ-1"]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/preview.html")
        self.assertContains(response, "Generate Link")

    @patch("enquiries.views.services.list_enquiries", return_value=[])
    def test_board_has_no_new_enquiry_sheet_card(self, _mock_list):
        # Excel now goes in through the quotation portal; the board keeps only the PDF RFQ upload.
        response = self.client.get(reverse("enquiries:board"))
        self.assertNotContains(response, "New Enquiry Sheet")
        self.assertNotContains(response, "createSheetForm")
        self.assertContains(response, "Upload PDF RFQ")

    @patch("enquiries.views.services.list_enquiries")
    def test_board_displays_generate_link_action(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": "ENQ-1001",
                "status": "open",
                "customer_name": "Northwind Manufacturing",
                "subject": "Specification change request",
            }
        ]

        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'title="Generate Link"')
        self.assertContains(response, reverse("enquiries:preview", args=["ENQ-1001"]))

    @patch("enquiries.views.services.list_enquiries")
    def test_board_displays_follow_up_action(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": "ENQ-1001",
                "status": "open",
                "customer_name": "Northwind Manufacturing",
                "subject": "Specification change request",
            }
        ]

        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'title="Follow-up"')
        self.assertContains(response, reverse("enquiries:follow_up", args=["ENQ-1001"]))

    @patch("enquiries.views.services.list_enquiries")
    def test_board_eye_action_opens_primary_pdf_inline(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": "ENQ-1001",
                "status": "open",
                "customer_name": "Northwind Manufacturing",
                "subject": "Specification change request",
                "general_attachments": [
                    {
                        "attachment_id": "att-1",
                        "file_name": "enquiry.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ]

        response = self.client.get(reverse("enquiries:board"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("enquiries:download_attachment", args=["ENQ-1001", "att-1"])}?inline=1"',
        )
        self.assertContains(response, 'title="Open Enquiry PDF"')
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, reverse("enquiries:preview", args=["ENQ-1001"]))

    @patch("enquiries.views.services.list_enquiries")
    def test_board_displays_rejection_comments_action(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": "ENQ-1007",
                "status": "rejected",
                "customer_name": "Demo",
                "subject": "Demo",
                "remarks": "Rejection reason (via share link): Price too high",
            }
        ]

        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "View comments")
        self.assertContains(response, 'data-rejection-reason="Price too high"')
        self.assertContains(response, 'badge-danger-soft')

    @patch("enquiries.views.services.list_enquiries")
    def test_board_displays_converted_toggle_for_accepted_enquiry(self, mock_list):
        # Accepted orders are converted through "Mark as paid"; the switch only undoes a conversion.
        mock_list.return_value = [
            {
                "enquiry_id": "ENQ-1008",
                "status": "converted",
                "customer_name": "Demo",
                "subject": "Converted enquiry",
            },
            {
                "enquiry_id": "ENQ-1009",
                "status": "accepted",
                "customer_name": "Demo",
                "subject": "Accepted enquiry",
            },
        ]

        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-toggle-url="/enquiries/ENQ-1009/converted/"')
        self.assertContains(response, 'data-converted-toggle')
        self.assertContains(response, 'data-toggle-url="/enquiries/ENQ-1008/converted/"')
        self.assertContains(response, 'for="convertedToggleENQ-1008"')

    @patch("enquiries.views.services.list_enquiries", return_value=[])
    def test_board_displays_per_page_selector(self, _mock_list):
        response = self.client.get(reverse("enquiries:board"), {"per_page": 100})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-page-size-select')
        self.assertContains(response, '<option value="100" selected>100</option>', html=True)

    @patch("enquiries.views.GmailService")
    @patch("enquiries.views.services.fetch_enquiry")
    def test_follow_up_redirects_to_compose_for_matching_gmail_thread(self, mock_fetch_enquiry, mock_gmail_service):
        mock_fetch_enquiry.return_value = {
            "enquiry_id": "ENQ-1001",
            "subject": "Specification change request",
            "customer_name": "Northwind Manufacturing",
        }
        mock_gmail_service.return_value.find_enquiry_conversations.return_value = [
            {
                "thread_id": "thread-123",
                "subject": "Specification change request",
                "customer_email": "buyer@northwind.example",
                "latest_message_id": "gmail-msg-123",
                "message_count": 1,
            }
        ]

        # A match found by subject/name only is shown for confirmation, not used blindly.
        response = self.client.get(reverse("enquiries:follow_up", args=["ENQ-1001"]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/follow_up_select.html")

        response = self.client.get(
            reverse("enquiries:follow_up", args=["ENQ-1001"]), {"thread": "thread-123"}
        )
        self.assertRedirects(
            response,
            reverse("dashboard:compose_email", args=["gmail-msg-123"]),
            fetch_redirect_response=False,
        )

    @patch("enquiries.views.services.fetch_summary")
    def test_summary_renders_filter_controls(self, mock_fetch_summary):
        mock_fetch_summary.return_value = {
            "generated_at_dt": timezone.now(),
            "metrics": {
                "total_enquiries": 12,
                "open_enquiries": 5,
                "accepted_enquiries": 2,
                "rejected_enquiries": 1,
                "converted_enquiries": 1,
                "by_priority": {},
                "by_channel": {},
                "by_material": {},
            },
            "highlights": [],
            "narrative": "Summary",
        }

        response = self.client.get(reverse("enquiries:summary"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="period"')
        self.assertContains(response, 'name="start_date"')
        self.assertContains(response, 'name="end_date"')
        self.assertContains(response, "Last 7 days")
        self.assertContains(response, "Last 30 days")
        self.assertContains(response, "Custom date range")
        self.assertNotContains(response, "Matching enquiries")

    @override_settings(ENQUIRY_API_ROOT="http://localhost:8405")
    @patch("enquiries.views.services.list_enquiries")
    @patch("enquiries.views.services.fetch_summary")
    def test_summary_passes_period_filters_to_service(self, mock_fetch_summary, mock_list_enquiries):
        mock_fetch_summary.return_value = {
            "generated_at_dt": timezone.now(),
            "metrics": {
                "total_enquiries": 6,
                "open_enquiries": 3,
                "accepted_enquiries": 1,
                "rejected_enquiries": 0,
                "converted_enquiries": 1,
                "by_priority": {},
                "by_channel": {},
                "by_material": {},
            },
            "highlights": [],
            "narrative": "Summary",
        }
        mock_list_enquiries.return_value = []

        response = self.client.get(
            reverse("enquiries:summary"),
            {"period": "custom", "start_date": "2026-04-01", "end_date": "2026-04-06", "applied": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_fetch_summary.assert_called_once_with(
            "http://localhost:8405",
            period="custom",
            start_date="2026-04-01",
            end_date="2026-04-06",
            request=response.wsgi_request,
        )
        self.assertEqual(response.context["summary_selected_period"], "custom")
        self.assertContains(response, 'value="2026-04-01"')
        self.assertContains(response, 'value="2026-04-06"')
        mock_list_enquiries.assert_called_once_with("http://localhost:8405", request=response.wsgi_request)

    @patch("enquiries.views.services.list_enquiries")
    @patch("enquiries.views.services.fetch_summary")
    def test_summary_shows_filtered_enquiry_rows_only_after_apply(self, mock_fetch_summary, mock_list_enquiries):
        mock_fetch_summary.return_value = {
            "generated_at_dt": timezone.now(),
            "metrics": {
                "total_enquiries": 2,
                "open_enquiries": 1,
                "accepted_enquiries": 1,
                "rejected_enquiries": 0,
                "converted_enquiries": 0,
                "by_priority": {},
                "by_channel": {},
                "by_material": {},
            },
            "highlights": [],
            "narrative": "Summary",
        }
        mock_list_enquiries.return_value = [
            {
                "enquiry_id": "ENQ-2001",
                "customer_name": "Acme",
                "subject": "Recent enquiry",
                "status": "open",
                "priority": "high",
                "received_at_dt": timezone.now() - timedelta(days=2),
            },
            {
                "enquiry_id": "ENQ-2002",
                "customer_name": "Legacy",
                "subject": "Older enquiry",
                "status": "accepted",
                "priority": "medium",
                "received_at_dt": timezone.now() - timedelta(days=40),
            },
        ]

        initial = self.client.get(reverse("enquiries:summary"))
        self.assertEqual(initial.status_code, 200)
        self.assertNotContains(initial, "ENQ-2001")
        self.assertNotContains(initial, "Matching enquiries")

        response = self.client.get(reverse("enquiries:summary"), {"period": "weekly", "applied": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Matching enquiries")
        self.assertContains(response, "ENQ-2001")
        self.assertContains(response, "Recent enquiry")
        self.assertContains(response, 'data-summary-open-enquiry')
        self.assertContains(response, reverse("enquiries:detail", args=["ENQ-2001"]))
        self.assertNotContains(response, "<th class=\"text-end\">Action</th>", html=True)
        self.assertNotContains(response, "View enquiry")
        self.assertNotContains(response, "ENQ-2002")

    def test_share_pretty_url_renders_password_prompt_without_http_401(self):
        token = signing.dumps(
            {
                "enquiry_id": "ENQ-1007",
                "pdf_filename": "quote.pdf",
                "pdf_content_b64": "JVBERi0xLjQ=",
                "pdf_content_type": "application/pdf",
                "requires_password": True,
                "password_hash": make_password("demo-pass"),
            },
            salt="enquiry-share",
            compress=True,
        )
        ShareLink.objects.create(
            id="ELqtfDKwDCPJ",
            token=token,
            brand="magicwand",
            enq_no="ENQ-1007",
        )

        response = self.client.get(
            reverse("enquiries:share_pretty", args=["magicwand", "ENQ-1007", "ELqtfDKwDCPJ"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "enquiries/share_password.html")
        self.assertContains(response, "This link is password protected")

    @patch("enquiries.views.services.list_enquiries")
    def test_board_paginates_enquiries_default_page_size(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": f"ENQ-{idx}",
                "status": "open",
                "customer_name": "Customer",
                "subject": f"Subject {idx}",
            }
            for idx in range(1, 121)
        ]

        response = self.client.get(reverse("enquiries:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "#ENQ-1")
        self.assertContains(response, "#ENQ-50")
        self.assertNotContains(response, "#ENQ-51")
        self.assertContains(response, "120 active")

    @patch("enquiries.views.services.list_enquiries")
    def test_board_paginates_enquiries_page_param(self, mock_list):
        mock_list.return_value = [
            {
                "enquiry_id": f"ENQ-{idx}",
                "status": "open",
                "customer_name": "Customer",
                "subject": f"Subject {idx}",
            }
            for idx in range(1, 121)
        ]

        response = self.client.get(reverse("enquiries:board"), {"page": 3, "per_page": 50})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "#ENQ-101")
        self.assertNotContains(response, "#ENQ-100")

    @patch(
        "enquiries.views.services.download_attachment",
        return_value=(b"%PDF-1.4", "quote.pdf", "application/pdf"),
    )
    def test_download_attachment_streams_file(self, mock_download_attachment):
        response = self.client.get(
            reverse("enquiries:download_attachment", args=["ENQ-1001", "att-1"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('filename="quote.pdf"', response["Content-Disposition"])
        mock_download_attachment.assert_called_once()


class CreateGoogleSheetViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="sheet-user",
            email="sheet@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.build")
    @patch("enquiries.views.GmailService")
    @patch("enquiries.views.services.create_enquiry")
    def test_create_google_sheet_success(self, mock_create_enquiry, mock_gmail_service, mock_build):
        mock_service_instance = mock_gmail_service.return_value
        mock_service_instance.build_authorized_credentials.return_value = object()

        mock_sheet_client = mock_build.return_value
        mock_sheets_resource = mock_sheet_client.spreadsheets.return_value
        mock_create_call = mock_sheets_resource.create.return_value
        mock_create_call.execute.return_value = {
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/demo/edit#gid=0",
            "spreadsheetId": "demo",
        }
        mock_create_enquiry.return_value = {"enquiry_id": "ENQ-42"}

        response = self.client.post(
            reverse("enquiries:create_google_sheet"),
            {
                "title": "Demo Enquiry",
            },
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["sheet_url"], "https://docs.google.com/spreadsheets/d/demo/edit#gid=0")
        self.assertEqual(payload["spreadsheet_id"], "demo")
        self.assertEqual(payload["enquiry_id"], "ENQ-42")
        mock_build.assert_called_once()
        mock_service_instance.build_authorized_credentials.assert_called_once()
        mock_sheets_resource.create.assert_called_once()
        mock_create_enquiry.assert_called_once()

    @patch("enquiries.views.GmailService")
    def test_create_google_sheet_auth_error(self, mock_gmail_service):
        mock_service_instance = mock_gmail_service.return_value
        mock_service_instance.build_authorized_credentials.side_effect = GmailAPIError("Connect account")

        response = self.client.post(
            reverse("enquiries:create_google_sheet"),
            {
                "title": "Demo",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Connect account")


class ImportGoogleSheetViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="import-user",
            email="import@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.services.create_enquiry")
    @patch("enquiries.views._build_enquiry_payload_from_sheet", return_value={"subject": "From Sheet"})
    def test_import_google_sheet_success(self, mock_builder, mock_create):
        response = self.client.post(
            reverse("enquiries:import_google_sheet"),
            {
                "sheet_url": "https://docs.google.com/spreadsheets/d/demo-id/edit",
                "status": "open",
            },
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        mock_builder.assert_called_once()
        mock_create.assert_called_once()

    @patch("enquiries.views.services.create_enquiry")
    def test_import_google_sheet_invalid_form(self, mock_create):
        response = self.client.post(reverse("enquiries:import_google_sheet"), {"sheet_url": ""})
        self.assertEqual(response.status_code, 302)
        mock_create.assert_not_called()


class UploadPDFEnquiryViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="pdf-user",
            email="pdf@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.services.create_enquiry")
    def test_upload_pdf_success(self, mock_create):
        pdf_file = SimpleUploadedFile("test.pdf", b"%PDF-1.4", content_type="application/pdf")
        response = self.client.post(
            reverse("enquiries:upload_pdf_enquiry"),
            {"title": "Uploaded PDF", "pdf": pdf_file},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        mock_create.assert_called_once()

    def test_upload_pdf_invalid(self):
        fake_file = SimpleUploadedFile("test.txt", b"not pdf", content_type="text/plain")
        response = self.client.post(
            reverse("enquiries:upload_pdf_enquiry"),
            {"title": "Uploaded PDF", "pdf": fake_file},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)


class ToggleConvertedStatusTests(TestCase):
    """The switch converts only orders whose payment is recorded as Paid (see test_order_tracking)."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="toggle-user",
            email="toggle@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.services.update_enquiry")
    def test_toggle_sets_converted_status(self, mock_update):
        mock_update.return_value = {}
        EnquiryContact.objects.create(enquiry_id="ENQ-500", payment_status="paid")  # converting needs payment
        url = reverse("enquiries:toggle_converted", args=["ENQ-500"])
        payload = json.dumps({"converted": True, "previous_status": "open"})
        response = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["new_status"], "converted")
        self.assertEqual(data["previous_status"], "open")
        self.assertEqual(data["enquiry_id"], "ENQ-500")
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        self.assertIn("status", args[2])
        self.assertEqual(args[2]["status"], "converted")

    @patch("enquiries.views.services.update_enquiry")
    def test_toggle_back_to_previous_status(self, mock_update):
        mock_update.return_value = {}
        EnquiryContact.objects.create(enquiry_id="ENQ-777", payment_status="paid")  # converting needs payment
        url = reverse("enquiries:toggle_converted", args=["ENQ-777"])

        initial_payload = json.dumps({"converted": True, "previous_status": "accepted"})
        self.client.post(url, data=initial_payload, content_type="application/json")
        self.assertTrue(mock_update.called)
        mock_update.reset_mock()

        restore_payload = json.dumps({"converted": False, "previous_status": "accepted"})
        response = self.client.post(url, data=restore_payload, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["new_status"], "accepted")
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        self.assertEqual(args[2]["status"], "accepted")


class GmailAiLeadTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="lead-user",
            email="leaduser@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.GmailService")
    def test_gmail_ai_leads_returns_relevant_pdf_enquiries_as_leads(
        self,
        mock_gmail_service,
    ) -> None:
        gmail = mock_gmail_service.return_value
        gmail.search_messages.return_value = [
            {
                "id": "msg-1",
                "sender": "buyer@customer.com",
                "subject": "PO for gasket drawing",
                "snippet": "Please review attached drawing and quote",
                "parsed_date": timezone.now(),
            }
        ]
        gmail.get_message.return_value = {
            "id": "msg-1",
            "sender": "buyer@customer.com",
            "subject": "PO for gasket drawing",
            "snippet": "Please review attached drawing and quote",
            "body_text": "Please send pricing and lead time for attached gasket profile.",
            "parsed_date": timezone.now(),
            "attachments": [{"filename": "drawing.pdf", "mimeType": "application/pdf"}],
        }

        response = self.client.get(reverse("enquiries:gmail_ai_leads"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["leads"][0]["message_id"], "msg-1")
        self.assertEqual(payload["leads"][0]["lead_score"], 100)
        self.assertIn("Heuristic lead detection matched", payload["leads"][0]["reason"])
        self.assertIn("mail.google.com", payload["leads"][0]["gmail_url"])
        self.assertEqual(GmailLeadAnalysis.objects.filter(user=self.user, is_lead=True).count(), 1)

    @patch("enquiries.views.GmailService")
    def test_gmail_ai_leads_accepts_gasket_feasibility_email(
        self,
        mock_gmail_service,
    ) -> None:
        gmail = mock_gmail_service.return_value
        gmail.search_messages.return_value = [
            {
                "id": "msg-feasibility",
                "sender": "aarpu@windows.example",
                "subject": "Requesting for gasket feasibility of AP34 CW (Curtain Wall) for aarpu windows and doors",
                "snippet": "Please check feasibility as per attached PDF.",
                "parsed_date": timezone.now(),
            }
        ]
        gmail.get_message.return_value = {
            "id": "msg-feasibility",
            "sender": "aarpu@windows.example",
            "subject": "Requesting for gasket feasibility of AP34 CW (Curtain Wall) for aarpu windows and doors",
            "snippet": "Please check feasibility as per attached PDF.",
            "body_text": "We need gasket feasibility for the attached curtain wall profile drawing.",
            "parsed_date": timezone.now(),
            "attachments": [{"filename": "AP34-CW-gasket.pdf", "mimeType": "application/pdf"}],
        }

        response = self.client.get(reverse("enquiries:gmail_ai_leads"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["leads"][0]["message_id"], "msg-feasibility")
        self.assertGreaterEqual(payload["leads"][0]["lead_score"], 65)
        self.assertEqual(GmailLeadAnalysis.objects.filter(user=self.user, is_lead=True).count(), 1)

    @patch("enquiries.views.GmailService")
    def test_gmail_ai_leads_rejects_pdf_invoice_false_positive(
        self,
        mock_gmail_service,
    ) -> None:
        gmail = mock_gmail_service.return_value
        gmail.search_messages.return_value = [
            {
                "id": "msg-invoice",
                "sender": "accounts@customer.com",
                "subject": "Tax invoice for gasket dispatch",
                "snippet": "Please find attached invoice PDF.",
                "parsed_date": timezone.now(),
            }
        ]
        gmail.get_message.return_value = {
            "id": "msg-invoice",
            "sender": "accounts@customer.com",
            "subject": "Tax invoice for gasket dispatch",
            "snippet": "Please find attached invoice PDF.",
            "body_text": "Please find attached tax invoice for your records.",
            "parsed_date": timezone.now(),
            "attachments": [{"filename": "invoice.pdf", "mimeType": "application/pdf"}],
        }

        response = self.client.get(reverse("enquiries:gmail_ai_leads"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["leads"], [])
        record = GmailLeadAnalysis.objects.get(user=self.user, message_id="msg-invoice")
        self.assertFalse(record.is_lead)
        self.assertIn("invoice", record.reason.lower())

    @patch("enquiries.views.GmailService")
    def test_gmail_ai_leads_skips_messages_without_pdf_attachments(
        self,
        mock_gmail_service,
    ) -> None:
        GmailLeadAnalysis.objects.create(
            user=self.user,
            message_id="msg-2",
            sender="buyer@customer.com",
            subject="Need pricing",
            is_lead=False,
            lead_score=0,
            analyzed_at=timezone.now() - timedelta(hours=3),
        )

        gmail = mock_gmail_service.return_value
        gmail.search_messages.return_value = [
            {
                "id": "msg-2",
                "sender": "buyer@customer.com",
                "subject": "Need pricing",
                "snippet": "Need pricing for gasket profile",
                "parsed_date": timezone.now(),
            }
        ]
        gmail.get_message.return_value = {
            "id": "msg-2",
            "sender": "buyer@customer.com",
            "subject": "Need pricing",
            "snippet": "Need pricing for gasket profile",
            "body_text": "Please quote urgently.",
            "parsed_date": timezone.now(),
            "attachments": [],
        }

        response = self.client.get(reverse("enquiries:gmail_ai_leads"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["leads"], [])
        record = GmailLeadAnalysis.objects.get(user=self.user, message_id="msg-2")
        self.assertFalse(record.is_lead)
        self.assertEqual(record.lead_score, 0)

    def test_toggle_requires_previous_status_when_converting(self):
        url = reverse("enquiries:toggle_converted", args=["ENQ-888"])
        payload = json.dumps({"converted": True})
        response = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(response.status_code, 400)


class DeleteEnquiryViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="delete-user",
            email="delete@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    @patch("enquiries.views.services.delete_enquiry")
    def test_delete_enquiry_proxies_to_service(self, mock_delete):
        url = reverse("enquiries:delete_enquiry", args=["ENQ-900"])
        response = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["enquiry_id"], "ENQ-900")
        mock_delete.assert_called_once()

    @patch("enquiries.views.services.delete_enquiry")
    def test_delete_enquiry_handles_service_error(self, mock_delete):
        mock_delete.side_effect = services.EnquiryServiceError("Boom")
        url = reverse("enquiries:delete_enquiry", args=["ENQ-901"])
        response = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

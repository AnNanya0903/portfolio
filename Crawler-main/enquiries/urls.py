from django.urls import path

from . import api_views, negotiation_views, views
from magicwand.access import require_service_access

app_name = "enquiries"

_enquiries = require_service_access("enquiries")

urlpatterns = [
    path("", _enquiries(views.board), name="board"),
    # Stage 2: called by n8n with the X-N8N-Token header (no login session).
    path("api/auto-follow-ups/due/", api_views.due, name="auto_follow_up_due"),
    path("api/auto-follow-ups/<str:enquiry_id>/send/", api_views.send, name="auto_follow_up_send"),
    path("api/customer-emails/run/", api_views.run_customer_emails, name="customer_emails_run"),
    path("tasks/", _enquiries(negotiation_views.tasks), name="tasks"),
    path("tasks/<int:pk>/update/", _enquiries(negotiation_views.task_update), name="task_update"),
    path("<str:enquiry_id>/negotiation/", _enquiries(negotiation_views.negotiation_page), name="negotiation"),
    path("<str:enquiry_id>/negotiation/stage/", _enquiries(negotiation_views.set_stage), name="negotiation_stage"),
    path("<str:enquiry_id>/negotiation/forward/", _enquiries(negotiation_views.forward), name="negotiation_forward"),
    path("<str:enquiry_id>/negotiation/followups/add/", _enquiries(negotiation_views.add_followup), name="followup_add"),
    path("<str:enquiry_id>/negotiation/attachments/add/", _enquiries(negotiation_views.upload_attachment), name="attachment_add"),
    path("followups/<int:pk>/done/", _enquiries(negotiation_views.followup_done), name="followup_done"),
    path("enquiry-files/<int:pk>/", _enquiries(negotiation_views.download_attachment), name="attachment_download"),
    path("gmail-ai-leads/", _enquiries(views.gmail_ai_leads), name="gmail_ai_leads"),
    path("google-sheet/create/", _enquiries(views.create_google_sheet), name="create_google_sheet"),
    path("google-sheet/import/", _enquiries(views.import_google_sheet), name="import_google_sheet"),
    path("upload-pdf/", _enquiries(views.upload_pdf_enquiry), name="upload_pdf_enquiry"),
    path("summary/", _enquiries(views.summary), name="summary"),
    path("gmail-queue/", _enquiries(views.gmail_queue), name="gmail_queue"),
    path("<str:enquiry_id>/follow-up/", _enquiries(views.follow_up), name="follow_up"),
    path("<str:enquiry_id>/follow-up/start/", _enquiries(views.follow_up_start), name="follow_up_start"),
    path(
        "<str:enquiry_id>/auto-follow-up/toggle/",
        _enquiries(views.toggle_auto_follow_up),
        name="toggle_auto_follow_up",
    ),
    path("<str:enquiry_id>/preview/", _enquiries(views.preview), name="preview"),
    path("share/", views.share, name="share"),
    path("share/action/", views.share_action, name="share_action"),
    # Pretty share URL including brand and enquiry/invoice number (token remains path param)
    path(
        "share/<str:brand>/<str:enq_no>/<str:token>/",
        views.share,
        name="share_pretty",
    ),
    path("generate-link/", _enquiries(views.generate_link), name="generate_link"),
    path("share/send-email/", _enquiries(views.send_share_email), name="send_share_email"),
    path("<str:enquiry_id>/subdivisions/add/", _enquiries(views.subdivision_create), name="subdivision_create"),
    path(
        "<str:enquiry_id>/subdivisions/<str:subdivision_id>/",
        _enquiries(views.subdivision_detail),
        name="subdivision_detail",
    ),
    path(
        "<str:enquiry_id>/subdivisions/<str:subdivision_id>/delete/",
        _enquiries(views.subdivision_delete),
        name="subdivision_delete",
    ),
    path(
        "<str:enquiry_id>/subdivisions/<str:subdivision_id>/preview/",
        _enquiries(views.preview_subdivision),
        name="subdivision_preview",
    ),
    path(
        "<str:enquiry_id>/subdivisions/<str:subdivision_id>/attachments/<str:attachment_id>/",
        _enquiries(views.download_subdivision_attachment),
        name="download_subdivision_attachment",
    ),
    path("preview/pdf/", _enquiries(views.preview_pdf), name="preview_pdf"),
    path("<str:enquiry_id>/export/pdf/", _enquiries(views.export_pdf), name="export_pdf"),
    path(
        "<str:enquiry_id>/attachments/<str:attachment_id>/",
        _enquiries(views.download_attachment),
        name="download_attachment",
    ),
    path(
        "<str:enquiry_id>/converted/",
        _enquiries(views.toggle_converted_status),
        name="toggle_converted",
    ),
    path(
        "<str:enquiry_id>/delete/",
        _enquiries(views.delete_enquiry),
        name="delete_enquiry",
    ),
    path("<str:enquiry_id>/", _enquiries(views.detail), name="detail"),
]

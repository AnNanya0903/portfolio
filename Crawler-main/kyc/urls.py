from django.urls import path

from magicwand.access import require_any_service_access, require_service_access

from . import (department_views, email_views, import_views, link_public_views, link_views, verify_views,
               views)

app_name = "kyc"

_kyc = require_service_access("kyc")
_kyc_json = require_service_access("kyc", deny="json")

urlpatterns = [
    path("", _kyc(views.customer_list), name="list"),
    path("new/", _kyc(views.customer_new), name="new"),
    # The picker is used on the enquiries board, so enquiry users may search too.
    path("api/customers/", require_any_service_access(["kyc", "enquiries"], deny="json")(views.customer_search),
         name="customer_search"),
    # The shared compose box (Marketing today): one endpoint for every portal's emails.
    path("emails/send/", require_any_service_access(["kyc", "marketing"], deny="json")(email_views.send),
         name="email_send"),
    path("departments/", _kyc(department_views.departments), name="departments"),
    path("departments/manage/", _kyc(department_views.departments_manage), name="departments_manage"),
    path("departments/save/", _kyc(department_views.department_save), name="department_add"),
    path("departments/<int:pk>/save/", _kyc(department_views.department_save), name="department_save"),
    path("departments/<int:pk>/toggle/", _kyc(department_views.department_toggle), name="department_toggle"),
    path("departments/<int:pk>/emails/add/", _kyc(department_views.department_email_add), name="department_email_add"),
    path("departments/<int:pk>/emails/<int:contact_id>/edit/", _kyc(department_views.department_email_edit),
         name="department_email_edit"),
    path("departments/<int:pk>/emails/<int:contact_id>/primary/", _kyc(department_views.department_email_primary),
         name="department_email_primary"),
    path("departments/<int:pk>/emails/<int:contact_id>/remove/", _kyc(department_views.department_email_remove),
         name="department_email_remove"),
    path("import/", _kyc(import_views.import_new), name="import_new"),
    path("import/sheet/", _kyc(import_views.import_sheet), name="import_sheet"),
    path("import/sheet/template/", _kyc(import_views.sheet_template), name="sheet_template"),
    path("import/<int:doc_id>/", _kyc(import_views.import_review), name="import_review"),
    path("import/<int:doc_id>/discard/", _kyc(import_views.import_discard), name="import_discard"),
    path("documents/<int:doc_id>/file/", _kyc(import_views.document_file), name="document_file"),
    # Public: the private link in the verification email. No sign-in, so no access check.
    path("verify/<str:token>/", verify_views.verify, name="verify"),
    # KYC by shareable link — staff (kyc/link_views.py) and the public form (kyc/link_public_views.py).
    path("requests/", _kyc(link_views.request_list), name="link_list"),
    path("requests/new/<int:customer_pk>/", _kyc(link_views.request_new), name="link_new"),
    path("requests/<int:pk>/", _kyc(link_views.request_detail), name="link_detail"),
    path("requests/<int:pk>/resend/", _kyc(link_views.request_resend), name="link_resend"),
    path("requests/<int:pk>/revoke/", _kyc(link_views.request_revoke), name="link_revoke"),
    path("requests/<int:pk>/review/", _kyc(link_views.request_review), name="link_review"),
    path("requests/<int:pk>/confirmation/", _kyc(link_views.request_confirmation), name="link_confirmation"),
    path("requests/<int:pk>/payment-terms/", _kyc(link_views.request_payment_terms), name="link_payment_terms"),
    path("requests/<int:pk>/pdf/", _kyc(link_views.request_pdf), name="link_request_pdf"),
    path("requests/<int:pk>/pdf/<int:doc_id>/", _kyc(link_views.request_pdf_version), name="link_pdf_version"),
    path("requests/<int:pk>/credit-limit/", _kyc(link_views.request_credit_limit), name="link_credit_limit"),
    path("requests/manual/<int:customer_pk>/", _kyc(link_views.request_manual), name="link_manual"),
    path("requests/<int:pk>/signed/", _kyc(link_views.request_signed_upload), name="link_signed_upload"),
    path("requests/<int:pk>/reveal/", _kyc_json(link_views.request_reveal), name="link_reveal"),
    path("requests/<int:pk>/fill/", _kyc(link_views.request_fill), name="link_fill"),
    path("requests/<int:pk>/fill/save/", _kyc_json(link_views.request_fill_save), name="link_fill_save"),
    path("requests/<int:pk>/fill/documents/", _kyc_json(link_views.request_fill_upload), name="link_fill_upload"),
    path("requests/<int:pk>/fill/submit/", _kyc_json(link_views.request_fill_submit), name="link_fill_submit"),
    path("request-documents/<int:doc_id>/download/", _kyc(link_views.document_download), name="link_document"),
    # Called by App Engine cron (cron.yaml): expire links, send reminders. Checks the cron header itself.
    path("api/cron/kyc-links/", link_views.cron_jobs, name="link_cron"),
    # Public: no sign-in. The token is the key; the form needs an emailed OTP as well.
    path("form/<str:token>/", link_public_views.form, name="link_form"),
    path("form/<str:token>/preview/", link_public_views.preview, name="link_preview"),
    path("form/<str:token>/otp/send/", link_public_views.otp_send, name="link_otp_send"),
    path("form/<str:token>/otp/verify/", link_public_views.otp_verify, name="link_otp_verify"),
    path("form/<str:token>/documents/", link_public_views.documents, name="link_documents"),
    path("form/<str:token>/submit/", link_public_views.submit, name="link_submit"),
    path("form/<str:token>/confirm/", link_public_views.confirm, name="link_confirm"),
    path("form/<str:token>/pdf/", link_public_views.pdf, name="link_pdf"),
    path("form/<str:token>/signed/", link_public_views.signed, name="link_signed"),
    path("<int:pk>/", _kyc(views.customer_detail), name="detail"),
    path("<int:pk>/verification/send/", _kyc(verify_views.verification_send), name="verification_send"),
    path("<int:pk>/kyc-form.pdf", _kyc(link_views.customer_pdf), name="customer_pdf"),
    path("<int:pk>/edit/", _kyc(views.customer_edit), name="edit"),
    path("<int:pk>/delete/", _kyc(views.customer_delete), name="delete"),
    path("<int:pk>/contacts/new/", _kyc(views.contact_new), name="contact_new"),
    path("<int:pk>/contacts/<int:contact_id>/edit/", _kyc(views.contact_edit), name="contact_edit"),
    path("<int:pk>/contacts/<int:contact_id>/delete/", _kyc(views.contact_delete), name="contact_delete"),
]

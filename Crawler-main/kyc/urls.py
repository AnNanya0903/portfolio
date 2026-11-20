from django.urls import path

from magicwand.access import require_any_service_access, require_service_access

from . import department_views, email_views, import_views, verify_views, views

app_name = "kyc"

_kyc = require_service_access("kyc")

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
    path("<int:pk>/", _kyc(views.customer_detail), name="detail"),
    path("<int:pk>/verification/send/", _kyc(verify_views.verification_send), name="verification_send"),
    path("<int:pk>/edit/", _kyc(views.customer_edit), name="edit"),
    path("<int:pk>/delete/", _kyc(views.customer_delete), name="delete"),
    path("<int:pk>/contacts/new/", _kyc(views.contact_new), name="contact_new"),
    path("<int:pk>/contacts/<int:contact_id>/edit/", _kyc(views.contact_edit), name="contact_edit"),
    path("<int:pk>/contacts/<int:contact_id>/delete/", _kyc(views.contact_delete), name="contact_delete"),
]

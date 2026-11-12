from django.urls import path

from magicwand.access import require_service_access

from . import import_views, verify_views, views

app_name = "kyc"

_kyc = require_service_access("kyc")

urlpatterns = [
    path("", _kyc(views.customer_list), name="list"),
    path("new/", _kyc(views.customer_new), name="new"),
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
    path("<int:pk>/contacts/new/", _kyc(views.contact_new), name="contact_new"),
    path("<int:pk>/contacts/<int:contact_id>/edit/", _kyc(views.contact_edit), name="contact_edit"),
    path("<int:pk>/contacts/<int:contact_id>/delete/", _kyc(views.contact_delete), name="contact_delete"),
]

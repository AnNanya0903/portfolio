from django.urls import path

from magicwand.access import require_service_access

from . import views

app_name = "kyc"

_kyc = require_service_access("kyc")

urlpatterns = [
    path("", _kyc(views.customer_list), name="list"),
    path("new/", _kyc(views.customer_new), name="new"),
    path("<int:pk>/", _kyc(views.customer_detail), name="detail"),
    path("<int:pk>/edit/", _kyc(views.customer_edit), name="edit"),
    path("<int:pk>/contacts/new/", _kyc(views.contact_new), name="contact_new"),
    path("<int:pk>/contacts/<int:contact_id>/edit/", _kyc(views.contact_edit), name="contact_edit"),
    path("<int:pk>/contacts/<int:contact_id>/delete/", _kyc(views.contact_delete), name="contact_delete"),
]

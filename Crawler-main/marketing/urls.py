from django.urls import path

from magicwand.access import require_service_access

from . import campaign_views, views

app_name = "marketing"

_marketing = require_service_access("marketing")

urlpatterns = [
    path("", _marketing(views.board), name="board"),
    path("campaigns/", _marketing(campaign_views.campaign_list), name="campaign_list"),
    path("campaigns/new/", _marketing(campaign_views.campaign_new), name="campaign_new"),
    path("campaigns/<int:pk>/", _marketing(campaign_views.campaign_detail), name="campaign_detail"),
    path("campaigns/<int:pk>/edit/", _marketing(campaign_views.campaign_edit), name="campaign_edit"),
    path("campaigns/<int:pk>/send/", _marketing(campaign_views.campaign_send), name="campaign_send"),
    path("campaigns/<int:pk>/replies/", _marketing(campaign_views.campaign_check_replies),
         name="campaign_check_replies"),
    path("campaigns/<int:pk>/delete/", _marketing(campaign_views.campaign_delete), name="campaign_delete"),
    path("<str:enquiry_id>/", _marketing(views.detail), name="detail"),
]

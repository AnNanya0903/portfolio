from django.urls import path

from magicwand.access import require_service_access

from . import views

app_name = "quotations"

# Everyone here needs the Quotations module. Uploading, approving and the Excel files are checked
# again inside the views (quotations/roles.py).
_q = require_service_access("quotations")

urlpatterns = [
    path("", _q(views.quotation_list), name="list"),
    path("new/", _q(views.quotation_new), name="new"),
    path("<int:pk>/", _q(views.quotation_detail), name="detail"),
    path("<int:pk>/versions/new/", _q(views.version_new), name="version_new"),
    path("<int:pk>/decide/", _q(views.decide), name="decide"),
    path("<int:pk>/sent/", _q(views.mark_sent), name="mark_sent"),
    path("<int:pk>/delete/", _q(views.quotation_delete), name="delete"),
    path("versions/<int:version_id>/pdf/", _q(views.version_pdf), name="version_pdf"),
    path("versions/<int:version_id>/excel/", _q(views.version_excel), name="version_excel"),
    path("events/<int:event_id>/file/", _q(views.event_file), name="event_file"),
]

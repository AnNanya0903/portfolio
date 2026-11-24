from django.urls import path

from functools import wraps

from magicwand.access import require_service_access

from . import roles, views

app_name = "quotations"

# Everyone here needs the Quotations module, except Top Management who always get in. Uploading, approving
# and the Excel files are checked again inside the views (quotations/roles.py).
_module = require_service_access("quotations")


def _q(view):
    guarded = _module(view)

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if roles.can_open_portal(request.user):
            return view(request, *args, **kwargs)
        return guarded(request, *args, **kwargs)  # shows the usual "no access" message

    return wrapper

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

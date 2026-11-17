from __future__ import annotations

from django.conf import settings

from .access import SERVICE_DEFINITIONS, user_has_service_access


def service_access(request):
    """Which sections the menus show: the ones this user may use, minus the ones hidden site-wide.

    Hiding (MAGICWAND_HIDDEN_SERVICES) only removes the links. Access to the pages themselves is
    decided by magicwand.access, so a hidden page still opens from a direct link.
    """
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"mw_service_access": {}}

    hidden = set(getattr(settings, "MAGICWAND_HIDDEN_SERVICES", None) or ())
    flags = {
        slug: slug not in hidden and user_has_service_access(user, slug)
        for slug in SERVICE_DEFINITIONS.keys()
    }
    from enquiries.negotiation import pending_task_count

    return {"mw_service_access": flags, "mw_pending_tasks": pending_task_count(user) if flags.get("enquiries") else 0}

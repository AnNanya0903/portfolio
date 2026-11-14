from __future__ import annotations

from functools import wraps
from typing import Dict, Literal

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect

from dashboard.models import UserServiceAccess
from .user_utils import get_user_primary_email

ServiceDenyMode = Literal["redirect", "json", "forbidden"]


SERVICE_DEFINITIONS: Dict[str, str] = {
    "gmail_ai": "Gmail AI",
    "design": "Design library",
    "enquiries": "Enquiries",
    "marketing": "Marketing",
    "kyc": "KYC",
    "quotations": "Quotations",
    "quotation_costing": "Quotations: upload and costing",
    "quotation_approval": "Quotations: price approval",
    "deskera": "Deskera",
    "mrp": "Manufacturing",
    "accounts": "Accounts",
    "compounding_planning": "Compounding planning",
    "compounding_board": "Compounding board",
    "discussion": "Discussion forum",
    "document_scanner": "Document scanner",
    "gasket_counter": "Gasket counter",
}

LEGACY_SERVICE_ALIASES: Dict[str, set[str]] = {
    # Older deployments used a single "compounding" module slug.
    "compounding_planning": {"compounding"},
    "compounding_board": {"compounding"},
}


def is_super_admin(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    email = get_user_primary_email(user)
    return bool(email) and email in getattr(settings, "SUPER_ADMIN_EMAILS", set())


def user_has_service_access(user, service: str) -> bool:
    """
    Default-allow permissions check.

    If the access model is missing/unavailable, fail open to avoid locking users out.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    service = (service or "").strip()
    if not service:
        return True

    try:
        access = UserServiceAccess.objects.filter(user=user).only(
            "restrict", "allowed_services"
        ).first()
    except Exception:
        return True

    if not access or not access.restrict:
        return True

    allowed = access.allowed_services or []
    if service in allowed:
        return True

    for alias in LEGACY_SERVICE_ALIASES.get(service, set()):
        if alias in allowed:
            return True

    return False


def expand_allowed_services(allowed_services: list[str]) -> list[str]:
    """
    Expand legacy slugs into their modern equivalents for UI display.

    Example: "compounding" -> ["compounding_planning", "compounding_board"].
    """
    expanded = set((allowed_services or []))
    if "compounding" in expanded:
        expanded.update({"compounding_planning", "compounding_board"})
    return sorted(expanded)


def require_service_access(
    service: str,
    *,
    deny: ServiceDenyMode = "redirect",
    redirect_url_name: str = "dashboard_hub",
):
    """
    Decorator enforcing module access. Use deny="json" for fetch/AJAX endpoints.
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if user_has_service_access(getattr(request, "user", None), service):
                return view_func(request, *args, **kwargs)

            label = SERVICE_DEFINITIONS.get(service, "This module")

            if deny == "json":
                return JsonResponse(
                    {"error": "access_denied", "message": f"No access to {label}."},
                    status=403,
                )

            if deny == "forbidden":
                return HttpResponseForbidden(f"No access to {label}.")

            messages.error(request, f"You do not have access to {label}.")
            return redirect(redirect_url_name)

        return _wrapped

    return decorator


def require_any_service_access(
    services: list[str],
    *,
    deny: ServiceDenyMode = "redirect",
    redirect_url_name: str = "dashboard_hub",
):
    """
    Decorator enforcing access to at least one of the given services.
    """

    services_clean = [str(s or "").strip() for s in services if str(s or "").strip()]

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if any(user_has_service_access(user, slug) for slug in services_clean):
                return view_func(request, *args, **kwargs)

            label = "this module"
            if services_clean:
                label = ", ".join(SERVICE_DEFINITIONS.get(s, s) for s in services_clean[:2])
                if len(services_clean) > 2:
                    label = f"{label}, …"

            if deny == "json":
                return JsonResponse(
                    {"error": "access_denied", "message": f"No access to {label}."},
                    status=403,
                )

            if deny == "forbidden":
                return HttpResponseForbidden(f"No access to {label}.")

            messages.error(request, f"You do not have access to {label}.")
            return redirect(redirect_url_name)

        return _wrapped

    return decorator

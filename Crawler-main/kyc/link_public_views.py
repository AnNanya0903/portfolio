"""The customer's side of KYC by shareable link. No sign-in: the private link is the key, then an emailed OTP.

    GET    /kyc/form/<token>/              the page (states: invalid, expired, revoked, submitted, OTP, form, confirm)
    PUT    /kyc/form/<token>/              save draft (JSON {"values": {...}})
    POST   /kyc/form/<token>/otp/send/
    POST   /kyc/form/<token>/otp/verify/   JSON {"code": "123456"}
    POST   /kyc/form/<token>/documents/    multipart: doc_type, file
    POST   /kyc/form/<token>/submit/       JSON {"consent": true}
    POST   /kyc/form/<token>/confirm/      the customer confirms details staff entered
    GET    /kyc/form/<token>/preview/      old preview address: redirects to the (one-page) form

Every endpoint is rate limited per IP and link. Nothing but the link state is shown before the OTP is verified.
"""
from __future__ import annotations

import json
from functools import wraps
from typing import Any, Dict

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST

from . import link_fields
from . import link_requests as svc
from .link_requests import Actor, KycLinkError
from .link_security import client_ip, mask, rate_limited
from .models import KycRequest

SESSION_KEY = "kyc_link_otp"


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _json(data: Dict[str, Any], status: int = 200) -> JsonResponse:
    return _no_store(JsonResponse(data, status=status))


def _body(request: HttpRequest) -> Dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _verified(request: HttpRequest, req: KycRequest) -> bool:
    """The OTP was entered in this browser for this exact link (a new link needs a new OTP)."""
    return bool(req.otp_verified_at) and req.token_hash in (request.session.get(SESSION_KEY) or [])


def _remember_verified(request: HttpRequest, req: KycRequest) -> None:
    done = [h for h in (request.session.get(SESSION_KEY) or []) if h != req.token_hash][-9:]
    request.session[SESSION_KEY] = done + [req.token_hash]
    request.session.cycle_key()  # a fresh session id once the customer has proved who they are


def _with_request(*, need_otp: bool, states=("open",)):
    """Resolve the token to a request in one of `states`; answer JSON errors for anything else."""

    def decorator(view):
        @wraps(view)
        def _wrapped(request: HttpRequest, token: str, *args, **kwargs):
            req = svc.find_by_token(token)
            if req is not None:
                svc.expire_if_due(req)
            state = svc.link_state(req)
            if state not in states:
                return _json({"error": "link_closed", "state": state,
                              "message": "This link can no longer be used. Please reload the page."}, status=410)
            if need_otp and not _verified(request, req):
                return _json({"error": "otp_required", "message": "Please verify the code we emailed you."},
                             status=403)
            return view(request, req, *args, **kwargs)

        return _wrapped

    return decorator


@ensure_csrf_cookie
@rate_limited("kyc-form", 60, 60)
@require_http_methods(["GET", "PUT"])
def form(request: HttpRequest, token: str) -> HttpResponse:
    if request.method == "PUT":
        return _save(request, token=token)
    req = svc.find_by_token(token)
    if req is not None:
        svc.expire_if_due(req)
    state = svc.link_state(req)
    actor = Actor.customer(client_ip(request))
    context: Dict[str, Any] = {"state": state, "req": req}
    if state in ("open", "confirm"):
        svc.mark_opened(req, actor)
        verified = _verified(request, req)
        context.update({
            "customer": req.customer,
            "verified": verified,
            "masked_email": svc.mask_email(req.recipient_email),
            "expires_at": req.expires_at,
            "urls": {
                "otpSend": reverse("kyc:link_otp_send", args=[token]),
                "otpVerify": reverse("kyc:link_otp_verify", args=[token]),
            },
        })
        if verified and state == "open":
            context["form_config"] = {
                **link_fields.client_config(),
                **svc.form_data(req, actor),
                "mode": "customer",
                "urls": {
                    "save": reverse("kyc:link_form", args=[token]),
                    "upload": reverse("kyc:link_documents", args=[token]),
                    "submit": reverse("kyc:link_submit", args=[token]),
                    "done": reverse("kyc:link_form", args=[token]),
                },
                "maxUploadMb": 5,
            }
        if verified and state == "confirm":
            context["summary"] = svc.confirm_summary(req)
            context["confirm_url"] = reverse("kyc:link_confirm", args=[token])
    elif state in AFTER_SUBMIT:
        verified = _verified(request, req)
        signed_doc = svc.signed_form(req)
        context.update({
            "customer": req.customer,
            "verified": verified,
            "masked_email": svc.mask_email(req.recipient_email),
            "signed_form": signed_doc,
            "pdf_url": reverse("kyc:link_pdf", args=[token]),
            "signed_url": reverse("kyc:link_signed", args=[token]),
            "urls": {
                "otpSend": reverse("kyc:link_otp_send", args=[token]),
                "otpVerify": reverse("kyc:link_otp_verify", args=[token]),
            },
        })
    elif req is not None and state == "confirmed":
        context["customer"] = req.customer
    status = 404 if state == "invalid" else 200
    return _no_store(render(request, "kyc/link/public.html", context, status=status))


@require_http_methods(["GET"])
def preview(request: HttpRequest, token: str) -> HttpResponse:
    """Old address of the all-steps preview: the form itself is now one page, so this goes to it."""
    return redirect("kyc:link_form", token=token)


@rate_limited("kyc-save", 120, 3600)
def _save(request: HttpRequest, token: str) -> HttpResponse:
    return _save_inner(request, token=token)


@_with_request(need_otp=True)
def _save_inner(request: HttpRequest, req: KycRequest) -> HttpResponse:
    try:
        errors = svc.save_draft(req, _body(request).get("values") or {}, Actor.customer(client_ip(request)))
    except KycLinkError as exc:
        return _json({"error": str(exc)}, status=409)
    details = svc.details_for(KycRequest.objects.get(pk=req.pk))
    return _json({"ok": not errors, "errors": errors, "savedAt": timezone.now().isoformat(),
                  "masked": {"pan": mask(details.pan_last4, 10), "account_number": mask(details.account_last4, 12)}})


OTP_STATES = ("open", "confirm", "submitted", "approved")
AFTER_SUBMIT = ("submitted", "approved")


@rate_limited("kyc-otp-send", 5, 900)
@require_POST
@_with_request(need_otp=False, states=OTP_STATES)
def otp_send(request: HttpRequest, req: KycRequest) -> HttpResponse:
    result = svc.send_otp(req, Actor.customer(client_ip(request)))
    data = {"ok": result.ok, "message": result.message}
    if result.test_code:
        data["testCode"] = result.test_code
    return _json(data, status=200 if result.ok else 429)


@rate_limited("kyc-otp-verify", 15, 900)
@require_POST
@_with_request(need_otp=False, states=OTP_STATES)
def otp_verify(request: HttpRequest, req: KycRequest) -> HttpResponse:
    result = svc.verify_otp(req, _body(request).get("code", ""), Actor.customer(client_ip(request)))
    if not result.ok:
        return _json({"ok": False, "message": result.message}, status=400)
    _remember_verified(request, req)
    return _json({"ok": True})


@rate_limited("kyc-upload", 30, 3600)
@require_POST
@_with_request(need_otp=True)
def documents(request: HttpRequest, req: KycRequest) -> HttpResponse:
    try:
        doc = svc.upload_document(req, request.POST.get("doc_type", ""), request.FILES.get("file"),
                                  Actor.customer(client_ip(request)))
    except KycLinkError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json({"ok": True, "document": {"name": doc.original_name, "size": doc.size}})


@rate_limited("kyc-submit", 10, 3600)
@require_POST
@_with_request(need_otp=True)
def submit(request: HttpRequest, req: KycRequest) -> HttpResponse:
    detail_url = request.build_absolute_uri(reverse("kyc:link_detail", args=[req.pk]))
    try:
        problems = svc.submit(req, bool(_body(request).get("consent")), Actor.customer(client_ip(request)),
                              detail_url)
    except KycLinkError as exc:
        return _json({"error": str(exc)}, status=409)
    if problems:
        return _json({"ok": False, "errors": problems}, status=400)
    # The OTP stays valid in this browser so the customer can download the PDF and upload the signed copy.
    return _json({"ok": True})


@rate_limited("kyc-pdf", 30, 3600)
@require_http_methods(["GET"])
@_with_request(need_otp=True, states=AFTER_SUBMIT)
def pdf(request: HttpRequest, req: KycRequest) -> HttpResponse:
    from .link_views import pdf_response

    return _no_store(pdf_response(req, Actor.customer(client_ip(request))))


@rate_limited("kyc-signed", 10, 3600)
@require_POST
@_with_request(need_otp=True, states=AFTER_SUBMIT)
def signed(request: HttpRequest, req: KycRequest) -> HttpResponse:
    try:
        doc = svc.upload_signed_form(req, request.FILES.get("file"), Actor.customer(client_ip(request)))
    except KycLinkError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json({"ok": True, "document": {"name": doc.original_name, "size": doc.size}})


@rate_limited("kyc-confirm", 10, 3600)
@require_POST
@_with_request(need_otp=True, states=("confirm",))
def confirm(request: HttpRequest, req: KycRequest) -> HttpResponse:
    try:
        svc.customer_confirm(req, Actor.customer(client_ip(request)))
    except KycLinkError as exc:
        return _json({"error": str(exc)}, status=409)
    return _json({"ok": True})


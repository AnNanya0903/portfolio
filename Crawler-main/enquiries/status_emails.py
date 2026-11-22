"""Automatic customer emails for an enquiry.

- rejection:        the order was rejected; asks for the reason, or thanks the customer if they gave one.
- payment_received: the payment was marked Paid (the order is now Converted).
- closing:          the 5th email of the automatic sequence (enquiries/auto_follow_up.py sends it, with the
                    same thread check and CC); it closes the enquiry ("No response") and makes it a
                    Marketing lead. send_closing_email() below only lets the board retry it.
There is no acknowledgement email any more.
A rejection makes the enquiry a Marketing lead (source "Rejected"), Ready at once; Marketing Follow-up 1
goes on the next hourly run, by itself.

Same safety rules as automatic follow-ups (enquiries/auto_follow_up.py):
- uses the FOLLOW_UP_AUTO_ENABLED switch, FOLLOW_UP_AUTO_DRY_RUN test mode and FOLLOW_UP_AUTO_SENDER mailbox;
- replies only in the enquiry's confirmed Gmail conversation, to the confirmed customer address;
- one email per enquiry and event (CustomerStatusEmail.event_key), even if the status is changed again.
If anything is unclear the email is not sent and the reason is shown on the enquiries board.
These functions never raise, so a status change is never blocked by an email problem.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from email.utils import parseaddr
from typing import Any, Callable, Dict, List, Mapping, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.follow_up_templates import (
    MARKETING_STAGE, PAYMENT_STAGE, document_for_stage, enquiry_closing_body, follow_up_count, sender_signature,
    to_html,
)
from dashboard.gmail_service import GmailAPIError, GmailService

from . import customer_emails
from .models import CustomerStatusEmail, FollowUpSent

logger = logging.getLogger(__name__)

# (paragraph, middle line, closing line) per email; {subject}, {reference} and {payment_detail} are filled in.
_TEMPLATES: Dict[str, tuple] = {
    "rejection_ask": (
        "Thank you for considering AVIGIRI for your requirement of {subject} (our reference: {reference}). "
        "We understand that you have decided not to proceed with this order.",
        "To help us serve you better, could you please let us know the main reason? For example: price, "
        "delivery time, specification, payment terms, or that you have chosen another supplier.",
        "Your feedback is very valuable to us, and we hope to work with you on a future requirement.",
    ),
    "rejection_thanks": (
        "Thank you for letting us know your decision on {subject} (our reference: {reference}), "
        "and for sharing your reason with us.",
        "We have noted your feedback and will use it to improve our offers.",
        "If anything changes, or for any new requirement, please reply to this email and we will be glad to assist.",
    ),
    "payment_received": (
        "Thank you for your payment for the order {subject} (our reference: {reference}){payment_detail}.",
        "We have received it, and your order is now confirmed for production. We will keep you updated "
        "on its progress.",
        "Thank you for choosing AVIGIRI.",
    ),
}


def build_status_email_body(template: str, *, reference: str, subject: str, user=None,
                            payment_detail: str = "") -> str:
    paragraph, middle, closing = _TEMPLATES[template]
    values = {
        "subject": (subject or "").strip() or "your enquiry",
        "reference": (reference or "").strip() or "-",
        "payment_detail": payment_detail,
    }
    return "\n\n".join([
        "Dear Sir/Madam,",
        paragraph.format(**values),
        middle,
        closing,
        sender_signature(user),
    ])


def payment_detail_text(paid_on: Optional[date], reference: str) -> str:
    """' (payment reference: UTR123, received on 01 Nov 2026)' or ''."""
    parts = []
    if (reference or "").strip():
        parts.append(f"payment reference: {reference.strip()}")
    if paid_on:
        parts.append(f"received on {paid_on.strftime('%d %b %Y')}")
    return f" ({', '.join(parts)})" if parts else ""


def _sender():
    username = (getattr(settings, "FOLLOW_UP_AUTO_SENDER", "") or "").strip()
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


def _record(enquiry_id: str, kind: str, event_key: str) -> Optional[CustomerStatusEmail]:
    """Claim this event. Returns None when it was already sent (or is being sent) for this enquiry."""
    try:
        with transaction.atomic():
            return CustomerStatusEmail.objects.create(enquiry_id=enquiry_id, kind=kind, event_key=event_key)
    except IntegrityError:
        pass
    with transaction.atomic():
        row = CustomerStatusEmail.objects.select_for_update().get(enquiry_id=enquiry_id, event_key=event_key)
        if row.status in (CustomerStatusEmail.STATUS_SENT, CustomerStatusEmail.STATUS_PENDING):
            return None
        # Not sent last time (skipped, failed or test mode): try again.
        row.status = CustomerStatusEmail.STATUS_PENDING
        row.note = ""
        row.save(update_fields=["status", "note", "updated_at"])
        return row


def _finish(row: CustomerStatusEmail, status: str, note: str = "", **fields: Any) -> Dict[str, Any]:
    row.status = status
    row.note = (note or "")[:500]
    for key, value in fields.items():
        setattr(row, key, value)
    row.save()
    if status != CustomerStatusEmail.STATUS_SENT:
        logger.info("Status email %s for enquiry %s: %s %s", row.event_key, row.enquiry_id, status, note)
    return {"enquiry_id": row.enquiry_id, "event": row.event_key, "result": status, "note": row.note}


def _sender_address(message: Mapping[str, Any]) -> str:
    return parseaddr(str(message.get("sender") or ""))[1].strip().lower()


def _customer_has_not_replied(thread: List[Mapping[str, Any]], own: set) -> str:
    """Closing: skip when the customer wrote the latest message."""
    if thread and _sender_address(thread[-1]) not in own:
        return "the customer replied; please answer them instead of closing"
    return ""


_THREAD_CHECKS: Dict[str, Callable[[List[Mapping[str, Any]], set], str]] = {
    CustomerStatusEmail.KIND_CLOSING: _customer_has_not_replied,
}


def send_status_email(kind: str, enquiry_id: str, enquiry: Mapping[str, Any], *, reason: str = "",
                      payment_detail: str = "") -> Dict[str, Any]:
    """Send the automatic email for `kind`. `reason` = rejection reason the customer already gave."""
    from .views import _resolve_follow_up_conversation, _saved_customer_email  # views imports a lot

    enquiry_id = str(enquiry_id)
    if kind == CustomerStatusEmail.KIND_REJECTION:
        # Lost: Marketing's lead is Ready at once and its Follow-up 1 goes on the next hourly run.
        mark_marketing_ready(enquiry_id, enquiry)
    row = _record(enquiry_id, kind, kind)
    if row is None:
        return {"enquiry_id": enquiry_id, "event": kind, "result": "already_sent", "note": ""}

    try:
        if not getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False):
            return _finish(row, CustomerStatusEmail.STATUS_SKIPPED, "automatic emails are turned off on this site")
        sender = _sender()
        if sender is None:
            return _finish(row, CustomerStatusEmail.STATUS_FAILED, "FOLLOW_UP_AUTO_SENDER is not set to an active user")

        enquiry = dict(enquiry or {})
        if not str(enquiry.get("customer_email") or "").strip():
            saved = _saved_customer_email(enquiry_id)
            if saved:
                enquiry["customer_email"] = saved

        gmail = GmailService(sender)
        outcome, result = _resolve_follow_up_conversation(enquiry_id, enquiry, gmail)
        if outcome != "ok":
            return _finish(row, CustomerStatusEmail.STATUS_SKIPPED,
                           "no confirmed Gmail conversation for this enquiry; please email the customer manually")
        conversation = result
        customer_email = str(conversation.get("customer_email") or "").strip().lower()
        reply_to_id = str(conversation.get("latest_message_id") or "").strip()
        thread_id = str(conversation.get("thread_id") or "")
        own = {a.lower() for a in gmail.own_addresses()}
        if not customer_email or "@" not in customer_email or customer_email in own or not reply_to_id:
            return _finish(row, CustomerStatusEmail.STATUS_SKIPPED,
                           "no confirmed customer email address; please email the customer manually")

        check = _THREAD_CHECKS.get(kind)
        if check is not None:
            thread = gmail.get_thread_messages(thread_id, limit=50) if thread_id else []
            if not own:
                return _finish(row, CustomerStatusEmail.STATUS_SKIPPED, "could not confirm the sending account's address")
            stop = check(thread or [], own)
            if stop:
                return _finish(row, CustomerStatusEmail.STATUS_SKIPPED, stop)

        html_body = None
        if kind == CustomerStatusEmail.KIND_ACKNOWLEDGEMENT:
            return _finish(row, CustomerStatusEmail.STATUS_SKIPPED, "the acknowledgement email is no longer sent")
        if kind == CustomerStatusEmail.KIND_CLOSING:
            from .auto_follow_up import customer_name_for

            body = enquiry_closing_body(customer_name=customer_name_for(enquiry_id, enquiry, customer_email),
                                        document=_closing_document(enquiry_id))
            html_body = to_html(body)
        else:
            template = ("rejection_thanks" if reason.strip() else "rejection_ask") \
                if kind == CustomerStatusEmail.KIND_REJECTION else kind
            body = build_status_email_body(template, reference=enquiry_id,
                                           subject=str(enquiry.get("subject") or ""), user=sender,
                                           payment_detail=payment_detail)
        original = gmail.get_message(reply_to_id)
        subject = (original or {}).get("subject") or str(enquiry.get("subject") or "")
        fields = {"customer_email": customer_email, "subject": subject[:500], "body": body,
                  "gmail_thread_id": thread_id}

        if getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True):
            logger.info("Status email %s dry run for enquiry %s to %s. Nothing was sent.\nSubject: %s\n%s",
                        kind, enquiry_id, customer_email, subject, body)
            return _finish(row, CustomerStatusEmail.STATUS_DRY_RUN, "test mode: nothing was sent", **fields)

        cc = customer_emails.with_customer_cc(enquiry_id, [], to=customer_email, own=own)  # other customer emails
        sent = gmail.send_reply(original_message=original, body=body, subject=subject, to=customer_email,
                                cc=cc or None, html_body=html_body)
        fields["gmail_message_id"] = (sent or {}).get("id") or ""
        fields["gmail_thread_id"] = (sent or {}).get("threadId") or fields["gmail_thread_id"]
        return _finish(row, CustomerStatusEmail.STATUS_SENT, "", sent_at=timezone.now(), **fields)
    except GmailAPIError as exc:
        return _finish(row, CustomerStatusEmail.STATUS_FAILED, f"Gmail error: {exc}")
    except Exception as exc:  # never block the status change
        logger.exception("Status email %s failed for enquiry %s", kind, enquiry_id)
        return _finish(row, CustomerStatusEmail.STATUS_FAILED, f"send failed: {exc}")


def _closing_document(enquiry_id: str) -> str:
    """"quotation" or "proforma invoice": what the follow-ups being closed were about."""
    from . import auto_follow_up

    cycle = auto_follow_up.cycles([str(enquiry_id)], include_marketing=False).get(str(enquiry_id))
    return document_for_stage(cycle.stage if cycle else "")


def latest_status_emails(enquiry_ids) -> Dict[str, Dict[str, CustomerStatusEmail]]:
    """{enquiry_id: {kind: latest row}} for the board."""
    result: Dict[str, Dict[str, CustomerStatusEmail]] = {}
    for row in CustomerStatusEmail.objects.filter(enquiry_id__in=list(enquiry_ids)).order_by("created_at"):
        result.setdefault(row.enquiry_id, {})[row.kind] = row
    return result


# ------------------------------------------------------------------------------------------
# Scheduled run (enquiries/scheduler.py calls this every hour)
# ------------------------------------------------------------------------------------------

def _status_of(enquiry: Mapping[str, Any]) -> str:
    return str(enquiry.get("status") or "").strip().lower()


def send_closing_email(enquiry: Mapping[str, Any], api_root: str) -> Dict[str, Any]:
    """Board retry of the closing email: runs the automatic sequence for this enquiry when its next email is
    the closing email (same thread check, CC, and move to Marketing)."""
    from . import auto_follow_up

    enquiry_id = str(enquiry.get("enquiry_id"))
    cycle = auto_follow_up.cycles([enquiry_id], include_marketing=False).get(enquiry_id)
    if cycle is None or cycle.count != follow_up_count(cycle.stage):
        return {"enquiry_id": enquiry_id, "event": CustomerStatusEmail.KIND_CLOSING,
                "result": CustomerStatusEmail.STATUS_SKIPPED, "note": "the closing email follows Follow-up 4"}
    outcome = auto_follow_up.run_auto_follow_up(enquiry_id)
    status = {auto_follow_up.SENT: CustomerStatusEmail.STATUS_SENT,
              auto_follow_up.DRY_RUN: CustomerStatusEmail.STATUS_DRY_RUN,
              auto_follow_up.FAILED: CustomerStatusEmail.STATUS_FAILED}.get(outcome.get("result"),
                                                                           CustomerStatusEmail.STATUS_SKIPPED)
    return {"enquiry_id": enquiry_id, "event": CustomerStatusEmail.KIND_CLOSING, "result": status,
            "note": outcome.get("reason") or "", "closed": status in (CustomerStatusEmail.STATUS_SENT,
                                                                      CustomerStatusEmail.STATUS_DRY_RUN)}


def mark_marketing_ready(enquiry_id: str, enquiry: Optional[Mapping[str, Any]] = None) -> None:
    """The enquiry was rejected: its Marketing lead (source Rejected) is Ready now and Follow-up 1 goes on the
    next hourly run. Never raises."""
    try:
        from marketing import lost
    except Exception:  # Marketing app not installed
        return
    try:
        from . import auto_follow_up

        lost.create_lead(enquiry_id, lost.REJECTED,
                         customer_email=auto_follow_up.customer_email(str(enquiry_id), enquiry),
                         first_due=timezone.now())
    except Exception:
        logger.exception("Could not create the Marketing lead of %s", enquiry_id)


def run_scheduled(api_root: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Nothing to do any more: the closing email is the 5th email of the automatic sequence."""
    return {"closing": []}

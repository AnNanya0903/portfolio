"""Automatic customer emails for an enquiry.

- acknowledgement:  a new enquiry was received (sent by the scheduled run for enquiries received recently).
- rejection:        the order was rejected; asks for the reason, or thanks the customer if they gave one.
- payment_received: the payment was marked Paid (the order is now Converted).
- closing:          all follow-ups of the quotation / proforma invoice were sent and the customer never
                    replied; the enquiry is then closed.
A sent rejection email starts Marketing's automatic follow-ups in the same thread (auto_follow_up.py).

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

from dashboard.follow_up_templates import MARKETING_STAGE, PAYMENT_STAGE, follow_up_count, sender_signature
from dashboard.gmail_service import GmailAPIError, GmailService

from . import customer_emails
from .models import CustomerStatusEmail, FollowUpSent

logger = logging.getLogger(__name__)

# (paragraph, middle line, closing line) per email; {subject}, {reference} and {payment_detail} are filled in.
_TEMPLATES: Dict[str, tuple] = {
    "acknowledgement": (
        "Thank you for your enquiry about {subject}. We have registered it with our reference {reference}.",
        "Our team is now reviewing your requirement and checking its feasibility. We will share our "
        "quotation with you shortly.",
        "If you have drawings or any further details, please reply to this email.",
    ),
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
    "closing": (
        "We have followed up several times regarding your enquiry about {subject} (our reference: {reference}), "
        "but we have not received a response.",
        "We are therefore closing this enquiry for now.",
        "For any new enquiry or requirement, please contact the AVIGIRI team. You can simply reply to this "
        "email, and we will be glad to assist.",
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


def _no_team_reply_yet(thread: List[Mapping[str, Any]], own: set) -> str:
    """Acknowledgement: skip when the team has already written to the customer in this conversation."""
    if any(_sender_address(m) in own for m in thread):
        return "the team has already replied to this enquiry"
    return ""


def _customer_has_not_replied(thread: List[Mapping[str, Any]], own: set) -> str:
    """Closing: skip when the customer wrote the latest message."""
    if thread and _sender_address(thread[-1]) not in own:
        return "the customer replied; please answer them instead of closing"
    return ""


_THREAD_CHECKS: Dict[str, Callable[[List[Mapping[str, Any]], set], str]] = {
    CustomerStatusEmail.KIND_ACKNOWLEDGEMENT: _no_team_reply_yet,
    CustomerStatusEmail.KIND_CLOSING: _customer_has_not_replied,
}


def send_status_email(kind: str, enquiry_id: str, enquiry: Mapping[str, Any], *, reason: str = "",
                      payment_detail: str = "") -> Dict[str, Any]:
    """Send the automatic email for `kind`. `reason` = rejection reason the customer already gave."""
    from .views import _resolve_follow_up_conversation, _saved_customer_email  # views imports a lot

    enquiry_id = str(enquiry_id)
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

        if kind == CustomerStatusEmail.KIND_REJECTION:
            template = "rejection_thanks" if reason.strip() else "rejection_ask"
        else:
            template = kind
        body = build_status_email_body(template, reference=enquiry_id, subject=str(enquiry.get("subject") or ""),
                                       user=sender, payment_detail=payment_detail)
        original = gmail.get_message(reply_to_id)
        subject = (original or {}).get("subject") or str(enquiry.get("subject") or "")
        fields = {"customer_email": customer_email, "subject": subject[:500], "body": body,
                  "gmail_thread_id": thread_id}

        if getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True):
            return _finish(row, CustomerStatusEmail.STATUS_DRY_RUN, "test mode: nothing was sent", **fields)

        cc = customer_emails.with_customer_cc(enquiry_id, [], to=customer_email, own=own)  # other customer emails
        sent = gmail.send_reply(original_message=original, body=body, subject=subject, to=customer_email,
                                cc=cc or None)
        fields["gmail_message_id"] = (sent or {}).get("id") or ""
        fields["gmail_thread_id"] = (sent or {}).get("threadId") or fields["gmail_thread_id"]
        if kind == CustomerStatusEmail.KIND_REJECTION and getattr(settings, "MARKETING_AUTO_FOLLOW_UP", True):
            # Marketing follows up from here, replying in this same thread.
            from . import auto_follow_up
            auto_follow_up.record_start(enquiry_id, MARKETING_STAGE,
                                        sent={**(sent or {}), "threadId": fields["gmail_thread_id"]},
                                        customer_email=customer_email, cc=cc, subject=subject, user=sender)
        return _finish(row, CustomerStatusEmail.STATUS_SENT, "", sent_at=timezone.now(), **fields)
    except GmailAPIError as exc:
        return _finish(row, CustomerStatusEmail.STATUS_FAILED, f"Gmail error: {exc}")
    except Exception as exc:  # never block the status change
        logger.exception("Status email %s failed for enquiry %s", kind, enquiry_id)
        return _finish(row, CustomerStatusEmail.STATUS_FAILED, f"send failed: {exc}")


def latest_status_emails(enquiry_ids) -> Dict[str, Dict[str, CustomerStatusEmail]]:
    """{enquiry_id: {kind: latest row}} for the board."""
    result: Dict[str, Dict[str, CustomerStatusEmail]] = {}
    for row in CustomerStatusEmail.objects.filter(enquiry_id__in=list(enquiry_ids)).order_by("created_at"):
        result.setdefault(row.enquiry_id, {})[row.kind] = row
    return result


# ------------------------------------------------------------------------------------------
# Scheduled run (n8n calls POST enquiries/api/customer-emails/run/ every hour or so)
# ------------------------------------------------------------------------------------------

def _status_of(enquiry: Mapping[str, Any]) -> str:
    return str(enquiry.get("status") or "").strip().lower()


def acknowledgement_due(enquiries: List[Mapping[str, Any]], today: date) -> List[Mapping[str, Any]]:
    """Open enquiries received in the last FOLLOW_UP_ACK_WITHIN_DAYS days with no acknowledgement yet.

    The short window means old enquiries are never acknowledged when this is first turned on.
    """
    within = int(getattr(settings, "FOLLOW_UP_ACK_WITHIN_DAYS", 2) or 0)
    if within <= 0:
        return []
    since = today - timedelta(days=within)
    done = set(CustomerStatusEmail.objects.filter(
        kind=CustomerStatusEmail.KIND_ACKNOWLEDGEMENT,
        status__in=(CustomerStatusEmail.STATUS_SENT, CustomerStatusEmail.STATUS_SKIPPED),
    ).values_list("enquiry_id", flat=True))
    due = []
    for enquiry in enquiries:
        received = enquiry.get("received_at_dt")
        received_day = received.date() if hasattr(received, "date") else received
        if (_status_of(enquiry) == "open" and received_day and since <= received_day <= today
                and str(enquiry.get("enquiry_id")) not in done):
            due.append(enquiry)
    return due


def closing_due(enquiries: List[Mapping[str, Any]], now=None) -> List[Mapping[str, Any]]:
    """Enquiries whose quotation / PI follow-ups are all sent, the last one FOLLOW_UP_CLOSE_AFTER_DAYS ago or more.

    Open enquiries (quotation or general follow-ups), and proforma invoices still unpaid (open or accepted,
    not inside an agreed credit period). Marketing sends its own closing email.
    """
    from . import auto_follow_up, payment_terms
    from .models import EnquiryContact

    now = now or timezone.now()
    wait = int(getattr(settings, "FOLLOW_UP_CLOSE_AFTER_DAYS", 2) or 0)
    if wait <= 0:
        return []
    candidates = {str(e.get("enquiry_id")): e for e in enquiries if _status_of(e) in ("open", "accepted")}
    if not candidates:
        return []
    already = set(CustomerStatusEmail.objects.filter(
        kind=CustomerStatusEmail.KIND_CLOSING, status=CustomerStatusEmail.STATUS_SENT
    ).values_list("enquiry_id", flat=True))
    paid = set(EnquiryContact.objects.filter(enquiry_id__in=list(candidates),
                                             payment_status=EnquiryContact.PAYMENT_PAID)
               .values_list("enquiry_id", flat=True))
    due = []
    for enquiry_id, cycle in sorted(auto_follow_up.cycles(list(candidates)).items()):
        limit = follow_up_count(cycle.stage)
        if cycle.stage == MARKETING_STAGE or limit <= 0 or enquiry_id in already:
            continue
        if cycle.count < limit or not cycle.last_sent_at or cycle.last_sent_at > now - timedelta(days=wait):
            continue
        if _status_of(candidates[enquiry_id]) == "accepted":
            # An accepted order is only closed when its proforma invoice was never paid.
            if (cycle.stage != PAYMENT_STAGE or enquiry_id in paid
                    or payment_terms.credit_hold_until(enquiry_id, timezone.localdate(now))):
                continue
        due.append(candidates[enquiry_id])
    return due


def send_closing_email(enquiry: Mapping[str, Any], api_root: str) -> Dict[str, Any]:
    """Send the closing email; once it is really sent, set the enquiry status to Closed."""
    from . import services

    enquiry_id = str(enquiry.get("enquiry_id"))
    outcome = send_status_email(CustomerStatusEmail.KIND_CLOSING, enquiry_id, enquiry)
    if outcome.get("result") == CustomerStatusEmail.STATUS_SENT:
        try:
            services.update_enquiry(api_root, enquiry_id, {"status": "closed"})
            outcome["closed"] = True
            from . import team_alerts
            team_alerts.notify_team(
                None, f"Enquiry closed, no reply: {team_alerts.enquiry_label(enquiry_id, enquiry)}",
                [f"All follow-ups for enquiry {enquiry_id} went unanswered, so the closing email was sent and the "
                 "enquiry is now Closed. Marketing continues from here."],
                once_key=f"closed:{enquiry_id}:{timezone.localdate().isoformat()}")
        except services.EnquiryServiceError as exc:
            outcome["closed"] = False
            outcome["note"] = f"email sent but the status could not be set to Closed: {exc}"
            logger.warning("Closing email sent for %s but status update failed: %s", enquiry_id, exc)
    return outcome


def run_scheduled(api_root: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Acknowledge new enquiries and close enquiries that never replied. Returns what happened."""
    from . import services

    today = today or timezone.localdate()
    enquiries = services.list_enquiries(api_root)
    results = {"acknowledgement": [], "closing": []}
    for enquiry in acknowledgement_due(enquiries, today):
        results["acknowledgement"].append(
            send_status_email(CustomerStatusEmail.KIND_ACKNOWLEDGEMENT, str(enquiry.get("enquiry_id")), enquiry))
    for enquiry in closing_due(enquiries):
        results["closing"].append(send_closing_email(enquiry, api_root))
    return results

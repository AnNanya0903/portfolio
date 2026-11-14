"""Stage 2: automatic enquiry follow-ups.

n8n calls two endpoints on a schedule (see enquiries/api_views.py):
  1. due_follow_ups()        -> which enquiries are due today
  2. run_auto_follow_up(id)  -> check the rules again and send one follow-up

Automatic follow-ups only continue a conversation a person already confirmed: the first
follow-up of every enquiry is sent manually (Stage 1), which stores the Gmail thread and the
customer address. When anything is unclear the enquiry is skipped with a reason, never guessed.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.follow_up_templates import DEFAULT_STAGE, PAYMENT_STAGE, STAGE_KEYS, build_follow_up_body
from dashboard.gmail_service import GmailAPIError, GmailService
from kyc import routing as kyc_routing

from . import customer_emails, payment_terms, services
from .models import EnquiryContact, FollowUpSent

logger = logging.getLogger(__name__)

SENT, SKIPPED, FAILED, DRY_RUN = "sent", "skipped", "failed", "dry_run"


def _api_root() -> str:
    return getattr(settings, "ENQUIRY_API_ROOT", "http://localhost:8405").rstrip("/")


def _result(enquiry_id: str, result: str, reason: str = "", **extra: Any) -> Dict[str, Any]:
    return {"enquiry_id": str(enquiry_id), "result": result, "reason": reason, **extra}


def _valid_email(value: str) -> bool:
    try:
        validate_email(value or "")
    except ValidationError:
        return False
    return True


def next_follow_up_date(attempt_sent: int, today: date) -> Optional[date]:
    """When to send the next follow-up after follow-up number `attempt_sent`; None after the last one."""
    limit = int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
    if limit and attempt_sent >= limit:
        return None
    gaps = list(getattr(settings, "FOLLOW_UP_AUTO_GAP_DAYS", []) or [7])
    gap = gaps[attempt_sent - 1] if 0 < attempt_sent <= len(gaps) else gaps[-1]
    return today + timedelta(days=gap)


def _history(enquiry_ids: Optional[List[str]] = None) -> Dict[str, Tuple[FollowUpSent, int]]:
    """Latest sent follow-up and number of sent follow-ups per enquiry."""
    rows = FollowUpSent.objects.filter(status=FollowUpSent.STATUS_SENT)
    if enquiry_ids is not None:
        rows = rows.filter(enquiry_id__in=enquiry_ids)
    history: Dict[str, Tuple[FollowUpSent, int]] = {}
    for row in rows.order_by("enquiry_id", "-sent_at"):
        if row.enquiry_id in history:
            latest, count = history[row.enquiry_id]
            history[row.enquiry_id] = (latest, count + 1)
        else:
            history[row.enquiry_id] = (row, 1)
    return history


def _skip_reason(enquiry_id: str, enquiry: Optional[Dict[str, Any]], latest: FollowUpSent, count: int,
                 today: date) -> str:
    """Why this enquiry must not get an automatic follow-up today, or '' if it may."""
    from .views import _follow_up_stopped_reason  # local import: views imports a lot

    if enquiry is None:
        return "enquiry not found"
    stopped = _follow_up_stopped_reason({**enquiry, "enquiry_id": enquiry_id}, count)
    if stopped:
        return stopped
    accepted = str(enquiry.get("status") or "").strip().lower() == "accepted"
    if accepted and latest.stage != PAYMENT_STAGE:
        return "order accepted; send the first payment follow-up manually"
    if accepted:
        hold = payment_terms.credit_hold_until(enquiry_id, today)
        if hold:
            return f"within the credit period; payment is due on {hold.isoformat()}"
    if EnquiryContact.objects.filter(enquiry_id=enquiry_id, auto_follow_up_paused=True).exists():
        return "automatic follow-ups are paused for this enquiry"
    if not latest.next_follow_up_on:
        return "no next follow-up date set"
    if latest.next_follow_up_on > today:
        return f"not due until {latest.next_follow_up_on.isoformat()}"
    if not latest.gmail_thread_id:
        return "no confirmed Gmail conversation; send the first follow-up manually"
    if not _valid_email(latest.customer_email):
        return "no confirmed customer email"
    return ""


def due_follow_ups(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Enquiries whose next follow-up date has arrived and that pass every rule."""
    today = today or timezone.localdate()
    history = {
        enquiry_id: item for enquiry_id, item in _history().items()
        if item[0].next_follow_up_on and item[0].next_follow_up_on <= today
    }
    if not history:
        return []
    enquiries = {str(e.get("enquiry_id")): e for e in services.list_enquiries(_api_root())}
    due = []
    for enquiry_id, (latest, count) in sorted(history.items()):
        if _skip_reason(enquiry_id, enquiries.get(enquiry_id), latest, count, today):
            continue
        due.append({
            "enquiry_id": enquiry_id,
            "customer_email": latest.customer_email,
            "attempt": count + 1,
            "stage": latest.stage or DEFAULT_STAGE,
            "due": latest.next_follow_up_on.isoformat(),
        })
    return due


def _sender():
    username = (getattr(settings, "FOLLOW_UP_AUTO_SENDER", "") or "").strip()
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


def run_auto_follow_up(enquiry_id: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Check the rules again and send one automatic follow-up. Never raises for expected problems."""
    enquiry_id = str(enquiry_id)
    today = today or timezone.localdate()

    if not getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False):
        return _result(enquiry_id, SKIPPED, "automatic follow-ups are turned off")
    sender = _sender()
    if sender is None:
        return _result(enquiry_id, FAILED, "FOLLOW_UP_AUTO_SENDER is not set to an active user")

    latest_and_count = _history([enquiry_id]).get(enquiry_id)
    if latest_and_count is None:
        return _result(enquiry_id, SKIPPED, "no follow-up has been sent manually yet")

    try:
        enquiry = services.fetch_enquiry(_api_root(), enquiry_id)
    except services.EnquiryServiceError as exc:
        return _result(enquiry_id, FAILED, f"could not load enquiry: {exc}")

    latest, count = latest_and_count
    reason = _skip_reason(enquiry_id, enquiry, latest, count, today)
    if reason:
        return _result(enquiry_id, SKIPPED, reason)

    stage = latest.stage if latest.stage in STAGE_KEYS else DEFAULT_STAGE
    if stage == PAYMENT_STAGE:  # payment reminders are numbered on their own
        count = FollowUpSent.objects.filter(
            enquiry_id=enquiry_id, status=FollowUpSent.STATUS_SENT, stage=PAYMENT_STAGE
        ).count()
    return _send(enquiry_id, enquiry, sender, today, thread_id=latest.gmail_thread_id,
                 customer_email=latest.customer_email, stage=stage, attempt=count + 1, cc=latest.cc)


def _send(enquiry_id: str, enquiry: Dict[str, Any], sender, today: date, *, thread_id: str,
          customer_email: str, stage: str, attempt: int, cc: str = "") -> Dict[str, Any]:
    """Reply in the conversation unless the customer wrote last; record the follow-up."""
    if not thread_id:
        return _result(enquiry_id, SKIPPED, "no confirmed Gmail conversation")
    gmail = GmailService(sender)
    try:
        own = {a.lower() for a in gmail.own_addresses()}
        if customer_email.lower() in own:
            return _result(enquiry_id, SKIPPED, "customer address is one of our own addresses")
        thread = gmail.get_thread_messages(thread_id, limit=50)
        if not thread:
            return _result(enquiry_id, SKIPPED, "Gmail conversation not found")
        last = thread[-1]
        last_sender = parseaddr(last.get("sender") or "")[1].strip().lower()
        if not own:
            return _result(enquiry_id, SKIPPED, "could not confirm the sending account's address")
        if last_sender and last_sender not in own:
            return _result(enquiry_id, SKIPPED, "customer replied; answer them manually")
        original = gmail.get_message(last.get("id"))
    except GmailAPIError as exc:
        return _result(enquiry_id, FAILED, f"Gmail error: {exc}")

    due_on, credit_days = payment_terms.payment_terms_for(enquiry_id) if stage == PAYMENT_STAGE else (None, None)
    body = build_follow_up_body(
        stage, attempt=attempt, reference=enquiry_id,
        subject=str(enquiry.get("subject") or ""), user=sender,
        due_on=due_on, credit_days=credit_days, today=today,
    )
    # The other customer emails, then the same CC as the last follow-up; never the customer or our own addresses.
    cc_list = customer_emails.with_customer_cc(
        enquiry_id, payment_terms.parse_cc(cc, exclude=customer_email)[0], to=customer_email, own=own,
        limit=payment_terms.MAX_CC)
    # Linked to a KYC customer: copy the team this stage concerns, and top management once it has escalated.
    cc_list = kyc_routing.add_cc(cc_list, enquiry_id, stage, attempt, to=customer_email, own=own,
                                 limit=payment_terms.MAX_CC)
    subject = original.get("subject") or str(enquiry.get("subject") or "")
    next_on = next_follow_up_date(attempt, today)
    details = {
        "customer_email": customer_email,
        "attempt": attempt,
        "stage": stage,
        "next_follow_up_on": next_on.isoformat() if next_on else None,
        "cc": cc_list,
    }

    if getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True):
        logger.info("Auto follow-up dry run for enquiry %s (attempt %s) to %s", enquiry_id, attempt,
                    customer_email)
        return _result(enquiry_id, DRY_RUN, "dry run: nothing was sent", **details)

    # The unique token means one automatic follow-up per enquiry per day, even if n8n retries.
    try:
        with transaction.atomic():
            record = FollowUpSent.objects.create(
                enquiry_id=enquiry_id,
                customer_email=customer_email,
                customer_name=str(enquiry.get("customer_name") or "").strip(),
                subject=subject,
                body=body,
                gmail_message_id=original.get("id") or "",
                gmail_thread_id=thread_id,
                sent_by=sender,
                status=FollowUpSent.STATUS_PENDING,
                submission_token=f"auto:{enquiry_id}:{today.isoformat()}",
                stage=stage,
                next_follow_up_on=next_on,
                source=FollowUpSent.SOURCE_AUTO,
                cc=", ".join(cc_list),
            )
    except IntegrityError:
        return _result(enquiry_id, SKIPPED, "already handled today")

    try:
        sent = gmail.send_reply(original_message=original, body=body, subject=subject, to=customer_email,
                                cc=cc_list or None)
    except Exception as exc:  # Gmail API or network failure
        logger.warning("Auto follow-up failed for enquiry %s: %s", enquiry_id, exc)
        record.status = FollowUpSent.STATUS_FAILED
        record.error_message = str(exc)[:1000]
        record.save(update_fields=["status", "error_message"])
        return _result(enquiry_id, FAILED, f"send failed: {exc}", **details)

    record.status = FollowUpSent.STATUS_SENT
    record.gmail_message_id = (sent or {}).get("id") or record.gmail_message_id
    record.gmail_thread_id = (sent or {}).get("threadId") or record.gmail_thread_id
    record.sent_at = timezone.now()
    record.save(update_fields=["status", "gmail_message_id", "gmail_thread_id", "sent_at"])
    return _result(enquiry_id, SENT, "", **details)

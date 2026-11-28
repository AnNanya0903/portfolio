"""Automatic follow-ups, for enquiries and for Marketing. Nobody presses a button.

The scheduler (enquiries/scheduler.py, run hourly by App Engine cron) calls:
  1. detect_gmail_quotes()   -> starts the sequence of a quotation / PI sent straight from Gmail
  2. due_follow_ups()        -> which sequences have an email due
  3. run_auto_follow_up(id)  -> check every rule again and send that one email

Enquiry sequence (Open enquiries, quotation or proforma invoice sent): Follow-up 1-4 every
FOLLOW_UP_AUTO_GAP_DAYS (the first that long after the quotation / PI), then the closing email (the 5th email)
FOLLOW_UP_CLOSE_AFTER_DAYS after Follow-up 4. It starts the moment the quotation / PI is emailed from the portal
(record_start) or when the scheduler finds it in our sent Gmail. A revised quotation does not restart it; every
follow-up carries the share link of the latest revision. If the customer never responds, the closing email sets
the enquiry to Closed ("No response") and makes it a Marketing lead.

Marketing sequence (lead Rejected, or No response): Follow-up 1 opens a new thread, Follow-up 2-4 and the
closing email reply in it.

Before every email (automatic or manual) the Gmail thread is verified (enquiries/thread_check.py). When no
thread passes, nothing is sent and the enquiry / lead shows "Gmail thread not found"; it is re-checked every run.
Every email has the fixed CC list (ENQUIRY_FOLLOW_UP_CC / MARKETING_FOLLOW_UP_CC). A customer reply,
share-link answer or logged contact stops the sequence; replies from our own staff never do. No customer gets
two emails within FOLLOW_UP_MIN_GAP_HOURS; a failed send is retried FOLLOW_UP_SEND_RETRIES times.
FOLLOW_UP_AUTO_DRY_RUN: everything is rendered and logged (To, CC, subject, thread, body) and nothing is sent,
but the sequence moves on as if it had been, so enquiries still close and Marketing leads are still created.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.follow_up_templates import (
    DEFAULT_STAGE, MARKETING_STAGE, PAYMENT_STAGE, QUOTATION_STAGE, STAGE_KEYS, build_follow_up_body,
    logo_inline_images,
    document_for_stage, enquiry_closing_body, follow_up_count, marketing_subject, to_html,
)
from dashboard.gmail_service import GmailAPIError, GmailService

from . import follow_up_links, services, thread_check
from .models import CustomerStatusEmail, EnquiryContact, EnquiryFollowup, FollowUpNote, FollowUpSent, FollowUpStart

logger = logging.getLogger(__name__)

SENT, SKIPPED, FAILED, DRY_RUN = "sent", "skipped", "failed", "dry_run"
THREAD_NOT_FOUND = "Gmail thread not found — check customer email / subject"


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


def dry_run() -> bool:
    return bool(getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True))


def counted_statuses() -> List[str]:
    """Rows that count as sent: really sent, and in dry-run mode also the dry-run rows (so the sequence moves on)."""
    return [FollowUpSent.STATUS_SENT] + ([FollowUpSent.STATUS_DRY_RUN] if dry_run() else [])


# ---------------------------------------------------------------------------------------------------------
# CC lists
# ---------------------------------------------------------------------------------------------------------

def cc_for(stage: str, to: str = "") -> List[str]:
    """The fixed CC list of the portal the email belongs to, without duplicates or the address in To."""
    raw = getattr(settings, "MARKETING_FOLLOW_UP_CC" if stage == MARKETING_STAGE else "ENQUIRY_FOLLOW_UP_CC", "")
    to = (to or "").strip().lower()
    out: List[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        address = part.strip().lower()
        if address and address != to and address not in out and _valid_email(address):
            out.append(address)
    return out


# ---------------------------------------------------------------------------------------------------------
# Where each sequence stands
# ---------------------------------------------------------------------------------------------------------

def _gap(after_attempt: int) -> int:
    """Days to wait after email number `after_attempt` (0 = after the quotation / PI). FOLLOW_UP_AUTO_GAP_DAYS lists
    the waits; its last value repeats."""
    gaps = list(getattr(settings, "FOLLOW_UP_AUTO_GAP_DAYS", []) or [2])
    index = max(after_attempt - 1, 0)
    return gaps[index] if index < len(gaps) else gaps[-1]


def sequence_length(stage: str) -> int:
    """Follow-ups plus the closing email."""
    return follow_up_count(stage) + 1


def next_follow_up_date(attempt_sent: int, today: date, stage: str = "") -> Optional[date]:
    """When the next email is due after email number `attempt_sent`; None after the closing email."""
    if attempt_sent >= sequence_length(stage):
        return None
    if attempt_sent == follow_up_count(stage) and stage != MARKETING_STAGE:
        return today + timedelta(days=int(getattr(settings, "FOLLOW_UP_CLOSE_AFTER_DAYS", 2) or 0))
    return today + timedelta(days=_gap(attempt_sent))


class Cycle:
    """Where a sequence stands: stage, how many emails went, the next due date and the thread."""

    def __init__(self, *, stage: str, count: int, next_on: Optional[date], thread_id: str, customer_email: str,
                 cc: str, last_sent_at, latest: Optional[FollowUpSent] = None,
                 start: Optional[FollowUpStart] = None, rows: Optional[List[FollowUpSent]] = None):
        self.stage = stage if stage in STAGE_KEYS else DEFAULT_STAGE
        self.count = count
        self.next_on = next_on
        self.thread_id = thread_id or ""
        self.customer_email = customer_email or ""
        self.cc = cc or ""
        self.last_sent_at = last_sent_at
        self.latest = latest
        self.start = start
        self.rows = rows or []  # this cycle's emails, newest first


def cycles(enquiry_ids: Optional[List[str]] = None, *, include_marketing: bool = True,
           marketing_only: bool = False) -> Dict[str, Cycle]:
    """The current sequence of every enquiry that has one.

    The newest FollowUpStart wins, with the emails sent after it in its stage (manual sends count too);
    older enquiries without a start continue from their last manually sent follow-up.
    """
    rows = FollowUpSent.objects.filter(status__in=counted_statuses())
    starts = FollowUpStart.objects.all()
    if marketing_only:
        rows, starts = rows.filter(stage=MARKETING_STAGE), starts.filter(stage=MARKETING_STAGE)
    elif not include_marketing:
        rows, starts = rows.exclude(stage=MARKETING_STAGE), starts.exclude(stage=MARKETING_STAGE)
    if enquiry_ids is not None:
        rows = rows.filter(enquiry_id__in=enquiry_ids)
        starts = starts.filter(enquiry_id__in=enquiry_ids)
    sent: Dict[str, List[FollowUpSent]] = {}
    for row in rows.order_by("-sent_at"):
        sent.setdefault(row.enquiry_id, []).append(row)
    latest_start: Dict[str, FollowUpStart] = {}
    for start in starts.order_by("-started_at"):
        latest_start.setdefault(start.enquiry_id, start)

    result: Dict[str, Cycle] = {}
    for enquiry_id in set(sent) | set(latest_start):
        rows_for = sent.get(enquiry_id, [])
        start = latest_start.get(enquiry_id)
        if start is not None:
            after = [r for r in rows_for if r.sent_at >= start.started_at and r.stage == start.stage]
            if after:
                latest = after[0]
                marketing_thread = next((r.gmail_thread_id for r in reversed(after) if r.gmail_thread_id), "")
                result[enquiry_id] = Cycle(
                    stage=start.stage, count=len(after), next_on=latest.next_follow_up_on,
                    thread_id=(marketing_thread if start.stage == MARKETING_STAGE
                               else start.gmail_thread_id or latest.gmail_thread_id),
                    customer_email=start.customer_email or latest.customer_email, cc=latest.cc or start.cc,
                    last_sent_at=latest.sent_at, latest=latest, start=start, rows=after)
            else:
                started_on = timezone.localdate(start.started_at)
                # A Marketing start's time is when Follow-up 1 is due; a quotation / PI waits the first gap.
                first_gap = 0 if start.stage == MARKETING_STAGE else _gap(0)
                result[enquiry_id] = Cycle(
                    stage=start.stage, count=0, next_on=started_on + timedelta(days=first_gap),
                    thread_id="" if start.stage == MARKETING_STAGE else start.gmail_thread_id,
                    customer_email=start.customer_email, cc=start.cc, last_sent_at=start.started_at, start=start)
            continue
        if not rows_for:
            continue
        latest = rows_for[0]
        stage = latest.stage if latest.stage in STAGE_KEYS else DEFAULT_STAGE
        count = 0
        for row in rows_for:
            if (row.stage if row.stage in STAGE_KEYS else DEFAULT_STAGE) != stage:
                break
            count += 1
        result[enquiry_id] = Cycle(
            stage=stage, count=count, next_on=latest.next_follow_up_on, thread_id=latest.gmail_thread_id,
            customer_email=latest.customer_email, cc=latest.cc, last_sent_at=latest.sent_at, latest=latest,
            rows=rows_for[:count])
    return result


# ---------------------------------------------------------------------------------------------------------
# Notes, flags and the customer
# ---------------------------------------------------------------------------------------------------------

def note(enquiry_id: str, kind: str, text: str) -> None:
    """Add a line to the enquiry's timeline, unless the latest automatic note is already this one."""
    last = FollowUpNote.objects.filter(enquiry_id=str(enquiry_id)).order_by("-created_at", "-pk").first()
    if last is not None and last.kind == kind and last.text == text[:500]:
        return
    FollowUpNote.objects.create(enquiry_id=str(enquiry_id), kind=kind, text=text[:500])


def _contact(enquiry_id: str) -> EnquiryContact:
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    return contact


def set_flag(enquiry_id: str, flag: str, text: str = "") -> None:
    contact = _contact(enquiry_id)
    if contact.follow_up_flag != flag:
        contact.follow_up_flag = flag
        contact.follow_up_flag_at = timezone.now() if flag else None
        contact.save(update_fields=["follow_up_flag", "follow_up_flag_at", "updated_at"])
        if text:
            note(enquiry_id, flag or "flag_cleared", text)


def customer_email(enquiry_id: str, enquiry: Optional[Mapping[str, Any]] = None, cycle: Optional[Cycle] = None) -> str:
    """The enquiry's customer email: the one saved on the enquiry, else the one the quotation went to."""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("customer_email").first()
    for value in ((contact.customer_email if contact else ""), (enquiry or {}).get("customer_email"),
                  (cycle.customer_email if cycle else "")):
        value = str(value or "").strip().lower()
        if _valid_email(value):
            return value
    start = (FollowUpStart.objects.filter(enquiry_id=str(enquiry_id)).exclude(stage=MARKETING_STAGE)
             .exclude(customer_email="").order_by("-started_at").first())
    return (start.customer_email or "").lower() if start else ""


def other_customer_emails(enquiry_id: str) -> List[str]:
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("other_customer_emails").first()
    return [a.strip().lower() for a in str(contact.other_customer_emails if contact else "").split(",") if a.strip()]


def recent_email_to(address: str, now=None) -> bool:
    """True when this customer got any of our emails (either portal) within FOLLOW_UP_MIN_GAP_HOURS."""
    hours = int(getattr(settings, "FOLLOW_UP_MIN_GAP_HOURS", 24) or 0)
    address = (address or "").strip().lower()
    if hours <= 0 or not address:
        return False
    since = (now or timezone.now()) - timedelta(hours=hours)
    if FollowUpSent.objects.filter(customer_email__iexact=address, status__in=counted_statuses(),
                                   sent_at__gte=since).exists():
        return True
    if CustomerStatusEmail.objects.filter(customer_email__iexact=address, status=CustomerStatusEmail.STATUS_SENT,
                                          sent_at__gte=since).exists():
        return True
    try:
        from marketing.models import CampaignRecipient
    except Exception:
        return False
    return CampaignRecipient.objects.filter(customer_email__iexact=address, status=CampaignRecipient.STATUS_SENT,
                                            sent_at__gte=since).exists()


def _failures(enquiry_id: str, cycle: Cycle) -> int:
    rows = FollowUpSent.objects.filter(enquiry_id=enquiry_id, stage=cycle.stage, attempt=cycle.count + 1,
                                       status=FollowUpSent.STATUS_FAILED)
    if cycle.start is not None:
        rows = rows.filter(sent_at__gte=cycle.start.started_at)
    return rows.count()


def _lead(enquiry_id: str):
    try:
        from marketing.models import MarketingLead
    except Exception:  # Marketing app not installed
        return None
    return MarketingLead.objects.filter(enquiry_id=str(enquiry_id)).first()


def _marketing_stop_reason(enquiry_id: str, cycle: Cycle) -> str:
    """Why Marketing must not send, or ''."""
    if not getattr(settings, "MARKETING_AUTO_FOLLOW_UP", True):
        return "automatic Marketing follow-ups are turned off"
    lead = _lead(enquiry_id)
    if lead is None:
        return "no Marketing lead"
    if lead.do_not_contact:
        return "marked Do not contact in Marketing"
    if lead.revived_enquiry_id or lead.accepted_at:
        return "won back in Marketing"
    if lead.flag == lead.FLAG_REPLIED:
        return "the customer replied; Marketing's follow-ups stopped"
    if lead.flag == lead.FLAG_BOUNCED:
        return "the customer's email bounced"
    if cycle.count >= sequence_length(MARKETING_STAGE):
        return "all Marketing follow-ups and the closing email were sent"
    return ""


def _responded_reason(enquiry_id: str, cycle: Cycle) -> str:
    """The customer responded (reply, share-link answer, logged contact): the enquiry sequence stops."""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()
    if contact is not None and contact.follow_up_responded_at:
        return "the customer responded; the automatic follow-ups stopped"
    since = cycle.start.started_at if cycle.start is not None else None
    if since is not None and EnquiryFollowup.objects.filter(enquiry_id=str(enquiry_id), created_at__gte=since).exists():
        mark_responded(enquiry_id, "A call, meeting or other contact was logged")
        return "a call, meeting or other contact was logged; the automatic follow-ups stopped"
    return ""


def mark_responded(enquiry_id: str, how: str) -> None:
    contact = _contact(enquiry_id)
    if contact.follow_up_responded_at is None:
        contact.follow_up_responded_at = timezone.now()
        contact.save(update_fields=["follow_up_responded_at", "updated_at"])
        note(enquiry_id, "responded", f"{how}: automatic follow-ups stopped.")


def _skip_reason(enquiry_id: str, enquiry: Optional[Mapping[str, Any]], cycle: Cycle, today: date) -> str:
    """Why this sequence must not send today, or '' if it may."""
    if enquiry is None:
        return "enquiry not found"
    if cycle.stage == MARKETING_STAGE:
        stopped = _marketing_stop_reason(enquiry_id, cycle)
        if stopped:
            return stopped
    else:
        status = str(enquiry.get("status") or "").strip().lower()
        if status != "open":
            return f"the enquiry is {status or 'not set'}; enquiry follow-ups are sent only while it is Open"
        if cycle.count >= sequence_length(cycle.stage):
            return "all follow-ups and the closing email were sent"
        responded = _responded_reason(enquiry_id, cycle)
        if responded:
            return responded
        contact = EnquiryContact.objects.filter(enquiry_id=enquiry_id).only("follow_up_flag").first()
        if contact is not None and contact.follow_up_flag == EnquiryContact.FLAG_BOUNCED:
            return "the customer's email bounced"
    if EnquiryContact.objects.filter(enquiry_id=enquiry_id, auto_follow_up_paused=True).exists():
        return "automatic follow-ups are paused for this enquiry"
    retries = int(getattr(settings, "FOLLOW_UP_SEND_RETRIES", 3) or 0)
    if retries and _failures(enquiry_id, cycle) >= retries:
        return f"Gmail could not send this email {retries} times; check the error on the timeline"
    if not cycle.next_on:
        return "no next follow-up date set"
    if cycle.next_on > today:
        return f"not due until {cycle.next_on.isoformat()}"
    address = cycle.customer_email if cycle.stage == MARKETING_STAGE else customer_email(enquiry_id, enquiry, cycle)
    if not _valid_email(address):
        return "no customer email on the enquiry"
    if recent_email_to(address):
        return "another email went to this customer in the last 24 hours; it goes on a later run"
    return ""


def due_follow_ups(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Sequences whose next email is due today and that pass every rule (oldest due first)."""
    today = today or timezone.localdate()
    current = {eid: c for eid, c in cycles().items() if c.next_on and c.next_on <= today}
    if not current:
        return []
    enquiries = {str(e.get("enquiry_id")): e for e in services.list_enquiries(_api_root())}
    due = []
    for enquiry_id, cycle in sorted(current.items(), key=lambda item: (item[1].next_on, item[0])):
        if _skip_reason(enquiry_id, enquiries.get(enquiry_id), cycle, today):
            continue
        due.append({"enquiry_id": enquiry_id, "customer_email": cycle.customer_email, "attempt": cycle.count + 1,
                    "stage": cycle.stage, "due": cycle.next_on.isoformat()})
    return due


def _sender(stage: str = ""):
    """The Gmail account that sends: Marketing's own sender for Marketing stages, else the enquiry sender."""
    username = ""
    if stage == MARKETING_STAGE:
        username = (getattr(settings, "MARKETING_FOLLOW_UP_SENDER", "") or "").strip()
    if not username:
        username = (getattr(settings, "FOLLOW_UP_AUTO_SENDER", "") or "").strip()
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


def run_auto_follow_up(enquiry_id: str, today: Optional[date] = None) -> Dict[str, Any]:
    """Check the rules again and send one email. Never raises for expected problems."""
    enquiry_id = str(enquiry_id)
    today = today or timezone.localdate()
    if not getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False):
        return _result(enquiry_id, SKIPPED, "automatic follow-ups are turned off")
    cycle = cycles([enquiry_id]).get(enquiry_id)
    if cycle is None:
        return _result(enquiry_id, SKIPPED, "no quotation or proforma invoice has been sent yet")
    sender = _sender(cycle.stage)
    if sender is None:
        return _result(enquiry_id, FAILED, "the automatic sender is not set to an active user")
    try:
        enquiry = services.fetch_enquiry(_api_root(), enquiry_id)
    except services.EnquiryServiceError as exc:
        return _result(enquiry_id, FAILED, f"could not load enquiry: {exc}")
    reason = _skip_reason(enquiry_id, enquiry, cycle, today)
    if reason:
        return _result(enquiry_id, SKIPPED, reason)
    return _send(enquiry_id, enquiry, sender, today, cycle)


def customer_name_for(enquiry_id: str, enquiry: Mapping[str, Any], email: str = "") -> str:
    """The name to greet the customer with; '' when unknown (the email then says "Dear Sir/Madam")."""
    try:
        from marketing import lost
        from marketing.models import MarketingLead
    except Exception:  # Marketing app not installed
        return str(enquiry.get("customer_name") or "").strip()
    lead = MarketingLead.objects.filter(enquiry_id=str(enquiry_id)).only("customer_name").first()
    return lost.customer_display(enquiry.get("customer_name"), enquiry.get("subject"), email,
                                 lead.customer_name if lead else "")["greeting"]


# ---------------------------------------------------------------------------------------------------------
# The verified thread
# ---------------------------------------------------------------------------------------------------------

class Stop(Exception):
    """Nothing may be sent: `result` says why."""

    def __init__(self, result: Dict[str, Any]):
        super().__init__(result.get("reason"))
        self.result = result


def enquiry_thread(gmail, enquiry_id: str, enquiry: Mapping[str, Any], cycle: Cycle, own: set,
                   address: str) -> Tuple[str, List[Dict[str, Any]], Optional[Mapping[str, Any]]]:
    """(thread id, messages, the quotation / PI message) of the verified enquiry thread. Raises Stop when none
    passes (flags "Gmail thread not found"), when the customer replied or when the email bounced."""
    start = cycle.start
    numbers = thread_check.enquiry_numbers(enquiry_id, enquiry)
    subject = (start.subject if start is not None else "") or (cycle.rows[-1].subject if cycle.rows else "")
    message_id = start.gmail_message_id if start is not None else ""
    thread_id = cycle.thread_id
    messages = gmail.get_thread_messages(thread_id, limit=50) if thread_id else []
    ok, why = thread_check.check(messages or [], customer_email=address, subject=subject, own=own, numbers=numbers,
                                 message_id=message_id)
    if not ok:
        found = thread_check.find(gmail, customer_email=address, numbers=numbers, own=own, subject=subject)
        if found is None:
            set_flag(enquiry_id, EnquiryContact.FLAG_THREAD_NOT_FOUND,
                     f"{THREAD_NOT_FOUND} ({why}). Nothing was sent; checked again every hour.")
            raise Stop(_result(enquiry_id, SKIPPED, THREAD_NOT_FOUND, detail=why))
        thread_id, messages = found["thread_id"], found["messages"]
        if start is not None:
            start.gmail_thread_id = thread_id
            start.gmail_message_id = str(found["quote"].get("id") or start.gmail_message_id)
            start.subject = (start.subject or found["subject"])[:500]
            start.customer_email = address
            start.save(update_fields=["gmail_thread_id", "gmail_message_id", "subject", "customer_email"])
    contact = EnquiryContact.objects.filter(enquiry_id=enquiry_id).only("follow_up_flag").first()
    if contact is not None and contact.follow_up_flag == EnquiryContact.FLAG_THREAD_NOT_FOUND:
        set_flag(enquiry_id, "", "Gmail thread found again: the follow-ups continue from where they stopped.")
    quote = thread_check.quote_message(messages, own=own, numbers=numbers, message_id=message_id)
    seen = thread_check.replies(messages, customer_email=address, others=other_customer_emails(enquiry_id), own=own,
                                after_id=str((quote or {}).get("id") or ""))
    if seen["bounced"]:
        set_flag(enquiry_id, EnquiryContact.FLAG_BOUNCED, f"Email bounced ({address}): automatic follow-ups stopped.")
        raise Stop(_result(enquiry_id, SKIPPED, "the customer's email bounced"))
    if seen["replied"]:
        mark_responded(enquiry_id, "The customer replied in the Gmail thread")
        raise Stop(_result(enquiry_id, SKIPPED, "customer replied; answer them manually"))
    return thread_id, messages, quote


def _set_lead_flag(lead, flag: str, text: str, user=None) -> None:
    from marketing.models import MarketingActivity

    if lead.flag == flag:
        return
    lead.flag = flag
    lead.flag_at = timezone.now() if flag else None
    lead.save(update_fields=["flag", "flag_at", "updated_at"])
    if text:
        MarketingActivity.objects.create(lead=lead, kind=MarketingActivity.KIND_SYSTEM, created_by=user, text=text)


def _enquiry_reply_after_move(gmail, enquiry_id: str, lead, own: set, address: str) -> bool:
    """True when the customer replied in the enquiry's own thread after it moved to Marketing."""
    start = (FollowUpStart.objects.filter(enquiry_id=enquiry_id).exclude(stage=MARKETING_STAGE)
             .exclude(gmail_thread_id="").order_by("-started_at").first())
    if start is None or lead.ready_at is None:
        return False
    messages = gmail.get_thread_messages(start.gmail_thread_id, limit=50) or []
    seen = thread_check.replies(messages, customer_email=address, others=other_customer_emails(enquiry_id), own=own)
    for message in seen["replied"]:
        when = message.get("parsed_date")
        if when is not None and timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.utc)  # Gmail dates without a zone are UTC
        if when is not None and when >= lead.ready_at:
            return True
    return False


def marketing_thread(gmail, enquiry_id: str, cycle: Cycle, own: set, address: str, sender
                     ) -> Tuple[str, Optional[Mapping[str, Any]], str]:
    """(thread id, message to reply to, Follow-up 1 subject) for Marketing email 2+, or ("", None, "") for
    Follow-up 1 (a new thread). Raises Stop when the thread fails the check, or the customer replied."""
    lead = _lead(enquiry_id)
    if lead is not None and _enquiry_reply_after_move(gmail, enquiry_id, lead, own, address):
        _set_lead_flag(lead, lead.FLAG_REPLIED, "The customer replied in the enquiry's thread after it moved to "
                                                "Marketing: automatic follow-ups stopped.", sender)
        _notify_reply(enquiry_id, lead)
        raise Stop(_result(enquiry_id, SKIPPED, "customer replied in the enquiry thread; Marketing stopped"))
    if cycle.count == 0:
        return "", None, ""
    first = cycle.rows[-1] if cycle.rows else None
    if first is None or not first.gmail_thread_id:
        if dry_run():  # Follow-up 1 was a dry run: there is no real thread to reply in
            return "", None, (first.subject if first else "")
        raise Stop(_result(enquiry_id, SKIPPED, THREAD_NOT_FOUND))
    messages = gmail.get_thread_messages(first.gmail_thread_id, limit=50) or []
    ok, why = thread_check.check(messages, customer_email=address, subject=first.subject, own=own, numbers=[],
                                 message_id=str(first.gmail_message_id or ""))
    if not ok:
        if lead is not None:
            _set_lead_flag(lead, lead.FLAG_THREAD_NOT_FOUND, f"{THREAD_NOT_FOUND} ({why}). Nothing was sent.", sender)
        raise Stop(_result(enquiry_id, SKIPPED, THREAD_NOT_FOUND, detail=why))
    if lead is not None and lead.flag == lead.FLAG_THREAD_NOT_FOUND:
        _set_lead_flag(lead, "", "Gmail thread found again: the follow-ups continue.", sender)
    seen = thread_check.replies(messages, customer_email=address, others=other_customer_emails(enquiry_id), own=own,
                                after_id=str(first.gmail_message_id or ""))
    if seen["bounced"] and lead is not None:
        _set_lead_flag(lead, lead.FLAG_BOUNCED, f"Email bounced ({address}): automatic follow-ups stopped.", sender)
        raise Stop(_result(enquiry_id, SKIPPED, "the customer's email bounced"))
    if seen["replied"]:
        _note_marketing_reply(enquiry_id, sender, seen["replied"])
        raise Stop(_result(enquiry_id, SKIPPED, "customer replied; answer them manually"))
    original = gmail.get_message(messages[-1].get("id"))
    return first.gmail_thread_id, original, first.subject


def _notify_reply(enquiry_id: str, lead) -> None:
    """Staff are told when a lead revives (the customer replied after it moved to Marketing)."""
    try:
        from django.urls import reverse

        from marketing import notify

        enquiry = services.fetch_enquiry(_api_root(), enquiry_id)
        user = notify.find_user(str(enquiry.get("assigned_to") or ""))
        if user is not None:
            notify.notify_reply(user, customer=lead.customer_name or str(enquiry.get("customer_name") or enquiry_id),
                                campaign_name=f"enquiry {enquiry_id} (Marketing follow-up)",
                                url=reverse("marketing:detail", args=[enquiry_id]))
    except Exception:
        logger.exception("Could not notify about the reply on %s", enquiry_id)


# ---------------------------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------------------------

def _document_number(gmail, quote: Mapping[str, Any]) -> str:
    """The Voucher No. printed on the quotation / PI PDF of the sent email, or '' when it cannot be read."""
    from .share_email import voucher_number

    message_id = str(quote.get("id") or "")
    for attachment in quote.get("attachments") or []:
        if not str(attachment.get("filename") or "").lower().endswith(".pdf"):
            continue
        try:
            data = gmail.download_attachment(message_id, str(attachment.get("attachment_id") or ""))
        except Exception as exc:  # the follow-up still goes out without the number
            logger.warning("Could not read the quotation PDF of message %s: %s", message_id, exc)
            continue
        number = voucher_number(data)
        if number:
            return number[:64]
    return ""


def _send(enquiry_id: str, enquiry: Dict[str, Any], sender, today: date, cycle: Cycle) -> Dict[str, Any]:
    """Verify the thread, render and send (or dry-run) the next email of the sequence, and record it."""
    stage = cycle.stage
    attempt = cycle.count + 1
    marketing = stage == MARKETING_STAGE
    closing = attempt > follow_up_count(stage)
    address = (cycle.customer_email if marketing else customer_email(enquiry_id, enquiry, cycle)).lower()

    gmail = GmailService(sender)
    quote_message_sent: Optional[Mapping[str, Any]] = None
    try:
        own = {a.lower() for a in gmail.own_addresses()}
        if not own:
            return _result(enquiry_id, SKIPPED, "could not confirm the sending account's address")
        if thread_check.is_ours(address, own):
            return _result(enquiry_id, SKIPPED, "the customer address is one of our own addresses")
        if marketing:
            thread_id, original, first_subject = marketing_thread(gmail, enquiry_id, cycle, own, address, sender)
        else:
            thread_id, messages, quote_message_sent = enquiry_thread(gmail, enquiry_id, enquiry, cycle, own, address)
            original = gmail.get_message(messages[-1].get("id"))
    except Stop as stop:
        return stop.result
    except GmailAPIError as exc:
        return _result(enquiry_id, FAILED, f"Gmail error: {exc}")

    # The quotation / PI number, read from the PDF we sent (once; then kept on the sequence's start).
    document_no = ""
    if not marketing and cycle.start is not None:
        document_no = cycle.start.document_no
        if not document_no and quote_message_sent:
            document_no = _document_number(gmail, quote_message_sent)
            if document_no:
                cycle.start.document_no = document_no
                cycle.start.save(update_fields=["document_no"])

    share_url = share_password = ""
    if not marketing and not closing:
        try:
            share_url, share_password = follow_up_links.for_follow_up(enquiry_id, enquiry, start=cycle.start)
        except follow_up_links.ShareLinkMissing as exc:
            logger.error("Follow-up %s for enquiry %s not sent: no share link (%s)", attempt, enquiry_id, exc)
            note(enquiry_id, "error", f"Follow-up {attempt} not sent: no share link ({exc}). Retried next run.")
            return _result(enquiry_id, FAILED, f"not sent: {exc}")

    title = str(enquiry.get("subject") or enquiry.get("project_name") or "")
    name = customer_name_for(enquiry_id, enquiry, address)
    lead = _lead(enquiry_id) if marketing else None
    source = lead.lead_source if lead is not None else ""
    if closing and not marketing:
        body = enquiry_closing_body(customer_name=name, document=document_for_stage(stage))
    else:
        body = build_follow_up_body(
            stage, attempt=attempt, reference=enquiry_id, subject=title, user=sender, today=today,
            share_url=share_url, share_password=share_password, customer_name=name,
            document_number=document_no,
            delivery_timeline=str(enquiry.get("delivery_timeline") or ""),
            confirm_by_date=str(enquiry.get("confirm_by_date") or ""), lead_source=source)
    if marketing and attempt == 1:
        subject = marketing_subject(1, customer_name=name, enquiry_title=title, lead_source=source)
    elif marketing:
        subject = first_subject
    else:
        subject = (original or {}).get("subject") or (cycle.start.subject if cycle.start else "") or title
    cc_list = cc_for(stage, address)
    next_on = next_follow_up_date(attempt, today, stage)
    what = "Closing email" if closing else f"Follow-up {attempt}"
    details = {"customer_email": address, "attempt": attempt, "stage": stage, "cc": cc_list, "subject": subject,
               "thread_id": thread_id, "next_follow_up_on": next_on.isoformat() if next_on else None}

    if dry_run():
        try:
            with transaction.atomic():
                record = FollowUpSent.objects.create(
                    enquiry_id=enquiry_id, attempt=attempt, customer_email=address,
                    customer_name=str(enquiry.get("customer_name") or "").strip(), subject=subject[:500], body=body,
                    gmail_thread_id="" if marketing and attempt == 1 else thread_id, sent_by=sender,
                    status=FollowUpSent.STATUS_DRY_RUN, submission_token=f"dry:{enquiry_id}:{stage}:{attempt}:"
                    f"{cycle.start.pk if cycle.start else 0}", stage=stage, next_follow_up_on=next_on,
                    source=FollowUpSent.SOURCE_AUTO, cc=", ".join(cc_list))
        except IntegrityError:
            return _result(enquiry_id, SKIPPED, "already handled")
        logger.info("Dry run, nothing sent: %s for %s (%s)\nTo: %s\nCC: %s\nSubject: %s\nThread: %s\n\n%s",
                    what, enquiry_id, stage, address, ", ".join(cc_list) or "none", subject,
                    thread_id or "(new thread)", body)
        _after(enquiry_id, enquiry, sender, cycle, attempt, closing, address, record, sent=False)
        return _result(enquiry_id, DRY_RUN, "dry run: nothing was sent", body=body, **details)

    failures = _failures(enquiry_id, cycle)
    try:
        with transaction.atomic():
            record = FollowUpSent.objects.create(
                enquiry_id=enquiry_id, attempt=attempt, customer_email=address,
                customer_name=str(enquiry.get("customer_name") or "").strip(), subject=subject[:500], body=body,
                gmail_message_id=(original or {}).get("id") or "", gmail_thread_id=thread_id, sent_by=sender,
                status=FollowUpSent.STATUS_PENDING,
                submission_token=f"auto:{enquiry_id}:{stage}:{attempt}:{cycle.start.pk if cycle.start else 0}:{failures}",
                stage=stage, next_follow_up_on=next_on, source=FollowUpSent.SOURCE_AUTO, cc=", ".join(cc_list))
    except IntegrityError:
        return _result(enquiry_id, SKIPPED, "already handled")
    try:
        html_body = to_html(body)
        if original is None:
            sent = gmail.send_email(address, subject, body, cc=cc_list or None, html_body=html_body,
                                    inline_images=logo_inline_images())
        else:
            sent = gmail.send_reply(original_message=original, body=body, subject=subject, to=address,
                                    cc=cc_list or None, html_body=html_body, inline_images=logo_inline_images())
    except Exception as exc:  # Gmail API or network failure: retried on the next runs
        logger.warning("Automatic %s failed for enquiry %s: %s", what.lower(), enquiry_id, exc)
        record.status = FollowUpSent.STATUS_FAILED
        record.error_message = str(exc)[:1000]
        record.save(update_fields=["status", "error_message"])
        retries = int(getattr(settings, "FOLLOW_UP_SEND_RETRIES", 3) or 0)
        if failures + 1 >= retries:
            note(enquiry_id, "error", f"{what} could not be sent after {retries} tries: {str(exc)[:200]}")
        return _result(enquiry_id, FAILED, f"send failed: {exc}", **details)
    record.status = FollowUpSent.STATUS_SENT
    record.gmail_message_id = (sent or {}).get("id") or record.gmail_message_id
    record.gmail_thread_id = (sent or {}).get("threadId") or record.gmail_thread_id
    record.sent_at = timezone.now()
    record.save(update_fields=["status", "gmail_message_id", "gmail_thread_id", "sent_at"])
    _after(enquiry_id, enquiry, sender, cycle, attempt, closing, address, record, sent=True)
    return _result(enquiry_id, SENT, "", **details)


def _after(enquiry_id: str, enquiry: Mapping[str, Any], sender, cycle: Cycle, attempt: int, closing: bool,
           address: str, record: FollowUpSent, *, sent: bool) -> None:
    """What follows an email: Marketing notes it on the lead; the enquiry's closing email closes the enquiry
    ("No response") and moves it to Marketing."""
    if cycle.stage == MARKETING_STAGE:
        _log_marketing(enquiry_id, sender, attempt, address, closing=closing, sent=sent)
        return
    if closing:
        CustomerStatusEmail.objects.update_or_create(
            enquiry_id=enquiry_id, event_key=CustomerStatusEmail.KIND_CLOSING,
            defaults={"kind": CustomerStatusEmail.KIND_CLOSING, "customer_email": address,
                      "status": CustomerStatusEmail.STATUS_SENT if sent else CustomerStatusEmail.STATUS_DRY_RUN,
                      "subject": record.subject, "body": record.body, "gmail_thread_id": record.gmail_thread_id,
                      "gmail_message_id": record.gmail_message_id or "", "sent_at": record.sent_at,
                      "note": "" if sent else "dry run (not sent)"})
        close_no_response(enquiry_id, address)


def close_no_response(enquiry_id: str, address: str) -> None:
    """The closing email went and the customer never responded: Closed ("No response"), then a Marketing lead."""
    try:
        services.update_enquiry(_api_root(), enquiry_id, {"status": "closed"})
    except services.EnquiryServiceError as exc:
        note(enquiry_id, "error", f"Closing email sent but the status could not be set to Closed: {exc}")
    contact = _contact(enquiry_id)
    contact.close_reason = EnquiryContact.CLOSE_NO_RESPONSE
    contact.moved_to_marketing_at = contact.moved_to_marketing_at or timezone.now()
    contact.save(update_fields=["close_reason", "moved_to_marketing_at", "updated_at"])
    note(enquiry_id, "moved_to_marketing",
         f"Closed – No response · moved to Marketing on {timezone.localtime(contact.moved_to_marketing_at):%d %b %Y}")
    try:
        from marketing import lost
    except Exception:  # Marketing app not installed
        return
    delay = int(getattr(settings, "MARKETING_NO_RESPONSE_FIRST_DELAY_DAYS", 2) or 0)
    lost.create_lead(enquiry_id, lost.NO_RESPONSE, customer_email=address,
                     first_due=timezone.now() + timedelta(days=delay))


def _log_marketing(enquiry_id: str, sender, attempt: int, address: str, *, closing: bool, sent: bool) -> None:
    try:
        from marketing.models import MarketingActivity, MarketingLead
    except Exception:
        return
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    what = "Closing email" if closing else f"Automatic follow-up {attempt}"
    how = "as a new email" if attempt == 1 else "in the Marketing thread"
    state = "sent" if sent else "dry run (not sent)"
    MarketingActivity.objects.create(lead=lead, kind=MarketingActivity.KIND_EMAIL, created_by=sender,
                                     text=f"{what} {state} to {address} {how}.")


def _note_marketing_reply(enquiry_id: str, sender, replied: List[Mapping[str, Any]]) -> None:
    """The customer answered a Marketing email: the follow-ups stop and the lead shows Replied. A reply starting
    with STOP marks the customer Do not contact."""
    try:
        from marketing import lost
        from marketing.models import MarketingActivity, MarketingLead
    except Exception:
        return
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    if any(lost.is_stop_reply(m.get("snippet") or m.get("body_text")) for m in replied):
        lost.mark_do_not_contact(lead, sender, 'Marked Do not contact: the customer replied "STOP".')
    if lead.flag != lead.FLAG_REPLIED:
        snippet = str(replied[0].get("snippet") or "").strip()[:300]
        _set_lead_flag(lead, lead.FLAG_REPLIED, f"{lost.REPLY_NOTE}: automatic follow-ups stopped."
                       + (f' "{snippet}"' if snippet else ""), sender)


# ---------------------------------------------------------------------------------------------------------
# Starting sequences (no buttons)
# ---------------------------------------------------------------------------------------------------------

def record_start(enquiry_id: str, stage: str, *, sent: Mapping[str, Any], customer_email: str, cc: List[str],
                 subject: str, user=None, document_no: str = "", share_url: str = "", share_password: str = "",
                 started_at: Optional[datetime] = None, restart: bool = False) -> Optional[FollowUpStart]:
    """The quotation / PI was emailed: its sequence starts (thread, customer email and subject stored now).

    A revised quotation or PI of a sequence already running does not restart it (the follow-ups carry the
    latest revision's link anyway); the existing start is returned. With `restart` (the quotations portal's send of
    an approved version) a new start is made, so the schedule counts again from this email.
    """
    if stage != MARKETING_STAGE:
        thread_id = str((sent or {}).get("threadId") or "")
        if not thread_id:
            return None
        existing = (FollowUpStart.objects.filter(enquiry_id=str(enquiry_id), stage=stage)
                    .order_by("-started_at").first())
        if existing is not None and not restart:
            return existing
    return FollowUpStart.objects.create(
        enquiry_id=str(enquiry_id), stage=stage, gmail_thread_id=str((sent or {}).get("threadId") or ""),
        gmail_message_id=str((sent or {}).get("id") or ""), customer_email=(customer_email or "").lower(),
        cc=", ".join(cc or []), subject=thread_check.normalize_subject(subject)[:500],
        document_no=(document_no or "")[:64], share_url=(share_url or "")[:500],
        share_password=(share_password or "")[:64], started_at=started_at or timezone.now(),
        started_by=user if getattr(user, "is_authenticated", False) else None)


def start_marketing(enquiry_id: str, *, customer_email: str, first_due: Optional[datetime] = None
                    ) -> Optional[FollowUpStart]:
    """Marketing's sequence for a lead: Follow-up 1 is due at `first_due`. One per lead, never restarted."""
    existing = FollowUpStart.objects.filter(enquiry_id=str(enquiry_id), stage=MARKETING_STAGE).first()
    if existing is not None:
        return existing
    if not _valid_email(customer_email):
        return None
    return FollowUpStart.objects.create(enquiry_id=str(enquiry_id), stage=MARKETING_STAGE,
                                        customer_email=customer_email.lower(),
                                        started_at=first_due or timezone.now())


def detect_gmail_quotes(enquiries: List[Mapping[str, Any]], *, limit: int = 30) -> List[Dict[str, Any]]:
    """Open enquiries with no sequence yet: find their quotation / PI in our sent Gmail (thread_check rules)
    and start the sequence. The first follow-up goes FOLLOW_UP_AUTO_GAP_DAYS after the quotation, or that long
    from now when it was found late, so nothing goes out in a burst."""
    sender = _sender()
    if sender is None:
        return [{"result": FAILED, "reason": "FOLLOW_UP_AUTO_SENDER is not set to an active user"}]
    started = set(FollowUpStart.objects.exclude(stage=MARKETING_STAGE).values_list("enquiry_id", flat=True))
    open_ids = [e for e in enquiries if str(e.get("status") or "").strip().lower() == "open"
                and str(e.get("enquiry_id")) not in started]
    if not open_ids:
        return []
    gmail = GmailService(sender)
    try:
        own = {a.lower() for a in gmail.own_addresses()}
    except GmailAPIError as exc:
        return [{"result": FAILED, "reason": f"Gmail error: {exc}"}]
    results = []
    gap = timedelta(days=_gap(0))
    now = timezone.now()
    for enquiry in open_ids[:limit]:
        enquiry_id = str(enquiry.get("enquiry_id"))
        address = customer_email(enquiry_id, enquiry)
        if not address:
            continue
        try:
            found = thread_check.find(gmail, customer_email=address, numbers=thread_check.enquiry_numbers(
                enquiry_id, enquiry), own=own)
        except GmailAPIError as exc:
            results.append({"enquiry_id": enquiry_id, "result": FAILED, "reason": f"Gmail error: {exc}"})
            continue
        if found is None:
            continue
        quote = found["quote"]
        when = quote.get("parsed_date")
        if when is not None and timezone.is_naive(when):
            when = timezone.make_aware(when, timezone.utc)  # Gmail dates without a zone are UTC
        started_at = when if when is not None and when >= now - gap else now
        stage = PAYMENT_STAGE if thread_check.document_of(quote) == "proforma" else QUOTATION_STAGE
        record_start(enquiry_id, stage, sent={"threadId": found["thread_id"], "id": quote.get("id")},
                     customer_email=address, cc=[], subject=found["subject"], started_at=started_at)
        note(enquiry_id, "started", f"Quotation / PI found in Gmail (sent to {address}): automatic follow-ups started.")
        results.append({"enquiry_id": enquiry_id, "result": "started", "stage": stage})
    return results


def manual_send_plan(gmail, enquiry_id: str, *, thread_id: str, recipient: str, stage: str) -> Dict[str, Any]:
    """Checks for a follow-up sent by hand from the follow-up page, so it fits the automatic sequence:
    {"stage", "attempt", "cc", "error"}. With a running sequence the email counts as its next number (the
    scheduler then never sends that number again), must go to the verified customer address and into the
    verified thread; it always gets the portal's fixed CC list."""
    enquiry_id = str(enquiry_id)
    recipient = (recipient or "").strip().lower()
    cycle = cycles([enquiry_id], include_marketing=False).get(enquiry_id)
    plan: Dict[str, Any] = {"stage": stage, "attempt": 0, "cc": cc_for(stage, recipient), "error": ""}
    if cycle is None or cycle.start is None:
        return plan
    plan.update(stage=cycle.stage, attempt=cycle.count + 1, cc=cc_for(cycle.stage, recipient))
    verified = customer_email(enquiry_id, None, cycle)
    if verified and recipient != verified:
        plan["error"] = f"Follow-ups for this enquiry go only to the verified customer email {verified}."
        return plan
    try:
        own = {a.lower() for a in gmail.own_addresses()}
        messages = gmail.get_thread_messages(thread_id, limit=50) if thread_id else []
    except GmailAPIError as exc:
        plan["error"] = f"Gmail error: {exc}"
        return plan
    ok, why = thread_check.check(messages or [], customer_email=recipient, subject=cycle.start.subject, own=own,
                                 numbers=thread_check.enquiry_numbers(enquiry_id),
                                 message_id=cycle.start.gmail_message_id)
    if not ok:
        plan["error"] = f"{THREAD_NOT_FOUND} ({why})."
    return plan

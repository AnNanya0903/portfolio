"""Stage 2: automatic enquiry follow-ups.

The scheduler (enquiries/scheduler.py, run hourly by App Engine cron) calls two functions:
  1. due_follow_ups()        -> which enquiries are due today
  2. run_auto_follow_up(id)  -> check the rules again and send one follow-up

Automatic follow-ups only continue a conversation that is already confirmed, and always reply in
that one Gmail thread:
- a sequence starts when the quotation or proforma invoice is emailed with "Send to customer"
  (FollowUpStart, stage "waiting_po" / "payment_pending"), or when the rejection email is sent
  (stage "marketing": the Marketing portal tries to win the customer back);
- older enquiries continue from their last manually sent follow-up, as before.
Follow-ups go every FOLLOW_UP_AUTO_GAP_DAYS; after the last one the closing email follows
(enquiries/status_emails.py for quotations and PIs, the final Marketing email for Marketing).
There is no acknowledgement email: a quotation / PI sequence is Follow-up 1-4, then the closing email.
Every quotation / PI follow-up carries the enquiry's own share link and password for its latest revision
(enquiries/follow_up_links.py); without one the follow-up is not sent.
Marketing's first follow-up is due as soon as the lead is Ready (the rejection), and each Marketing email
is a new email with its own subject, never in the enquiry's thread.
When anything is unclear the enquiry is skipped with a reason, never guessed.
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

from dashboard.follow_up_templates import (
    DEFAULT_STAGE, MARKETING_STAGE, PAYMENT_STAGE, STAGE_KEYS, build_follow_up_body, follow_up_count,
    marketing_subject, to_html,
)
from dashboard.gmail_service import GmailAPIError, GmailService
from kyc import routing as kyc_routing

from . import customer_emails, follow_up_links, payment_terms, services
from .models import EnquiryContact, FollowUpSent, FollowUpStart

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


def _gap(after_attempt: int) -> int:
    """Days to wait after follow-up number `after_attempt` (0 = after the quotation / PI / rejection email).

    FOLLOW_UP_AUTO_GAP_DAYS lists the waits after follow-up #1, #2, ...; its first value is also used after
    the quotation / PI / rejection email, and its last value repeats.
    """
    gaps = list(getattr(settings, "FOLLOW_UP_AUTO_GAP_DAYS", []) or [2])
    index = max(after_attempt - 1, 0)
    return gaps[index] if index < len(gaps) else gaps[-1]


def next_follow_up_date(attempt_sent: int, today: date, stage: str = "") -> Optional[date]:
    """When to send the next email after follow-up number `attempt_sent`; None after the last one.

    Marketing's closing email counts as one more step after its follow-ups.
    """
    limit = follow_up_count(stage) + (1 if stage == MARKETING_STAGE else 0)
    if limit and attempt_sent >= limit:
        return None
    return today + timedelta(days=_gap(attempt_sent))


class Cycle:
    """Where an enquiry's follow-ups stand: stage, how many were sent, and the thread to reply in."""

    def __init__(self, *, stage: str, count: int, next_on: Optional[date], thread_id: str, customer_email: str,
                 cc: str, last_sent_at, latest: Optional[FollowUpSent] = None,
                 start: Optional[FollowUpStart] = None):
        self.stage = stage if stage in STAGE_KEYS else DEFAULT_STAGE
        self.count = count
        self.next_on = next_on
        self.thread_id = thread_id or ""
        self.customer_email = customer_email or ""
        self.cc = cc or ""
        self.last_sent_at = last_sent_at
        self.latest = latest
        self.start = start


def cycles(enquiry_ids: Optional[List[str]] = None, *, include_marketing: bool = True) -> Dict[str, Cycle]:
    """The current follow-up cycle of every enquiry that has one.

    The newest of these wins: a FollowUpStart (quotation / PI / rejection email) with the follow-ups sent
    after it in the same stage, or the last manually started follow-up (older enquiries, or a person who
    changed the stage by hand).
    """
    rows = FollowUpSent.objects.filter(status=FollowUpSent.STATUS_SENT)
    starts = FollowUpStart.objects.all()
    if not include_marketing:  # the Enquiries board shows the enquiry's own emails, not Marketing's
        rows = rows.exclude(stage=MARKETING_STAGE)
        starts = starts.exclude(stage=MARKETING_STAGE)
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
            after = [r for r in rows_for if r.sent_at >= start.started_at]
            if all(r.stage == start.stage for r in after):
                if after:
                    latest = after[0]
                    result[enquiry_id] = Cycle(
                        stage=start.stage, count=len(after), next_on=latest.next_follow_up_on,
                        thread_id=latest.gmail_thread_id or start.gmail_thread_id,
                        customer_email=latest.customer_email or start.customer_email, cc=latest.cc or start.cc,
                        last_sent_at=latest.sent_at, latest=latest, start=start)
                else:
                    started_on = timezone.localdate(start.started_at)
                    # Marketing follows up as soon as the lead is Ready; quotations / PIs wait the first gap.
                    first_gap = 0 if start.stage == MARKETING_STAGE else _gap(0)
                    result[enquiry_id] = Cycle(
                        stage=start.stage, count=0, next_on=started_on + timedelta(days=first_gap),
                        thread_id=start.gmail_thread_id, customer_email=start.customer_email, cc=start.cc,
                        last_sent_at=start.started_at, start=start)
                continue
        if not rows_for:
            continue
        # Started with the Follow-up button: the last follow-up sets the stage, and only that stage's
        # follow-ups count (a quotation or PI sent with the button is follow-up #1 of its stage).
        latest = rows_for[0]
        stage = latest.stage if latest.stage in STAGE_KEYS else DEFAULT_STAGE
        if stage == PAYMENT_STAGE:
            count = sum(1 for r in rows_for if r.stage == PAYMENT_STAGE)
        else:
            count = 0
            for row in rows_for:
                if (row.stage if row.stage in STAGE_KEYS else DEFAULT_STAGE) != stage:
                    break
                count += 1
        result[enquiry_id] = Cycle(
            stage=stage, count=count, next_on=latest.next_follow_up_on, thread_id=latest.gmail_thread_id,
            customer_email=latest.customer_email, cc=latest.cc, last_sent_at=latest.sent_at, latest=latest)
    return result


def _marketing_stop_reason(enquiry_id: str, enquiry: Dict[str, Any], cycle: Cycle, *, manual: bool = False) -> str:
    """Why Marketing must not follow up (rejected enquiries only), or ''. `manual`: sent with a button."""
    if not manual and not getattr(settings, "MARKETING_AUTO_FOLLOW_UP", True):
        return "automatic Marketing follow-ups are turned off"
    if str(enquiry.get("status") or "").strip().lower() != "rejected":
        return "the enquiry is no longer rejected"
    try:
        from marketing.models import MarketingLead
    except Exception:  # Marketing app not installed
        return "the Marketing portal is not installed"
    lead = MarketingLead.objects.filter(enquiry_id=enquiry_id).first()
    if lead is not None and lead.do_not_contact:
        return "marked Do not contact in Marketing"
    if lead is not None and (lead.revived_enquiry_id or lead.accepted_at):
        return "won back in Marketing"
    if cycle.count >= follow_up_count(MARKETING_STAGE) + 1:
        return "all Marketing follow-ups and the closing email were sent"
    return ""


def _skip_reason(enquiry_id: str, enquiry: Optional[Dict[str, Any]], cycle: Cycle, today: date) -> str:
    """Why this enquiry must not get an automatic follow-up today, or '' if it may."""
    from .views import _follow_up_stopped_reason  # local import: views imports a lot

    if enquiry is None:
        return "enquiry not found"
    if cycle.stage == MARKETING_STAGE:
        stopped = _marketing_stop_reason(enquiry_id, enquiry, cycle)
        if stopped:
            return stopped
    else:
        # Quotation and PI follow-ups are sent only while the enquiry is Open.
        status = str(enquiry.get("status") or "").strip().lower()
        if status != "open":
            return f"the enquiry is {status or 'not set'}; quotation and PI follow-ups are sent only while it is Open"
        stopped = _follow_up_stopped_reason({**enquiry, "enquiry_id": enquiry_id}, cycle.count)
        if stopped:
            return stopped
        if cycle.stage == PAYMENT_STAGE:
            hold = payment_terms.credit_hold_until(enquiry_id, today)
            if hold:
                return f"within the credit period; payment is due on {hold.isoformat()}"
    if EnquiryContact.objects.filter(enquiry_id=enquiry_id, auto_follow_up_paused=True).exists():
        return "automatic follow-ups are paused for this enquiry"
    if not cycle.next_on:
        return "no next follow-up date set"
    if cycle.next_on > today:
        return f"not due until {cycle.next_on.isoformat()}"
    if not cycle.thread_id and cycle.stage != MARKETING_STAGE:  # Marketing emails open their own threads
        return "no confirmed Gmail conversation; send the first follow-up manually"
    if not _valid_email(cycle.customer_email):
        return "no confirmed customer email"
    return ""


def due_follow_ups(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Enquiries whose next follow-up date has arrived and that pass every rule."""
    today = today or timezone.localdate()
    current = {eid: c for eid, c in cycles().items() if c.next_on and c.next_on <= today}
    if not current:
        return []
    enquiries = {str(e.get("enquiry_id")): e for e in services.list_enquiries(_api_root())}
    due = []
    for enquiry_id, cycle in sorted(current.items()):
        if _skip_reason(enquiry_id, enquiries.get(enquiry_id), cycle, today):
            continue
        due.append({
            "enquiry_id": enquiry_id,
            "customer_email": cycle.customer_email,
            "attempt": cycle.count + 1,
            "stage": cycle.stage,
            "due": cycle.next_on.isoformat(),
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

    cycle = cycles([enquiry_id]).get(enquiry_id)
    if cycle is None:
        return _result(enquiry_id, SKIPPED, "no quotation, proforma invoice or follow-up has been sent yet")

    try:
        enquiry = services.fetch_enquiry(_api_root(), enquiry_id)
    except services.EnquiryServiceError as exc:
        return _result(enquiry_id, FAILED, f"could not load enquiry: {exc}")

    reason = _skip_reason(enquiry_id, enquiry, cycle, today)
    if reason:
        return _result(enquiry_id, SKIPPED, reason)
    return _send(enquiry_id, enquiry, sender, today, thread_id=cycle.thread_id,
                 customer_email=cycle.customer_email, stage=cycle.stage, attempt=cycle.count + 1, cc=cycle.cc,
                 start=cycle.start)


def customer_name_for(enquiry_id: str, enquiry: Dict[str, Any], email: str = "") -> str:
    """The name to greet the customer with: the name Marketing typed, else the enquiry's customer name unless
    it is really an email subject; '' when unknown (the email then says "Dear Sir/Madam")."""
    try:
        from marketing import lost
        from marketing.models import MarketingLead
    except Exception:  # Marketing app not installed
        return str(enquiry.get("customer_name") or "").strip()
    lead = MarketingLead.objects.filter(enquiry_id=str(enquiry_id)).only("customer_name").first()
    return lost.customer_display(enquiry.get("customer_name"), enquiry.get("subject"), email,
                                 lead.customer_name if lead else "")["greeting"]


def _marketing_threads(enquiry_id: str, start: Optional[FollowUpStart]) -> List[str]:
    """The Gmail threads of the Marketing emails already sent in this sequence (each email opens its own)."""
    rows = FollowUpSent.objects.filter(enquiry_id=enquiry_id, stage=MARKETING_STAGE,
                                       status=FollowUpSent.STATUS_SENT).exclude(gmail_thread_id="")
    if start is not None:
        rows = rows.filter(sent_at__gte=start.started_at)
    return list(dict.fromkeys(rows.order_by("sent_at").values_list("gmail_thread_id", flat=True)))


def _send(enquiry_id: str, enquiry: Dict[str, Any], sender, today: date, *, thread_id: str,
          customer_email: str, stage: str, attempt: int, cc: str = "", start: Optional[FollowUpStart] = None,
          source: str = FollowUpSent.SOURCE_AUTO, token: str = "") -> Dict[str, Any]:
    """Send one follow-up and record it.

    Quotation / PI follow-ups reply in the enquiry's conversation unless the customer wrote last, and always
    carry the enquiry's share link and password. Marketing emails are NEW emails (each with its own subject,
    never in the enquiry's thread); they stop when the customer answered any of them, and a STOP reply marks
    the customer Do not contact.
    """
    marketing = stage == MARKETING_STAGE
    if not thread_id and not marketing:
        return _result(enquiry_id, SKIPPED, "no confirmed Gmail conversation")

    share_url = share_password = ""
    if not marketing:
        try:
            share_url, share_password = follow_up_links.for_follow_up(enquiry_id, enquiry, start=start)
        except follow_up_links.ShareLinkMissing as exc:
            logger.error("Follow-up %s for enquiry %s not sent: no share link (%s)", attempt, enquiry_id, exc)
            return _result(enquiry_id, FAILED, f"not sent: {exc}")

    gmail = GmailService(sender)
    original = None
    try:
        own = {a.lower() for a in gmail.own_addresses()}
        if customer_email.lower() in own:
            return _result(enquiry_id, SKIPPED, "customer address is one of our own addresses")
        if not own:
            return _result(enquiry_id, SKIPPED, "could not confirm the sending account's address")
        if marketing:
            for marketing_thread in _marketing_threads(enquiry_id, start):
                senders = [(m, parseaddr(m.get("sender") or "")[1].strip().lower())
                           for m in (gmail.get_thread_messages(marketing_thread, limit=50) or [])]
                replies = [m for m, address in senders if address and address not in own]
                if replies:
                    _note_marketing_reply(enquiry_id, sender, replies)
                    return _result(enquiry_id, SKIPPED, "customer replied; answer them manually")
        else:
            thread = gmail.get_thread_messages(thread_id, limit=50)
            if not thread:
                return _result(enquiry_id, SKIPPED, "Gmail conversation not found")
            last = thread[-1]
            last_sender = parseaddr(last.get("sender") or "")[1].strip().lower()
            if last_sender and last_sender not in own:
                return _result(enquiry_id, SKIPPED, "customer replied; answer them manually")
            original = gmail.get_message(last.get("id"))
    except GmailAPIError as exc:
        return _result(enquiry_id, FAILED, f"Gmail error: {exc}")

    title = str(enquiry.get("subject") or enquiry.get("project_name") or "")
    name = customer_name_for(enquiry_id, enquiry, customer_email)
    body = build_follow_up_body(
        stage, attempt=attempt, reference=enquiry_id, subject=title, user=sender, today=today,
        share_url=share_url, share_password=share_password, customer_name=name,
        document_number=(start.document_no if start is not None and not marketing else ""),
        delivery_timeline=str(enquiry.get("delivery_timeline") or ""),
        confirm_by_date=str(enquiry.get("confirm_by_date") or ""),
    )
    html_body = to_html(body)
    # The other customer emails, then the same CC as the last follow-up; never the customer or our own addresses.
    cc_list = customer_emails.with_customer_cc(
        enquiry_id, payment_terms.parse_cc(cc, exclude=customer_email)[0], to=customer_email, own=own,
        limit=payment_terms.MAX_CC)
    # Linked to a KYC customer: copy the team this stage concerns, and top management once it has escalated.
    cc_list = kyc_routing.add_cc(cc_list, enquiry_id, stage, attempt, to=customer_email, own=own,
                                 limit=payment_terms.MAX_CC)
    if marketing:  # its own new conversation, with this email's own subject
        subject = marketing_subject(attempt, customer_name=name, enquiry_title=title)
    else:
        subject = (original or {}).get("subject") or str(enquiry.get("subject") or "")
    next_on = next_follow_up_date(attempt, today, stage)
    details = {
        "customer_email": customer_email,
        "attempt": attempt,
        "stage": stage,
        "next_follow_up_on": next_on.isoformat() if next_on else None,
        "cc": cc_list,
    }

    if getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True):
        logger.info("Follow-up dry run for enquiry %s (attempt %s, %s) to %s, CC %s. Nothing was sent.\n"
                    "Subject: %s\n%s", enquiry_id, attempt, stage, customer_email, ", ".join(cc_list) or "none",
                    subject, body)
        return _result(enquiry_id, DRY_RUN, "dry run: nothing was sent", subject=subject, body=body, **details)

    # The unique token means one automatic follow-up per enquiry per day, even if a run is repeated.
    try:
        with transaction.atomic():
            record = FollowUpSent.objects.create(
                enquiry_id=enquiry_id,
                customer_email=customer_email,
                customer_name=str(enquiry.get("customer_name") or "").strip(),
                subject=subject,
                body=body,
                gmail_message_id=(original or {}).get("id") or "",
                gmail_thread_id=thread_id,
                sent_by=sender,
                status=FollowUpSent.STATUS_PENDING,
                submission_token=token or f"auto:{enquiry_id}:{today.isoformat()}",
                stage=stage,
                next_follow_up_on=next_on,
                source=source,
                cc=", ".join(cc_list),
            )
    except IntegrityError:
        return _result(enquiry_id, SKIPPED, "already handled today")

    try:
        if original is None:
            sent = gmail.send_email(customer_email, subject, body, cc=cc_list or None, html_body=html_body)
        else:
            sent = gmail.send_reply(original_message=original, body=body, subject=subject, to=customer_email,
                                    cc=cc_list or None, html_body=html_body)
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
    if stage == MARKETING_STAGE:
        _log_marketing(enquiry_id, sender, attempt, customer_email, manual=source == FollowUpSent.SOURCE_MANUAL)
    return _result(enquiry_id, SENT, "", **details)


def send_marketing_now(enquiry_id: str, *, customer_email: str, cc: Optional[List[str]] = None,
                       today: Optional[date] = None) -> Dict[str, Any]:
    """"Send first follow-up now" on a Marketing lead: sends Marketing Follow-up 1 at once.

    It counts as Follow-up 1, so the scheduler continues with Follow-up 2 and never sends #1 again. Sent from
    FOLLOW_UP_AUTO_SENDER (the mailbox the later follow-ups and the reply checks use); FOLLOW_UP_AUTO_DRY_RUN
    still applies.
    """
    enquiry_id = str(enquiry_id)
    today = today or timezone.localdate()
    sender = _sender()
    if sender is None:
        return _result(enquiry_id, FAILED, "FOLLOW_UP_AUTO_SENDER is not set to an active user")
    cycle = cycles([enquiry_id]).get(enquiry_id)
    if cycle is None or cycle.stage != MARKETING_STAGE:
        if not _valid_email(customer_email):
            return _result(enquiry_id, SKIPPED, "no customer email saved for this lead")
        record_start(enquiry_id, MARKETING_STAGE, sent={}, customer_email=customer_email, cc=cc or [], subject="",
                     user=sender)
        cycle = cycles([enquiry_id]).get(enquiry_id)
    if cycle is None or cycle.stage != MARKETING_STAGE:
        return _result(enquiry_id, FAILED, "could not start the Marketing follow-ups")
    if cycle.count >= 1:
        return _result(enquiry_id, SKIPPED, "Follow-up 1 was already sent")
    try:
        enquiry = services.fetch_enquiry(_api_root(), enquiry_id)
    except services.EnquiryServiceError as exc:
        return _result(enquiry_id, FAILED, f"could not load enquiry: {exc}")
    stopped = _marketing_stop_reason(enquiry_id, enquiry, cycle, manual=True)
    if stopped:
        return _result(enquiry_id, SKIPPED, stopped)
    if not _valid_email(cycle.customer_email):
        return _result(enquiry_id, SKIPPED, "no confirmed customer email")
    return _send(enquiry_id, enquiry, sender, today, thread_id="", customer_email=cycle.customer_email,
                 stage=MARKETING_STAGE, attempt=1, cc=cycle.cc, start=cycle.start,
                 source=FollowUpSent.SOURCE_MANUAL, token=f"marketing-first:{enquiry_id}:{cycle.start.pk if cycle.start else 0}")


def _log_marketing(enquiry_id: str, sender, attempt: int, customer_email: str, manual: bool = False) -> None:
    """Show the Marketing email on the lead's activity list."""
    try:
        from marketing.models import MarketingActivity, MarketingLead
    except Exception:
        return
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    final = attempt > follow_up_count(MARKETING_STAGE)
    what = "Closing email" if final else f"{'Follow-up' if manual else 'Automatic follow-up'} {attempt}"
    how = " (sent now with the button)" if manual else ""
    MarketingActivity.objects.create(lead=lead, kind=MarketingActivity.KIND_EMAIL, created_by=sender,
                                     text=f"{what} sent to {customer_email} as a new email{how}.")


def _note_marketing_reply(enquiry_id: str, sender, replies: List[Dict[str, Any]]) -> None:
    """The customer answered a Marketing email: the follow-ups stop. A reply starting with STOP marks the
    customer Do not contact. Noted once on the lead; no email to anyone."""
    try:
        from marketing import lost
        from marketing.models import MarketingActivity, MarketingLead
    except Exception:
        return
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    if any(lost.is_stop_reply(m.get("snippet")) for m in replies):
        lost.mark_do_not_contact(lead, sender, 'Marked Do not contact: the customer replied "STOP".')
    if getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True):
        return
    marker = lost.REPLY_NOTE
    if lead.activities.filter(text__startswith=marker).exists():
        return
    snippet = str(replies[0].get("snippet") or "").strip()[:300]
    MarketingActivity.objects.create(
        lead=lead, kind=MarketingActivity.KIND_SYSTEM, created_by=sender,
        text=f"{marker}: automatic follow-ups stopped." + (f' "{snippet}"' if snippet else ""))


def record_start(enquiry_id: str, stage: str, *, sent: Dict[str, Any], customer_email: str, cc: List[str],
                 subject: str, user=None, document_no: str = "", share_url: str = "",
                 share_password: str = "") -> Optional[FollowUpStart]:
    """Remember the email that starts a follow-up sequence (its Gmail thread is where follow-ups reply)."""
    thread_id = str((sent or {}).get("threadId") or "")
    if not thread_id and stage != MARKETING_STAGE:
        return None  # Marketing starts without a thread: its first email opens its own
    return FollowUpStart.objects.create(
        enquiry_id=str(enquiry_id), stage=stage, gmail_thread_id=thread_id,
        gmail_message_id=str((sent or {}).get("id") or ""), customer_email=(customer_email or "").lower(),
        cc=", ".join(cc or []), subject=(subject or "")[:500], document_no=(document_no or "")[:64],
        share_url=(share_url or "")[:500], share_password=(share_password or "")[:64],
        started_by=user if getattr(user, "is_authenticated", False) else None)

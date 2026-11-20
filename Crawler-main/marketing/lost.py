"""Which enquiries are lost, and the facts the Marketing portal shows about each one.

An enquiry is lost when it is:
- Rejected: its status is "rejected".
- No response: the closing email was sent (enquiries/status_emails.py), or every follow-up was sent
  (FOLLOW_UP_MAX_COUNT) and the customer stayed silent for FOLLOW_UP_CLOSE_AFTER_DAYS after the last one.
  This is the same rule the closing email uses, so the two never disagree.

Marketing waits MARKETING_WAIT_DAYS after the loss before the lead is "Ready to contact".
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

from django.conf import settings
from django.db.models import prefetch_related_objects
from django.utils import timezone

from dashboard.follow_up_templates import MARKETING_STAGE, PAYMENT_STAGE, follow_up_count
from enquiries.models import CustomerStatusEmail, EnquiryContact, EnquiryFollowup, FollowUpSent
from kyc import links as kyc_links
from kyc.models import CustomerContact

from .models import MarketingActivity, MarketingLead

# Written on the lead when the customer answers Marketing's automatic follow-up (the follow-ups then stop).
REPLY_NOTE = "Customer replied to Marketing's follow-up"

REJECTED = "rejected"
NO_RESPONSE = "no_response"
KIND_LABELS = {REJECTED: "Rejected", NO_RESPONSE: "No response"}

STAGE_WAITING = "waiting"
STAGE_READY = "ready"
STAGE_DO_NOT_CONTACT = "do_not_contact"
STAGE_REVIVED = "revived"
STAGE_LABELS = {
    STAGE_WAITING: "Waiting period",
    STAGE_READY: "Ready to contact",
    STAGE_DO_NOT_CONTACT: "Do not contact",
    STAGE_REVIVED: "Revived",
}

REASON_LABELS = dict(MarketingLead.REASON_CHOICES)

SHARE_REASON_MARKER = "Rejection reason (via share link):"
# Email-subject starts that show a "customer name" is really an email subject or document title.
_SUBJECT_PREFIX = re.compile(r"^\s*(re|fw|fwd|aw|wg|rfq|rfi|po|pi|enquiry|inquiry|quotation|quote)\b\s*[:#_\-]", re.I)
_PERSONAL_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
                  "live.com", "icloud.com", "rediffmail.com", "aol.com", "proton.me", "protonmail.com"}


def customer_reason_from_remarks(remarks: str) -> str:
    """Everything the customer typed on the share link (the last time), not only its first line."""
    text = str(remarks or "")
    at = text.rfind(SHARE_REASON_MARKER)
    if at < 0:
        return ""
    reason = " ".join(text[at + len(SHARE_REASON_MARKER):].split())
    return reason[:1000]


def looks_like_subject(name: str, subject: str = "") -> bool:
    """True when a stored "customer name" is really an email subject or file title."""
    value = " ".join(str(name or "").split())
    if not value:
        return False
    if subject and value.lower() == " ".join(str(subject).split()).lower() and len(value) > 30:
        return True
    return bool(_SUBJECT_PREFIX.match(value)) or len(value) > 80


def company_from_email(address: str) -> str:
    """The company part of a work email address (buyer@acme.co.in -> acme.co.in); '' for personal mail."""
    domain = str(address or "").rsplit("@", 1)[-1].strip().lower() if "@" in str(address or "") else ""
    return "" if not domain or domain in _PERSONAL_MAIL else domain


def customer_display(enquiry_name: str, subject: str, email: str, marketing_name: str = "") -> Dict[str, str]:
    """Best customer name to show, where it came from, and the name to greet the customer with in an email."""
    marketing_name = " ".join(str(marketing_name or "").split())
    if marketing_name:
        return {"name": marketing_name, "source": "marketing", "greeting": marketing_name}
    enquiry_name = " ".join(str(enquiry_name or "").split())
    if enquiry_name and not looks_like_subject(enquiry_name, subject):
        return {"name": enquiry_name, "source": "enquiry", "greeting": enquiry_name}
    company = company_from_email(email)
    if company:
        return {"name": company, "source": "email", "greeting": ""}
    return {"name": "", "source": "", "greeting": ""}


def kyc_campaign_emails(customer) -> List[str]:
    """KYC contacts a campaign goes to: the purchaser and the Sales and marketing team (with an email)."""
    if customer is None:
        return []
    out: List[str] = []
    contacts = list(customer.contacts.all())
    # Purchaser first (the same contact KYC uses as the main enquiry email), then the Sales team.
    for pick in (lambda c: c.is_purchaser, lambda c: c.category == CustomerContact.CATEGORY_SALES):
        for contact in contacts:
            if not pick(contact):
                continue
            # A contact may hold several comma-separated addresses.
            for part in str(contact.email or "").split(","):
                address = part.strip().lower()
                if address and address not in out:
                    out.append(address)
    return out


def auto_follow_up_status(kind: str, lead: Optional[MarketingLead], cycle, replied: bool) -> Optional[Dict[str, Any]]:
    """Where Marketing's automatic follow-ups stand for a rejected enquiry, for the lists:
    {"state", "label", "sent", "limit", "next_on"}; None when there are none (not rejected, or not started)."""
    if kind != REJECTED or cycle is None or cycle.stage != MARKETING_STAGE:
        return None
    limit = follow_up_count(MARKETING_STAGE)
    sent = cycle.count
    state, label = "running", ""
    if lead is not None and lead.do_not_contact:
        state, label = "stopped", "Stopped: do not contact"
    elif lead is not None and (lead.revived_enquiry_id or lead.accepted_at):
        state, label = "stopped", "Stopped: won back"
    elif replied:
        state, label = "replied", "Customer replied: follow-ups stopped"
    elif sent > limit:
        state, label = "closed", "Closing email sent"
    elif sent == 0:
        label = f"First follow-up {cycle.next_on:%d %b}" if cycle.next_on else "First follow-up due"
    elif sent < limit:
        label = f"{sent} of {limit} sent" + (f", next {cycle.next_on:%d %b}" if cycle.next_on else "")
    else:
        label = f"{limit} of {limit} sent" + (f", closing email {cycle.next_on:%d %b}" if cycle.next_on else "")
    return {"state": state, "label": label, "sent": min(sent, limit), "limit": limit,
            "next_on": cycle.next_on if state == "running" else None}


def wait_days() -> int:
    return max(int(getattr(settings, "MARKETING_WAIT_DAYS", 7) or 0), 0)


def financial_year_label(value: date) -> str:
    """Indian financial year (1 April - 31 March), e.g. 2026-27."""
    start = value.year if value.month >= 4 else value.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def _status(enquiry: Mapping[str, Any]) -> str:
    return str(enquiry.get("status") or "").strip().lower()


def _local_date(value) -> Optional[date]:
    if value is None:
        return None
    if hasattr(value, "tzinfo") and getattr(value, "tzinfo", None) is not None:
        return timezone.localtime(value).date()
    return value.date() if hasattr(value, "date") else value


def _follow_up_stats(enquiry_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = {}
    rows = (FollowUpSent.objects.filter(enquiry_id__in=enquiry_ids, status=FollowUpSent.STATUS_SENT)
            .exclude(stage__in=(PAYMENT_STAGE, MARKETING_STAGE)).values_list("enquiry_id", "sent_at"))
    for enquiry_id, sent_at in rows:
        item = stats.setdefault(enquiry_id, {"count": 0, "last": sent_at, "last_type": "Email"})
        item["count"] += 1
        if sent_at > item["last"]:
            item["last"], item["last_type"] = sent_at, "Email"
    # Follow-ups logged by hand on the enquiry (call, meeting, other) count too, with their type.
    logged = EnquiryFollowup.objects.filter(enquiry_id__in=enquiry_ids).values_list(
        "enquiry_id", "created_at", "followup_type")
    labels = dict(EnquiryFollowup.TYPE_CHOICES)
    for enquiry_id, created_at, kind in logged:
        item = stats.setdefault(enquiry_id, {"count": 0, "last": created_at, "last_type": labels.get(kind, "")})
        item["count"] += 1
        if created_at > item["last"]:
            item["last"], item["last_type"] = created_at, labels.get(kind, "")
    return stats


def _status_email_dates(enquiry_ids: List[str], kind: str, sent_only: bool) -> Dict[str, Any]:
    rows = CustomerStatusEmail.objects.filter(enquiry_id__in=enquiry_ids, kind=kind)
    if sent_only:
        rows = rows.filter(status=CustomerStatusEmail.STATUS_SENT)
    dates: Dict[str, Any] = {}
    for enquiry_id, sent_at, created_at in rows.values_list("enquiry_id", "sent_at", "created_at"):
        dates[enquiry_id] = sent_at or created_at
    return dates


def _price_text(enquiry: Mapping[str, Any]) -> str:
    currency = str(enquiry.get("currency") or "").strip()
    for key, unit in (("final_price_per_piece", "pc"), ("final_price_per_meter", "m"),
                      ("total_price_per_piece", "pc"), ("price_per_piece", "pc")):
        value = enquiry.get(key)
        if value in (None, "", 0):
            continue
        try:
            amount = f"{float(value):,.2f}"
        except (TypeError, ValueError):
            continue
        return f"{currency} {amount}/{unit}".strip()
    return ""


def build_lost_leads(enquiries: Iterable[Mapping[str, Any]], today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Every lost enquiry with what marketing needs to know, newest loss first."""
    today = today or timezone.localdate()
    candidates = {str(e.get("enquiry_id")): e for e in enquiries
                  if e.get("enquiry_id") and _status(e) in {"open", "closed", "rejected"}}
    if not candidates:
        return []
    ids = list(candidates)
    follow_ups = _follow_up_stats(ids)
    closing_sent = _status_email_dates(ids, CustomerStatusEmail.KIND_CLOSING, sent_only=True)
    rejection_seen = _status_email_dates(ids, CustomerStatusEmail.KIND_REJECTION, sent_only=False)
    limit = int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
    silent_days = int(getattr(settings, "FOLLOW_UP_CLOSE_AFTER_DAYS", 7) or 0)
    now = timezone.now()

    found: Dict[str, Dict[str, Any]] = {}
    for enquiry_id, enquiry in candidates.items():
        stats = follow_ups.get(enquiry_id, {"count": 0, "last": None, "last_type": ""})
        kind = None
        lost_at = None
        if _status(enquiry) == "rejected":
            kind = REJECTED
            lost_at = rejection_seen.get(enquiry_id)
        elif enquiry_id in closing_sent:
            kind = NO_RESPONSE
            lost_at = closing_sent[enquiry_id]
        elif (limit and stats["count"] >= limit and stats["last"]
              and stats["last"] <= now - timedelta(days=silent_days)):
            kind = NO_RESPONSE
            lost_at = stats["last"] + timedelta(days=silent_days)
        if kind is None:
            continue
        found[enquiry_id] = {"enquiry": enquiry, "kind": kind, "lost_at": lost_at, "follow_ups": stats}

    if not found:
        return []
    marketing = {lead.enquiry_id: lead for lead in MarketingLead.objects.filter(enquiry_id__in=list(found))}
    emails: Dict[str, str] = {}
    for enquiry_id, address in (FollowUpSent.objects.filter(enquiry_id__in=list(found), status=FollowUpSent.STATUS_SENT)
                                .exclude(customer_email="").order_by("sent_at")
                                .values_list("enquiry_id", "customer_email")):
        emails[enquiry_id] = address  # latest wins
    others: Dict[str, List[str]] = {}
    for enquiry_id, address, more in EnquiryContact.objects.filter(enquiry_id__in=list(found)).values_list(
            "enquiry_id", "customer_email", "other_customer_emails"):
        if address:
            emails[enquiry_id] = address  # the address sales saved wins
        others[enquiry_id] = [a.strip().lower() for a in (more or "").split(",") if a.strip()]
    # KYC customer linked to the enquiry: its code, name and Sales contacts fill the gaps.
    kyc_customers = kyc_links.customers_for(list(found))
    prefetch_related_objects(list({c.pk: c for c in kyc_customers.values()}.values()), "contacts")
    for enquiry_id, customer in kyc_customers.items():
        kyc_emails = kyc_campaign_emails(customer)
        if not emails.get(enquiry_id) and kyc_emails:
            emails[enquiry_id] = kyc_emails[0]
        for address in kyc_emails:
            if address not in others.setdefault(enquiry_id, []):
                others[enquiry_id].append(address)

    from enquiries import auto_follow_up  # Marketing's automatic follow-ups share the enquiry follow-up engine

    mk_cycles = {eid: c for eid, c in auto_follow_up.cycles(list(found)).items() if c.stage == MARKETING_STAGE}
    replied_ids = set(MarketingActivity.objects.filter(
        lead__enquiry_id__in=list(mk_cycles), text__startswith=REPLY_NOTE).values_list("lead__enquiry_id", flat=True))

    leads: List[Dict[str, Any]] = []
    for enquiry_id, item in found.items():
        enquiry = item["enquiry"]
        lead = marketing.get(enquiry_id)
        lost_on = _local_date(item["lost_at"]) or _local_date(enquiry.get("received_at_dt"))
        ready_on = lost_on + timedelta(days=wait_days()) if lost_on else None

        if lead and lead.revived_enquiry_id:
            stage = STAGE_REVIVED
        elif lead and lead.do_not_contact:
            stage = STAGE_DO_NOT_CONTACT
        elif ready_on and ready_on > today:
            stage = STAGE_WAITING
        else:
            stage = STAGE_READY

        reason_key = lead.lost_reason if lead and lead.lost_reason else ""
        if not reason_key and item["kind"] == NO_RESPONSE:
            reason_key = "no_reply"
        received = _local_date(enquiry.get("received_at_dt"))
        who = customer_display(enquiry.get("customer_name"), enquiry.get("subject"), emails.get(enquiry_id, ""),
                               lead.customer_name if lead else "")
        kyc_customer = kyc_customers.get(enquiry_id)
        if kyc_customer is not None and who["source"] in ("", "email"):
            who = {"name": kyc_customer.name, "source": "kyc", "greeting": kyc_customer.name}

        leads.append({
            "enquiry_id": enquiry_id,
            "customer_name": who["name"],
            "customer_name_source": who["source"],  # marketing / enquiry / email / "" (not recorded)
            "greeting_name": who["greeting"],  # only a real name; campaigns fall back to "Sir/Madam"
            "raw_customer_name": enquiry.get("customer_name") or "",
            "subject": enquiry.get("subject") or "",
            "project_name": enquiry.get("project_name") or "",
            "assigned_to": enquiry.get("assigned_to") or "",
            "customer_email": emails.get(enquiry_id, ""),
            # More customer addresses, CC'd on campaign emails (never the main address again).
            "other_emails": [a for a in others.get(enquiry_id, []) if a != emails.get(enquiry_id, "").lower()],
            "kind": item["kind"],
            "kind_label": KIND_LABELS[item["kind"]],
            "reason_key": reason_key,
            "reason_label": REASON_LABELS.get(reason_key, "")
            + (f": {lead.lost_reason_note}" if reason_key == "other" and lead and lead.lost_reason_note else ""),
            "customer_reason": customer_reason_from_remarks(enquiry.get("remarks")),  # typed on the share link
            "price": _price_text(enquiry),
            "follow_up_count": item["follow_ups"]["count"],
            "last_follow_up": item["follow_ups"]["last"],
            "last_follow_up_type": item["follow_ups"].get("last_type", ""),
            "lost_on": lost_on,
            "ready_on": ready_on,
            "received_on": received,
            "fin_year": financial_year_label(lost_on or received) if (lost_on or received) else "",
            "stage": stage,
            "stage_label": STAGE_LABELS[stage],
            "revived_enquiry_id": lead.revived_enquiry_id if lead else "",
            "lead": lead,
            "auto_follow_up": auto_follow_up_status(item["kind"], lead, mk_cycles.get(enquiry_id),
                                                    enquiry_id in replied_ids),
            "kyc_customer": kyc_customer,
            "kyc_code": kyc_customer.code if kyc_customer is not None else "",
        })
    leads.sort(key=lambda row: (row["lost_on"] or date.min, row["enquiry_id"]), reverse=True)
    return leads


FILTER_KEYS = ("q", "kind", "reason", "stage", "fin_year")


def filter_leads(leads: List[Dict[str, Any]], params) -> tuple:
    """Apply the Marketing list filters. Returns (matching rows, cleaned filter values)."""
    chosen = {
        "q": (params.get("q") or "").strip(),
        "kind": (params.get("kind") or "").strip(),
        "reason": (params.get("reason") or "").strip(),
        "stage": (params.get("stage") or "").strip(),
        "fin_year": (params.get("fin_year") or "").strip(),
    }
    if chosen["kind"] not in KIND_LABELS:
        chosen["kind"] = ""
    if chosen["stage"] not in STAGE_LABELS:
        chosen["stage"] = ""
    if chosen["reason"] not in REASON_LABELS and chosen["reason"] != "none":
        chosen["reason"] = ""

    query = chosen["q"].lower()
    rows = []
    for row in leads:
        if chosen["kind"] and row["kind"] != chosen["kind"]:
            continue
        if chosen["stage"] and row["stage"] != chosen["stage"]:
            continue
        if chosen["reason"] == "none" and row["reason_key"]:
            continue
        if chosen["reason"] not in ("", "none") and row["reason_key"] != chosen["reason"]:
            continue
        if chosen["fin_year"] and row["fin_year"] != chosen["fin_year"]:
            continue
        if query:
            haystack = " ".join(str(row[k]) for k in (
                "enquiry_id", "customer_name", "raw_customer_name", "subject", "project_name", "customer_email",
                "assigned_to", "other_emails", "kyc_code",
            )).lower()
            if query not in haystack:
                continue
        rows.append(row)
    return rows, chosen


def summarise(leads: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_reason: Dict[str, int] = {}
    for row in leads:
        label = row["reason_label"] or "Reason not recorded"
        by_reason[label] = by_reason.get(label, 0) + 1
    return {
        "total": len(leads),
        "rejected": sum(1 for r in leads if r["kind"] == REJECTED),
        "no_response": sum(1 for r in leads if r["kind"] == NO_RESPONSE),
        "ready": sum(1 for r in leads if r["stage"] == STAGE_READY),
        "waiting": sum(1 for r in leads if r["stage"] == STAGE_WAITING),
        "revived": sum(1 for r in leads if r["stage"] == STAGE_REVIVED),
        # Same rule as the "Do not contact" stage filter, so the card and the filtered list always agree.
        "do_not_contact": sum(1 for r in leads if r["stage"] == STAGE_DO_NOT_CONTACT),
        "by_reason": sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0])),
    }

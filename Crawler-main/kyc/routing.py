"""Which of the customer's teams an enquiry email concerns, and when top management is copied in.

The enquiry's follow-up emails stay in their Gmail conversation with the person already in "To".
Routing only adds people in CC, taken from the KYC contacts of the customer the enquiry is linked to:

- payment reminders          -> the customer's Accounts contacts
- every other follow-up      -> the customer's purchaser and Sales contacts
- after KYC_ESCALATE_AFTER_REMINDERS emails with no reply, the next ones also copy Top management

An enquiry that is not linked to a KYC customer is left exactly as it was.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from django.conf import settings

from .models import Customer, CustomerContact, EnquiryCustomer

# Follow-up stage (dashboard/follow_up_templates.py) -> team; stages not listed go to Sales.
STAGE_TEAM = {"payment_pending": CustomerContact.CATEGORY_ACCOUNTS}
DEFAULT_TEAM = CustomerContact.CATEGORY_SALES
TEAM_LABELS = dict(CustomerContact.CATEGORY_CHOICES)


def team_for_stage(stage: str) -> str:
    return STAGE_TEAM.get(str(stage or ""), DEFAULT_TEAM)


def escalate_after() -> int:
    """Emails sent without a reply before top management is copied in; 0 turns escalation off."""
    return max(int(getattr(settings, "KYC_ESCALATE_AFTER_REMINDERS", 2) or 0), 0)


def is_escalated(attempt: int) -> bool:
    limit = escalate_after()
    return bool(limit) and int(attempt or 0) > limit


def _unique(addresses: Iterable[str]) -> List[str]:
    out: List[str] = []
    for address in addresses:
        address = str(address or "").strip().lower()
        if address and address not in out:
            out.append(address)
    return out


def team_emails(customer: Customer, team: str) -> List[str]:
    """Emails of the customer's contacts in `team`. For Sales the purchaser comes first, whatever their team."""
    contacts = [c for c in customer.contacts.all() if c.email]
    picked: List[str] = []
    for c in contacts:
        if c.category == team:
            picked.extend(c.email_addresses())
    if team == CustomerContact.CATEGORY_SALES:
        purchaser = [e for c in contacts if c.is_purchaser for e in c.email_addresses()]
        picked = purchaser + picked
    return _unique(picked)


def customer_for_enquiry(enquiry_id: Any) -> Optional[Customer]:
    enquiry_id = str(enquiry_id or "").strip()
    if not enquiry_id:
        return None
    link = EnquiryCustomer.objects.filter(enquiry_id=enquiry_id).select_related("customer").first()
    return link.customer if link else None


def route(customer: Optional[Customer], stage: str, attempt: int = 1) -> Dict[str, Any]:
    """Who to copy on follow-up number `attempt` of this stage."""
    team = team_for_stage(stage)
    escalated = is_escalated(attempt)
    return {
        "customer": customer,
        "team": team,
        "team_label": TEAM_LABELS.get(team, team),
        "team_emails": team_emails(customer, team) if customer else [],
        "escalated": escalated,
        "escalation_emails": (team_emails(customer, CustomerContact.CATEGORY_MANAGEMENT)
                              if customer and escalated else []),
    }


def add_cc(base: Iterable[str], enquiry_id: Any, stage: str, attempt: int = 1, *, to: str = "",
           own: Iterable[str] = (), limit: int = 10) -> List[str]:
    """`base` CC plus the routed contacts; never the To address, our own addresses or duplicates.

    When there are more than `limit` addresses the routed contacts are kept and the oldest extras dropped.
    """
    skip = {str(to or "").strip().lower()} | {str(a).strip().lower() for a in own or []}
    base = [a for a in _unique(base) if a not in skip]
    customer = customer_for_enquiry(enquiry_id)
    if customer is None:
        return base[:limit]
    plan = route(customer, stage, attempt)
    routed = [a for a in _unique(plan["team_emails"] + plan["escalation_emails"]) if a not in skip][:limit]
    kept = [a for a in base if a not in routed][:max(limit - len(routed), 0)]
    return kept + routed


def describe(enquiry_id: Any, stage: str, attempt: int = 1) -> str:
    """One line for the compose page saying who was added and why; '' when the enquiry is not linked."""
    customer = customer_for_enquiry(enquiry_id)
    if customer is None:
        return ""
    plan = route(customer, stage, attempt)
    if plan["team_emails"]:
        text = f"{plan['team_label']} contacts of {customer.code} are copied."
    else:
        text = f"{customer.code} has no {plan['team_label']} contact with an email in KYC."
    if plan["escalated"]:
        text += (" Top management is copied too (follow-up #%d)." % attempt if plan["escalation_emails"]
                 else " Top management would be copied now, but KYC has no such contact with an email.")
    elif escalate_after():
        text += f" Top management is copied from follow-up #{escalate_after() + 1}."
    return text

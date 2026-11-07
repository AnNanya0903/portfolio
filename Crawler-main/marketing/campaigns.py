"""Campaign emails to lost customers.

Safety rules (every one is checked again at the moment of sending):
- Only customers who are "Ready to contact": the waiting period after the loss is over, not Do not contact,
  not already revived, and with a valid email address.
- If any lost enquiry with the same email address is Do not contact, that address is never emailed.
- One email per address per campaign, and no address is emailed again within MARKETING_CAMPAIGN_GAP_DAYS
  of an earlier campaign email.
- At most MARKETING_CAMPAIGN_MAX_RECIPIENTS addresses per campaign.
- MARKETING_CAMPAIGN_DRY_RUN (on by default) records what would be sent and sends nothing.
- Emails go from the Gmail account of the person who clicks Send, as a new conversation.
"""
from __future__ import annotations

import base64
import logging
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Dict, List, Mapping, Optional, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from dashboard.follow_up_templates import sender_signature
from dashboard.gmail_service import GmailAPIError, GmailService

from . import lost
from .models import Campaign, CampaignRecipient, MarketingActivity, MarketingLead

logger = logging.getLogger(__name__)

AUDIENCE_FILTER_KEYS = ("q", "kind", "reason", "fin_year")  # stage is always "Ready to contact"

PLACEHOLDERS = {
    "{customer_name}": "the customer's company name",
    "{enquiry_subject}": "what they enquired about",
    "{reference}": "the lost enquiry number",
}

OPT_OUT_LINE = ("If you would prefer not to receive these emails, simply reply with \"unsubscribe\" "
                "and we will not contact you again.")

STARTER_TEMPLATES: Dict[str, Dict[str, str]] = {
    "price": {
        "label": "Revised price offer",
        "subject": "A revised offer for {enquiry_subject}",
        "body": ("Dear {customer_name} team,\n\n"
                 "Some time ago you asked us about {enquiry_subject} (our reference {reference}). "
                 "We have since reviewed our costs and can now offer a better price for this requirement.\n\n"
                 "If the requirement is still open, please reply to this email and we will send you an "
                 "updated quotation."),
    },
    "capability": {
        "label": "New capability",
        "subject": "New capabilities at AVIGIRI",
        "body": ("Dear {customer_name} team,\n\n"
                 "Thank you for your earlier enquiry about {enquiry_subject} (our reference {reference}). "
                 "We wanted to let you know that we have added new capabilities which may suit your needs.\n\n"
                 "If you have a current or upcoming requirement, please reply to this email and our team "
                 "will be glad to help."),
    },
    "checking_in": {
        "label": "Checking in",
        "subject": "Checking in on {enquiry_subject}",
        "body": ("Dear {customer_name} team,\n\n"
                 "We are checking in about your earlier enquiry for {enquiry_subject} (our reference {reference}). "
                 "If you have a new or revised requirement, we would be glad to quote again.\n\n"
                 "Please reply to this email with any details and our team will get back to you."),
    },
}


class CampaignError(RuntimeError):
    """Something stops the whole campaign from being sent (shown to the user)."""


def max_recipients() -> int:
    return max(int(getattr(settings, "MARKETING_CAMPAIGN_MAX_RECIPIENTS", 50) or 0), 1)


def gap_days() -> int:
    return max(int(getattr(settings, "MARKETING_CAMPAIGN_GAP_DAYS", 30) or 0), 0)


def dry_run() -> bool:
    return bool(getattr(settings, "MARKETING_CAMPAIGN_DRY_RUN", True))


def _valid_email(value: str) -> bool:
    try:
        validate_email(value or "")
    except ValidationError:
        return False
    return True


def render_email(subject: str, body: str, row: Mapping[str, Any]) -> Tuple[str, str]:
    """Fill the placeholders and add the opt-out line and the signature."""
    values = {
        "{customer_name}": (row.get("customer_name") or "").strip() or "Sir/Madam",
        "{enquiry_subject}": (row.get("subject") or "").strip() or "your requirement",
        "{reference}": str(row.get("enquiry_id") or "").strip() or "-",
    }
    for key, value in values.items():
        subject = subject.replace(key, value)
        body = body.replace(key, value)
    full_body = "\n\n".join([body.strip(), OPT_OUT_LINE, sender_signature()])
    return " ".join(subject.split())[:300], full_body


def clean_filters(values: Mapping[str, Any]) -> Dict[str, str]:
    _, chosen = lost.filter_leads([], values)
    return {key: chosen[key] for key in AUDIENCE_FILTER_KEYS if chosen.get(key)}


def build_audience(leads: List[Dict[str, Any]], filters: Mapping[str, Any]) -> Dict[str, Any]:
    """Who would get this campaign now: {"recipients": [...], "skipped": [...]}. Each item has the lead row."""
    rows, _ = lost.filter_leads(leads, {key: filters.get(key, "") for key in AUDIENCE_FILTER_KEYS})
    # Do not contact applies to the email address, whichever lost enquiry it was set on.
    blocked = {str(r["customer_email"]).strip().lower() for r in leads
               if r["stage"] == lost.STAGE_DO_NOT_CONTACT and r["customer_email"]}
    recent = {}
    if gap_days():
        since = timezone.now() - timedelta(days=gap_days())
        for address, sent_at, name in (CampaignRecipient.objects
                                       .filter(status=CampaignRecipient.STATUS_SENT, sent_at__gte=since)
                                       .values_list("customer_email", "sent_at", "campaign__name")):
            recent[address.lower()] = (sent_at, name)

    recipients: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    chosen_emails: Dict[str, str] = {}
    for row in rows:
        address = str(row["customer_email"] or "").strip().lower()
        reason = ""
        if row["stage"] == lost.STAGE_REVIVED:
            reason = f"already revived as {row['revived_enquiry_id']}"
        elif row["stage"] == lost.STAGE_DO_NOT_CONTACT:
            reason = "Do not contact"
        elif row["stage"] == lost.STAGE_WAITING:
            reason = f"waiting period until {row['ready_on']:%d %b %Y}" if row["ready_on"] else "waiting period"
        elif not address or not _valid_email(address):
            reason = "no customer email saved"
        elif address in blocked:
            reason = "Do not contact (set on another enquiry from this email)"
        elif address in recent:
            sent_at, name = recent[address]
            reason = f"already emailed on {timezone.localtime(sent_at):%d %b %Y} (campaign \"{name}\")"
        elif address in chosen_emails:
            reason = f"same email as {chosen_emails[address]}, which gets the email"
        if reason:
            skipped.append({"row": row, "reason": reason})
            continue
        chosen_emails[address] = row["enquiry_id"]
        recipients.append({"row": row, "email": address})
    return {"recipients": recipients, "skipped": skipped}


def _log(enquiry_id: str, user, text: str) -> None:
    lead, _ = MarketingLead.objects.get_or_create(enquiry_id=enquiry_id)
    MarketingActivity.objects.create(lead=lead, kind=MarketingActivity.KIND_EMAIL, text=text, created_by=user)


def send_new_email(gmail: GmailService, to: str, subject: str, body: str) -> Dict[str, Any]:
    """Send a new email (not a reply) from the signed-in Gmail account."""
    if not gmail.service and not gmail.authenticate():
        raise GmailAPIError("Gmail sign-in failed")
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    try:
        return gmail.service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:  # googleapiclient HttpError and network errors
        raise GmailAPIError(f"Gmail could not send the email: {exc}") from exc


def _still_allowed(enquiry_id: str, address: str, campaign_id: Optional[int] = None) -> str:
    """Last check just before one email goes out. Returns why not, or ''."""
    if MarketingLead.objects.filter(enquiry_id=enquiry_id).exclude(revived_enquiry_id="").exists():
        return "revived before sending"
    if MarketingLead.objects.filter(enquiry_id=enquiry_id, do_not_contact=True).exists():
        return "marked Do not contact before sending"
    if gap_days():
        since = timezone.now() - timedelta(days=gap_days())
        if (CampaignRecipient.objects.filter(customer_email__iexact=address, status=CampaignRecipient.STATUS_SENT,
                                             sent_at__gte=since).exclude(campaign_id=campaign_id).exists()):
            return f"emailed by another campaign in the last {gap_days()} days"
    return ""


def send_campaign(campaign: Campaign, user, leads: List[Dict[str, Any]], expected_count: Optional[int] = None
                  ) -> Dict[str, int]:
    """Send a draft campaign, or retry its failed / test-mode emails. Returns counts by result."""
    testing = dry_run()
    if campaign.status == Campaign.STATUS_DRAFT:
        audience = build_audience(leads, campaign.filters)["recipients"]
        if not audience:
            raise CampaignError("Nobody is ready to receive this campaign. Check the filters.")
        if len(audience) > max_recipients():
            raise CampaignError(f"{len(audience)} customers match, but a campaign can go to at most "
                                f"{max_recipients()}. Narrow the filters.")
        if expected_count is not None and expected_count != len(audience):
            raise CampaignError("The list of customers changed since you opened this page. "
                                "Please check it again before sending.")
        with transaction.atomic():
            locked = Campaign.objects.select_for_update().get(pk=campaign.pk)
            if locked.status != Campaign.STATUS_DRAFT:
                raise CampaignError("This campaign was already sent.")
            for item in audience:
                row = item["row"]
                subject, body = render_email(campaign.subject, campaign.body, row)
                try:
                    with transaction.atomic():
                        CampaignRecipient.objects.create(
                            campaign=campaign, customer_email=item["email"], enquiry_id=row["enquiry_id"],
                            customer_name=(row["customer_name"] or "")[:320], subject=subject, body=body)
                except IntegrityError:
                    continue
            locked.status = Campaign.STATUS_SENT
            locked.sent_at = timezone.now()
            locked.sent_by = user
            locked.save(update_fields=["status", "sent_at", "sent_by", "updated_at"])
        campaign.refresh_from_db()
        to_send = list(campaign.recipients.filter(status=CampaignRecipient.STATUS_PENDING))
    else:
        if testing:
            raise CampaignError("Test mode is still on, so nothing would be sent.")
        to_send = list(campaign.recipients.filter(
            status__in=(CampaignRecipient.STATUS_FAILED, CampaignRecipient.STATUS_DRY_RUN)))
        if not to_send:
            raise CampaignError("There is nothing left to send in this campaign.")

    gmail = None if testing else GmailService(user)
    if gmail is not None:
        try:
            if not gmail.authenticate():
                raise GmailAPIError("Gmail sign-in failed")
        except GmailAPIError as exc:
            CampaignRecipient.objects.filter(pk__in=[r.pk for r in to_send]).update(
                status=CampaignRecipient.STATUS_FAILED, note=f"Gmail error: {exc}"[:500])
            raise CampaignError(f"Could not use your Gmail account: {exc}. Sign in with Google again and retry.")

    counts = {"sent": 0, "dry_run": 0, "failed": 0, "skipped": 0}
    for recipient in to_send:
        stop = _still_allowed(recipient.enquiry_id, recipient.customer_email, campaign.pk)
        if stop:
            recipient.status = CampaignRecipient.STATUS_FAILED
            recipient.note = f"Not sent: {stop}"
            recipient.save(update_fields=["status", "note"])
            counts["skipped"] += 1
            continue
        if testing:
            recipient.status = CampaignRecipient.STATUS_DRY_RUN
            recipient.note = "Test mode: nothing was sent"
            recipient.save(update_fields=["status", "note"])
            counts["dry_run"] += 1
            continue
        try:
            sent = send_new_email(gmail, recipient.customer_email, recipient.subject, recipient.body)
        except GmailAPIError as exc:
            recipient.status = CampaignRecipient.STATUS_FAILED
            recipient.note = str(exc)[:500]
            recipient.save(update_fields=["status", "note"])
            counts["failed"] += 1
            logger.warning("Campaign %s email to %s failed: %s", campaign.pk, recipient.customer_email, exc)
            continue
        recipient.status = CampaignRecipient.STATUS_SENT
        recipient.note = ""
        recipient.sent_at = timezone.now()
        recipient.gmail_message_id = (sent or {}).get("id") or ""
        recipient.gmail_thread_id = (sent or {}).get("threadId") or ""
        recipient.save(update_fields=["status", "note", "sent_at", "gmail_message_id", "gmail_thread_id"])
        _log(recipient.enquiry_id, user, f"Campaign email \"{campaign.name}\" sent to {recipient.customer_email}.")
        counts["sent"] += 1
    return counts


def _sender_address(message: Mapping[str, Any]) -> str:
    return parseaddr(str(message.get("reply_to") or message.get("sender") or ""))[1].strip().lower()


def check_replies(campaign: Campaign, user) -> Dict[str, int]:
    """Look in Gmail for customer replies to this campaign's emails.

    A reply that contains "unsubscribe" turns on Do not contact for that lost enquiry.
    """
    waiting = list(campaign.recipients.filter(status=CampaignRecipient.STATUS_SENT, replied_at__isnull=True)
                   .exclude(gmail_thread_id=""))
    counts = {"checked": len(waiting), "replied": 0, "unsubscribed": 0}
    if not waiting:
        campaign.replies_checked_at = timezone.now()
        campaign.save(update_fields=["replies_checked_at", "updated_at"])
        return counts
    gmail = GmailService(user)
    try:
        own = {a.lower() for a in gmail.own_addresses()}
    except Exception as exc:
        raise CampaignError(f"Could not use your Gmail account: {exc}")
    if not own:
        raise CampaignError("Could not confirm your Gmail address, so replies cannot be told apart.")
    for recipient in waiting:
        try:
            thread = gmail.get_thread_messages(recipient.gmail_thread_id, limit=50) or []
        except GmailAPIError as exc:
            raise CampaignError(f"Could not read Gmail: {exc}")
        replies = [m for m in thread if _sender_address(m) and _sender_address(m) not in own]
        if not replies:
            continue
        reply = replies[0]
        recipient.replied_at = reply.get("parsed_date") or timezone.now()
        recipient.save(update_fields=["replied_at"])
        counts["replied"] += 1
        text = " ".join(str(m.get("snippet") or "") for m in replies).lower()
        snippet = str(reply.get("snippet") or "").strip()[:300]
        _log(recipient.enquiry_id, user,
             f"Customer replied to campaign \"{campaign.name}\"" + (f": \"{snippet}\"" if snippet else "."))
        if "unsubscribe" in text:
            lead, _ = MarketingLead.objects.get_or_create(enquiry_id=recipient.enquiry_id)
            if not lead.do_not_contact:
                lead.do_not_contact = True
                lead.updated_by = user
                lead.save(update_fields=["do_not_contact", "updated_by", "updated_at"])
                MarketingActivity.objects.create(lead=lead, kind=MarketingActivity.KIND_SYSTEM, created_by=user,
                                                 text="Marked Do not contact: the customer replied \"unsubscribe\".")
            counts["unsubscribed"] += 1
    campaign.replies_checked_at = timezone.now()
    campaign.save(update_fields=["replies_checked_at", "updated_at"])
    return counts

"""The one email service for the portals.

- `send_email` checks the addresses and text, sends a new email (To, CC, BCC) from the signed-in user's Gmail,
  and logs it in EmailLog. Every feature that sends mail from a portal should use it.
- `internal_template` and `customer_template` fill in the Marketing “Email internal” and “Email to customer”
  emails from the enquiry and the KYC customer.
- `compose_config` gives the shared compose box (kyc/_email_compose.html) what it needs for one record.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from dashboard.gmail_service import GmailAPIError, GmailService

from .models import Customer, CustomerContact, Department, EmailLog

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 20
MAX_SUBJECT = 300
MAX_BODY = 20000
# Text from the templates that must be replaced before the email can go out.
UNFILLED_MARKERS = ("[Write action needed here]", "[Response text")
UNFILLED_MESSAGE = "Replace the text in [square brackets] with the real text before sending."
ADDRESS_SPLIT = re.compile(r"[,;\s]+")


class MailError(Exception):
    """The email could not be sent. `problems` lists what to fix; `gmail` is True when Gmail itself failed."""

    def __init__(self, message: str, problems: Iterable[str] = (), gmail: bool = False):
        super().__init__(message)
        self.problems = list(problems)
        self.gmail = gmail


@dataclass
class Message:
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def clean_addresses(values: Any, label: str) -> Tuple[List[str], List[str]]:
    """Addresses typed or picked (commas, semicolons or spaces between them). Returns (addresses, problems)."""
    addresses: List[str] = []
    problems: List[str] = []
    for raw in _as_list(values):
        for part in ADDRESS_SPLIT.split(raw.strip()):
            if not part:
                continue
            address = part.lower()
            try:
                validate_email(address)
            except ValidationError:
                problems.append(f"{label}: “{part}” is not a valid email address.")
                continue
            if address not in addresses:
                addresses.append(address)
    return addresses, problems


def check(to: Any, cc: Any = (), bcc: Any = (), subject: str = "", body: str = "") -> Tuple[Message, List[str]]:
    """Cleans and checks an email. Returns (message, problems); send only when there are no problems."""
    to_list, problems = clean_addresses(to, "To")
    cc_list, cc_problems = clean_addresses(cc, "CC")
    bcc_list, bcc_problems = clean_addresses(bcc, "BCC")
    problems += cc_problems + bcc_problems
    if not to_list and not problems:
        problems.append("Add at least one To address.")
    # An address is listed once: To wins over CC and BCC, CC wins over BCC.
    cc_list = [a for a in cc_list if a not in to_list]
    bcc_list = [a for a in bcc_list if a not in to_list and a not in cc_list]
    if len(to_list) + len(cc_list) + len(bcc_list) > MAX_RECIPIENTS:
        problems.append(f"Up to {MAX_RECIPIENTS} addresses in all (To, CC and BCC together).")

    subject = " ".join(str(subject or "").split())
    if not subject:
        problems.append("Add a subject.")
    elif len(subject) > MAX_SUBJECT:
        problems.append(f"The subject is longer than {MAX_SUBJECT} characters.")

    body = str(body or "").replace("\r\n", "\n").strip()
    if not body:
        problems.append("Write the email.")
    elif len(body) > MAX_BODY:
        problems.append(f"The email is longer than {MAX_BODY} characters.")
    elif any(marker in body for marker in UNFILLED_MARKERS):
        problems.append(UNFILLED_MESSAGE)
    return Message(to=to_list, cc=cc_list, bcc=bcc_list, subject=subject, body=body), problems


def _send_raw(gmail: GmailService, msg: Message) -> str:
    """Sends through Gmail (BCC included, which the Gmail API keeps hidden from the other recipients)."""
    if gmail._local_demo_enabled():  # local development: nothing leaves the machine
        logger.info("Local demo: not sending %r to %s", msg.subject, ", ".join(msg.to))
        return f"demo-{timezone.now():%Y%m%d%H%M%S%f}"
    if not gmail.service and not gmail.authenticate():
        raise GmailAPIError("Gmail sign-in failed")
    email = EmailMessage()
    email["To"] = ", ".join(msg.to)
    if msg.cc:
        email["Cc"] = ", ".join(msg.cc)
    if msg.bcc:
        email["Bcc"] = ", ".join(msg.bcc)
    email["Subject"] = msg.subject
    email["Message-ID"] = make_msgid()
    email.set_content(msg.body)
    raw = base64.urlsafe_b64encode(email.as_bytes()).decode("utf-8")
    try:
        result = gmail.service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:  # googleapiclient HttpError and network errors
        raise GmailAPIError(f"Gmail could not send the email: {exc}") from exc
    return str((result or {}).get("id") or "")


def send_email(*, user, to: Any, cc: Any = (), bcc: Any = (), subject: str = "", body: str = "",
               customer: Optional[Customer] = None, enquiry_id: str = "") -> EmailLog:
    """Send one new email and log it. Raises MailError when the email is not valid or Gmail refuses it;
    a Gmail refusal is still logged, with status “not sent” and the reason."""
    msg, problems = check(to, cc, bcc, subject, body)
    if problems:
        raise MailError("The email was not sent.", problems)
    try:
        gmail_id = _send_raw(GmailService(user), msg)
    except GmailAPIError as exc:
        EmailLog.objects.create(
            customer=customer, enquiry_id=enquiry_id or "", sent_by=user if user.is_authenticated else None,
            to=", ".join(msg.to), cc=", ".join(msg.cc), bcc=", ".join(msg.bcc), subject=msg.subject, body=msg.body,
            status=EmailLog.STATUS_FAILED, error=str(exc)[:500])
        raise MailError(f"The email was not sent. {exc}", gmail=True) from exc
    return EmailLog.objects.create(
        customer=customer, enquiry_id=enquiry_id or "", sent_by=user if user.is_authenticated else None,
        to=", ".join(msg.to), cc=", ".join(msg.cc), bcc=", ".join(msg.bcc), subject=msg.subject, body=msg.body,
        status=EmailLog.STATUS_SENT, gmail_message_id=gmail_id[:255])


def history_for_customer(customer: Customer, limit: int = 30):
    return list(EmailLog.objects.filter(customer=customer).select_related("sent_by")[:limit])


def history_for_enquiry(enquiry_id: str, limit: int = 30):
    return list(EmailLog.objects.filter(enquiry_id=str(enquiry_id)).select_related("sent_by")[:limit])


# ----- Templates for Marketing -------------------------------------------------------------------------------

def display_name(user) -> str:
    return (user.get_full_name() or user.get_username() or user.email or "").strip()


def department_label(user) -> str:
    """“Sales – AVIGIRI”, from the user's department, or just “AVIGIRI”."""
    from .departments import departments_of

    names = [d.name for d in departments_of(user) if d.is_active]
    return f"{names[0]} – AVIGIRI" if names else "AVIGIRI"


def primary_contact(customer: Optional[Customer]) -> Optional[CustomerContact]:
    if customer is None:
        return None
    contacts = list(customer.contacts.all())  # the primary contact comes first
    return contacts[0] if contacts else None


def _code(customer: Optional[Customer]) -> str:
    if customer is None:
        return "Not in KYC yet"
    return customer.customer_code or customer.code


def _status_text(enquiry: Dict[str, Any]) -> str:
    return str(enquiry.get("status") or "").strip().capitalize() or "—"


def _comment(enquiry: Dict[str, Any]) -> str:
    return " ".join(str(enquiry.get("remarks") or "").split()) or "—"


def internal_template(*, user, enquiry: Dict[str, Any], customer: Optional[Customer]) -> Dict[str, Any]:
    name = customer.name if customer else str(enquiry.get("customer_name") or "").strip()
    code = _code(customer)
    contact = primary_contact(customer)
    if contact:
        contact_text = f"{contact.name} – {contact.designation}" if contact.designation else contact.name
    else:
        contact_text = "—"
    enquiry_id = str(enquiry.get("enquiry_id") or "")
    body = "\n".join([
        "Hi Team,",
        "",
        "Please find the details below regarding the following customer:",
        "",
        f"Customer Name : {name}",
        f"Customer Code : {code}",
        f"Contact Person: {contact_text}",
        f"Enquiry / Ref : {enquiry_id}",
        f"Status        : {_status_text(enquiry)}",
        "",
        "Comment / Requirement:",
        _comment(enquiry),
        "",
        "Action Required:",
        "[Write action needed here]",
        "",
        "Regards,",
        display_name(user),
        department_label(user),
    ])
    return {"subject": f"Internal Update – {name} ({code})", "body": body, "to": [], "cc": []}


def customer_template(*, enquiry: Dict[str, Any], customer: Optional[Customer]) -> Dict[str, Any]:
    enquiry_id = str(enquiry.get("enquiry_id") or "")
    code = _code(customer)
    contact = primary_contact(customer)
    to: List[str] = []
    cc: List[str] = []
    if contact and contact.email_address:
        to.append(contact.email_address)
    elif enquiry.get("customer_email"):
        to.append(str(enquiry["customer_email"]).strip())
    if customer is not None:
        for other in customer.contacts.all():
            for address in other.email_addresses():
                if address not in to and address not in cc:
                    cc.append(address)
    body = "\n".join([
        "Dear Sir/Madam,",
        "",
        f"With reference to your comment: \"{_comment(enquiry)}\"",
        "",
        "[Response text – editable by user]",
        "",
        "Best regards,",
        "AVIGIRI Team",
    ])
    return {"subject": f"Regarding Your Enquiry – {enquiry_id} ({code})", "body": body, "to": to, "cc": cc}


def suggestions(customer: Optional[Customer]) -> List[Dict[str, str]]:
    """Addresses offered in To and CC: internal users and departments, then the customer's contacts."""
    out: List[Dict[str, str]] = []
    seen = set()

    def add(address: str, label: str, group: str) -> None:
        key = address.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append({"email": key, "label": label, "group": group})

    User = get_user_model()
    for user in User.objects.filter(is_active=True).exclude(email="").order_by("first_name", "username")[:300]:
        add(user.email, display_name(user) or user.email, "Internal")
    for department in Department.objects.filter(is_active=True).prefetch_related("contacts"):
        for contact in department.contacts.all():
            add(contact.email, f"{department.name} department", "Internal")
    if customer is not None:
        for contact in customer.contacts.all():
            label = contact.name + (f" – {contact.designation}" if contact.designation else "")
            for address in contact.email_addresses():
                add(address, label, "Customer")
    return out


def compose_config(*, user, enquiry: Dict[str, Any], customer: Optional[Customer], url: str) -> Dict[str, Any]:
    """Everything the shared compose box needs for one enquiry or customer record."""
    return {
        "url": url,
        "customer_id": customer.pk if customer else None,
        "enquiry_id": str(enquiry.get("enquiry_id") or ""),
        "suggestions": suggestions(customer),
        "templates": {
            "internal": internal_template(user=user, enquiry=enquiry, customer=customer),
            "customer": customer_template(enquiry=enquiry, customer=customer),
        },
    }

"""The CRO emails the approved quotation to the customer, in the customer's own email thread.

preview() gives the send box its defaults; send() checks everything again on the server and sends:
  - only an Approved quotation (Sent, for a resend) linked to its enquiry, and only the latest version, approved,
    whose frozen PDF still matches its SHA-256;
  - a share link tied to that version (the enquiry share page); its password never goes in the same email: it
    follows in a separate email in the same thread;
  - To: the enquiry's customer email 1; CC: its other customer emails and the enquiry's CCs (the CRO may edit
    them; typed addresses are saved back to an enquiry that had none);
  - a linked enquiry email gets a reply in its thread (threadId, In-Reply-To, References; "Re: <original subject>",
    not editable, because Gmail only keeps a reply in the thread when the subject matches), otherwise a new email
    whose thread becomes the enquiry's thread; a new email for a revised version (v2+) says "(Revised)";
  - on success the quotation is Sent to customer, the email becomes the thread's latest message (the proforma
    invoice and later emails reply in it) and the follow-ups start again from now; on failure it stays Approved,
    the link is removed and the error is in the tracker.
Sending by hand always works, even when the site's automatic emails are switched off.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from kyc.mailer import ADDRESS_SPLIT, primary_contact

from . import mailer, share_links, threads, workflow
from .models import Quotation, QuotationEvent, QuotationVersion

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 20
QUOTE_ORIGINAL_CHARS = 3000
# Where the share link goes in the message. Replaced on send; the CRO never types the link.
LINK_PLACEHOLDER = "[Link to the quotation – added when the email is sent]"
PASSWORD_NOTE = "The password to open the link is sent to you in a separate email."


class SendError(Exception):
    """The email was not sent; the message says what to fix. `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Draft:
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    subject: str = ""
    subject_locked: bool = False  # a reply in the linked thread keeps the thread's subject
    linked: bool = False
    body: str = ""
    attachment_name: str = ""
    version: Optional[QuotationVersion] = None


@dataclass
class SendResult:
    """What send() did: the quotation email, and whether the separate password email went too."""

    mail: mailer.SentMail
    share_url: str = ""
    password_error: str = ""

    @property
    def references(self) -> str:
        return self.mail.references

    @property
    def local_demo(self) -> bool:
        return self.mail.local_demo

    @property
    def message_id(self) -> str:
        return self.mail.message_id

    @property
    def thread_id(self) -> str:
        return self.mail.thread_id


def parse_addresses(value: Any, label: str) -> List[str]:
    """Addresses typed with commas, semicolons or spaces between them. Raises SendError on a bad one."""
    values = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for raw in values:
        for part in ADDRESS_SPLIT.split(str(raw or "").strip()):
            if not part:
                continue
            address = part.lower()
            try:
                validate_email(address)
            except ValidationError:
                raise SendError(f"{label}: “{part}” is not a valid email address.")
            if address not in out:
                out.append(address)
    return out


def _contact_name(quotation: Quotation) -> str:
    contact = primary_contact(quotation.customer)
    return (contact.name if contact and contact.name else "").strip() or "Sir/Madam"


def attachment_name(quotation: Quotation) -> str:
    return f"{quotation.number}.pdf"


def default_subject(quotation: Quotation) -> str:
    return f"Quotation {quotation.number} – AVIGIRI"


def _enquiry_subject(quotation: Quotation) -> str:
    """The enquiry's own subject (from the enquiry service), '' when unknown."""
    if not quotation.enquiry_id:
        return ""
    try:
        from .enquiry_link import EnquiryNotFound, EnquiryUnavailable, fetch

        return str(fetch(quotation.enquiry_id).get("subject") or "").strip()
    except (EnquiryNotFound, EnquiryUnavailable):
        return ""
    except Exception as exc:  # the default subject is a nicety
        logger.info("Quotations: no enquiry subject for %s: %s", quotation.number, exc)
        return ""


def new_thread_subject(quotation: Quotation, version: Optional[QuotationVersion], enquiry_subject: str = "") -> str:
    """A new email (no thread known): "Re: <enquiry subject>", "(Revised)" from version 2 on."""
    subject = mailer.reply_subject(enquiry_subject) if enquiry_subject else default_subject(quotation)
    if version is not None and version.number > 1 and "(revised)" not in subject.lower():
        subject = f"{subject} (Revised)"
    return subject


def valid_until(today=None):
    days = int(getattr(settings, "QUOTATION_VALIDITY_DAYS", 30) or 30)
    return (today or timezone.localdate()) + timedelta(days=days)


def default_body(quotation: Quotation, version: Optional[QuotationVersion] = None) -> str:
    number = version.number if version is not None else 1
    revised = number > 1
    opening = ("Further to our earlier quotation, please find attached our revised quotation "
               if revised else "Thank you for your enquiry. Please find attached our quotation ")
    return "\n".join([
        f"Dear {_contact_name(quotation)},",
        "",
        f"{opening}{quotation.number} (version {number}) for {quotation.title}"
        + (f", reference {quotation.enquiry_id}" if quotation.enquiry_id else "") + ".",
        "",
        "You can also view it online, and accept or reject it, using this link:",
        LINK_PLACEHOLDER,
        PASSWORD_NOTE,
        "",
        f"This quotation is valid until {valid_until():%d %B %Y}.",
        "",
        "Kindly review it and confirm your acceptance by replying to this email, with your purchase order or "
        "reference number. Please let us know if you have any questions.",
        "",
        "Regards,",
        "AVIGIRI team",
    ])


def default_recipients(quotation: Quotation) -> tuple:
    """(To, CC): the enquiry's customer email 1; its other customer emails and the enquiry's CCs, never our own
    mailbox."""
    from enquiries import payment_terms

    to = threads.contact_emails(quotation.enquiry_id)[:1]
    own = mailer.mailbox_address()
    cc: List[str] = []
    for address in str(payment_terms.default_cc(quotation.enquiry_id) or "").replace(";", ",").split(","):
        address = address.strip().lower()
        if address and address != own and address not in to and address not in cc:
            cc.append(address)
    return to, cc


def preview(quotation: Quotation) -> Draft:
    version = quotation.active_version
    ref = threads.thread_for(quotation.enquiry_id)
    if ref is not None:
        subject, locked = mailer.reply_subject(ref.subject), True
    else:
        subject, locked = new_thread_subject(quotation, version, _enquiry_subject(quotation)), False
    to, cc = default_recipients(quotation)
    return Draft(to=to, cc=cc, subject=subject, subject_locked=locked, linked=ref is not None,
                 body=default_body(quotation, version), attachment_name=attachment_name(quotation), version=version)


def _quoted(message: Optional[Dict[str, Any]]) -> str:
    """The customer's message under ours, as email programs quote it."""
    text = str((message or {}).get("body_text") or "").strip()
    if not text:
        return ""
    text = text[:QUOTE_ORIGINAL_CHARS]
    who = str(message.get("sender") or "the customer")
    when = str(message.get("date") or "").strip()
    head = f"On {when}, {who} wrote:" if when else f"{who} wrote:"
    return "\n\n" + head + "\n" + "\n".join(f"> {line}" for line in text.splitlines())


def with_link(body: str, url: str) -> str:
    """The message with the share link where the placeholder is (or before the sign-off when it was removed).
    A password typed into the message is the CRO's choice; the link's own password is never added here."""
    if LINK_PLACEHOLDER in body:
        return body.replace(LINK_PLACEHOLDER, url)
    block = f"View the quotation online: {url}\n{PASSWORD_NOTE}"
    marker = "\nRegards,"
    if marker in body:
        head, tail = body.rsplit(marker, 1)
        return f"{head.rstrip()}\n\n{block}\n{marker}{tail}"
    return f"{body}\n\n{block}"


def _html(text: str) -> Optional[str]:
    try:
        from dashboard.follow_up_templates import to_html

        return to_html(text)
    except Exception:
        return None


def _start_follow_ups(quotation: Quotation, sent: mailer.SentMail, *, to: List[str], cc: List[str], subject: str,
                      user, share_url: str = "", share_password: str = "") -> None:
    """Start the automatic follow-ups again from this email, as the Enquiry Board's "Send to customer" does (a
    revised version restarts them). Never fails the send."""
    if not quotation.enquiry_id or sent.local_demo or not sent.thread_id:
        return
    try:
        from dashboard.follow_up_templates import QUOTATION_STAGE
        from enquiries import auto_follow_up

        auto_follow_up.record_start(quotation.enquiry_id, QUOTATION_STAGE,
                                    sent={"threadId": sent.thread_id, "id": sent.provider_id},
                                    customer_email=to[0], cc=to[1:] + cc, subject=subject, user=user,
                                    document_no=quotation.number, share_url=share_url,
                                    share_password=share_password, restart=True)
    except Exception as exc:
        logger.warning("Quotations: follow-ups not started for %s: %s", quotation.number, exc)


def _password_body(quotation: Quotation, version: QuotationVersion, password: str) -> str:
    return "\n".join([
        f"Dear {_contact_name(quotation)},",
        "",
        f"The password to open the online link to our quotation {quotation.number} (version {version.number}), "
        "sent in our previous email, is:",
        "",
        password,
        "",
        "Regards,",
        "AVIGIRI team",
    ])


def _send_password(quotation: Quotation, version: QuotationVersion, password: str, *, to: List[str],
                   cc: List[str], user, after: Optional[mailer.SentMail] = None, subject: str = "") -> str:
    """The link's password, alone, as a reply in the thread (to the quotation email when `after` is given).
    Returns '' when it went out, else the error. Never raises."""
    if after is not None:
        ref = mailer.ThreadRef(message_id=after.message_id, references=after.references, subject=subject,
                               gmail_thread_id=after.thread_id)
    else:
        ref = threads.thread_for(quotation.enquiry_id)
    body = _password_body(quotation, version, password)
    try:
        sent = mailer.send_email(to, cc, (), subject or default_subject(quotation), body, (),
                                 reply_to_thread=ref, html_body=_html(body))
    except mailer.MailerError as exc:
        logger.error("Quotations: password email for %s failed: %s", quotation.number, exc)
        QuotationEvent.objects.create(quotation=quotation, version=version, user=user,
                                      action=QuotationEvent.ACTION_PASSWORD_FAILED, comment=str(exc)[:1000],
                                      details={"to": to, "cc": cc, "error": str(exc)[:1000]})
        return str(exc)
    QuotationEvent.objects.create(
        quotation=quotation, version=version, user=user, action=QuotationEvent.ACTION_PASSWORD_SENT,
        comment=f"To {', '.join(to)}" + (f", CC {', '.join(cc)}" if cc else ""),
        details={"to": to, "cc": cc, "message_id": sent.message_id, "thread_id": sent.thread_id,
                 "test_mode": sent.local_demo})
    if not sent.local_demo:
        try:
            threads.remember_sent(quotation.enquiry_id, sent, subject=subject)
        except Exception:
            logger.exception("Quotations: password for %s sent but the thread was not updated", quotation.number)
    return ""


def password_failed(quotation: Quotation) -> bool:
    """True when the last email about the link was a failed password email (offer to send it again)."""
    last = (quotation.events.filter(action__in=(QuotationEvent.ACTION_SENT, QuotationEvent.ACTION_RESENT,
                                                QuotationEvent.ACTION_PASSWORD_SENT,
                                                QuotationEvent.ACTION_PASSWORD_FAILED))
            .order_by("-created_at", "-id").first())
    return last is not None and last.action == QuotationEvent.ACTION_PASSWORD_FAILED


def _base_url(base_url: str, quotation: Quotation) -> str:
    if base_url:
        return base_url.rstrip("/")
    from enquiries.follow_up_links import _base_url as known_base

    found = known_base(quotation.enquiry_id)
    if not found:
        raise SendError("The site address for the share link is not known (set ENQUIRY_SHARE_BASE_URL).")
    return found


def send(quotation: Quotation, *, user, to: Any, cc: Any = (), subject: str = "", body: str = "",
         resend: bool = False, base_url: str = "") -> SendResult:
    """Send (or resend) the approved quotation. Raises SendError; the quotation's status is unchanged then."""
    quotation.refresh_from_db()
    wanted = Quotation.STATUS_SENT if resend else Quotation.STATUS_APPROVED
    if quotation.status != wanted:
        what = "Only a quotation already sent can be resent." if resend else \
            "Only an approved quotation can be sent to the customer."
        raise SendError(f"{what} This one is “{quotation.get_status_display()}”.")
    if not quotation.enquiry_id:
        raise SendError("Link this quotation to its enquiry first, so it goes in the enquiry's email thread.")
    to_list = parse_addresses(to, "To")
    cc_list = [a for a in parse_addresses(cc, "CC") if a not in to_list]
    if not to_list:
        raise SendError("Add the customer's email address in To. It will be saved on the enquiry.")
    if len(to_list) + len(cc_list) > MAX_RECIPIENTS:
        raise SendError(f"Up to {MAX_RECIPIENTS} addresses in all (To and CC together).")
    body = str(body or "").replace("\r\n", "\n").strip()
    if not body:
        raise SendError("Write the email.")

    try:
        version, pdf = workflow.approved_pdf(quotation)  # the latest version, approved, unchanged
    except workflow.WorkflowError as exc:
        raise SendError(str(exc)) from exc

    ref = threads.thread_for(quotation.enquiry_id)
    original = None
    if ref is not None:
        ref, original = threads.latest_in_thread(ref)
        subject = mailer.reply_subject(ref.subject)  # never what was typed: it would break the thread
    else:
        subject = " ".join(str(subject or "").split()) or new_thread_subject(quotation, version,
                                                                             _enquiry_subject(quotation))

    record, url, password = share_links.create_for_version(quotation, version, pdf,
                                                          base_url=_base_url(base_url, quotation))
    full_body = with_link(body, url) + _quoted(original)
    try:
        sent = mailer.send_email(to_list, cc_list, (), subject, full_body,
                                 [mailer.Attachment(attachment_name(quotation), pdf, "application/pdf")],
                                 reply_to_thread=ref, html_body=_html(full_body))
    except mailer.MailerError as exc:
        record.delete()  # the customer never got this link
        logger.error("Quotations: sending %s to %s failed: %s", quotation.number, to_list, exc)
        workflow.record_send_failed(quotation, user=user, version=version, error=str(exc), to=to_list, cc=cc_list)
        raise SendError(f"The email was not sent: {exc}", status=502) from exc

    # The email is out: record it first, so a problem below can never leave it looking unsent.
    workflow.record_send(quotation, user=user, version=version, to=to_list, cc=cc_list, message_id=sent.message_id,
                         thread_id=sent.thread_id, resend=resend,
                         details={"subject": subject, "in_reply_to": ref.message_id if ref else "",
                                  "new_thread": ref is None, "sha256": version.sha256, "version": version.number,
                                  "share_link": record.id, "gmail_id": sent.provider_id,
                                  "test_mode": sent.local_demo})
    try:
        threads.save_contact_emails(quotation.enquiry_id, to_list, user=user)
        if not sent.local_demo:
            threads.remember_sent(quotation.enquiry_id, sent, subject=subject)
    except Exception:
        logger.exception("Quotations: %s was sent but the enquiry's thread was not updated", quotation.number)
    thread_subject = ref.subject if ref is not None else subject
    password_error = _send_password(quotation, version, password, to=to_list, cc=cc_list, user=user, after=sent,
                                    subject=thread_subject)
    _start_follow_ups(quotation, sent, to=to_list, cc=cc_list, subject=thread_subject, user=user, share_url=url,
                      share_password=password)
    return SendResult(mail=sent, share_url=url, password_error=password_error)


def resend_password(quotation: Quotation, *, user) -> None:
    """The password email failed: the link gets a new password, emailed alone in the enquiry's thread to the
    addresses the quotation went to. Raises SendError."""
    quotation.refresh_from_db()
    if quotation.status != Quotation.STATUS_SENT:
        raise SendError("The quotation has not been sent, so there is no password to send.")
    version = quotation.active_version
    record = share_links.latest_approved_link(quotation)
    if version is None or record is None:
        raise SendError("There is no link of the approved version to send a password for. Send the quotation again.")
    to = parse_addresses(quotation.sent_to, "To")
    cc = parse_addresses(quotation.sent_cc, "CC")
    if not to:
        raise SendError("The quotation's recipients are not known. Send the quotation again.")
    password = share_links.new_password(record)
    ref = threads.thread_for(quotation.enquiry_id)
    error = _send_password(quotation, version, password, to=to, cc=cc, user=user,
                           subject=ref.subject if ref is not None else default_subject(quotation))
    if error:
        raise SendError(f"The password email was not sent: {error}", status=502)
    # The follow-ups repeat the link with its password: keep them in step with the new one.
    from enquiries.models import FollowUpStart

    FollowUpStart.objects.filter(enquiry_id=quotation.enquiry_id, share_url__endswith=f"/{record.id}/").update(
        share_password=password[:64])

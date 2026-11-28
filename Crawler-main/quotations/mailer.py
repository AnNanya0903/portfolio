"""Email from the shared customer mailbox (customerservice@avigiri.com): quotations now, the proforma invoice later.

    send_email(to, cc, bcc, subject, body, attachments, reply_to_thread=None) -> SentMail

`reply_to_thread` (a ThreadRef) makes the email a reply in the customer's own thread: In-Reply-To is the latest
Message-ID of the thread, References is the thread's References plus that Message-ID, the subject is
"Re: <original subject>" (never "Re: Re:"), and with the Gmail API the threadId is set too. Without it a new
email (new thread) is sent.

The provider comes from settings.QUOTATION_MAIL_PROVIDER:
  GMAIL_API  sends through the Gmail API with the portal Google sign-in of the mailbox user
             (settings.QUOTATION_MAILBOX_ADDRESS). Nobody needs the mailbox password; the token stays on the
             server. Gmail keeps the email in the mailbox's Sent folder.
  SMTP       not implemented yet. It must BCC the mailbox so it keeps a copy.
  MS_GRAPH   not implemented yet (createReply on graph_message_id, attach, send).
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Iterable, List, Optional, Sequence

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from enquiries.thread_check import normalize_subject

logger = logging.getLogger(__name__)

GMAIL_API, SMTP, MS_GRAPH = "GMAIL_API", "SMTP", "MS_GRAPH"
PROVIDERS = (GMAIL_API, SMTP, MS_GRAPH)


class MailerError(Exception):
    """The email was not sent. The message is safe to show to staff."""


class MailerNotConfigured(MailerError):
    """The mailbox or provider is not set up on this server."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/pdf"


@dataclass
class ThreadRef:
    """Where a reply goes: the latest message of the customer's thread."""

    message_id: str = ""  # RFC Message-ID of the latest message in the thread
    references: str = ""  # that message's References header
    subject: str = ""  # the thread's subject (any "Re:" is removed)
    gmail_thread_id: str = ""
    graph_message_id: str = ""
    graph_conversation_id: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.message_id or self.gmail_thread_id or self.graph_message_id)


@dataclass
class SentMail:
    message_id: str  # RFC Message-ID of the email just sent
    provider_id: str = ""  # Gmail message id / Graph message id
    thread_id: str = ""  # Gmail threadId / Graph conversationId
    references: str = ""  # the References header it carried
    local_demo: bool = False  # local development: nothing left the machine


def provider() -> str:
    return str(getattr(settings, "QUOTATION_MAIL_PROVIDER", GMAIL_API) or GMAIL_API).strip().upper()


def mailbox_address() -> str:
    return str(getattr(settings, "QUOTATION_MAILBOX_ADDRESS", "") or "").strip().lower()


def sender_header() -> str:
    name = str(getattr(settings, "QUOTATION_MAILBOX_NAME", "") or "AVIGIRI Customer Service")
    return formataddr((name, mailbox_address()))


def reply_subject(subject: str) -> str:
    """'Re: <subject>' with any number of leading Re:/Fwd: removed first, so it never reads 'Re: Re:'."""
    clean = normalize_subject(subject)
    return f"Re: {clean}" if clean else "Re:"


def chain_references(ref: Optional[ThreadRef]) -> str:
    """The References header of a reply: the thread's References plus the message replied to, each once."""
    if ref is None:
        return ""
    ids: List[str] = []
    for token in f"{ref.references or ''} {ref.message_id or ''}".split():
        if token not in ids:
            ids.append(token)
    return " ".join(ids)


def _clean(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for value in values or ():
        address = str(value or "").strip()
        if address and address.lower() not in {a.lower() for a in out}:
            out.append(address)
    return out


def build_message(*, to: Sequence[str], cc: Sequence[str] = (), bcc: Sequence[str] = (), subject: str, body: str,
                  attachments: Sequence[Attachment] = (), reply_to_thread: Optional[ThreadRef] = None,
                  html_body: Optional[str] = None, include_bcc_header: bool = False) -> EmailMessage:
    """The MIME email, with the reply headers when `reply_to_thread` is set. Pure: sends nothing."""
    message = EmailMessage()
    message["From"] = sender_header()
    message["To"] = ", ".join(_clean(to))
    if _clean(cc):
        message["Cc"] = ", ".join(_clean(cc))
    if include_bcc_header and _clean(bcc):
        message["Bcc"] = ", ".join(_clean(bcc))
    ref = reply_to_thread if reply_to_thread is not None and reply_to_thread.is_set else None
    if ref is not None:
        message["Subject"] = reply_subject(ref.subject or subject)
        if ref.message_id:
            message["In-Reply-To"] = ref.message_id
        references = chain_references(ref)
        if references:
            message["References"] = references
    else:
        message["Subject"] = " ".join(str(subject or "").split())
    domain = mailbox_address().rsplit("@", 1)[-1] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for item in attachments or ():
        maintype, _, subtype = (item.mime_type or "application/octet-stream").partition("/")
        message.add_attachment(item.content, maintype=maintype, subtype=subtype or "octet-stream",
                               filename=item.filename)
    return message


def send_email(to: Sequence[str], cc: Sequence[str] = (), bcc: Sequence[str] = (), subject: str = "",
               body: str = "", attachments: Sequence[Attachment] = (), reply_to_thread: Optional[ThreadRef] = None,
               *, html_body: Optional[str] = None) -> SentMail:
    """Send one email from the shared mailbox. Raises MailerError when it was not sent."""
    if not _clean(to):
        raise MailerError("Add at least one To address.")
    kind = provider()
    if kind == GMAIL_API:
        return _send_gmail(to=to, cc=cc, bcc=bcc, subject=subject, body=body, attachments=attachments,
                           reply_to_thread=reply_to_thread, html_body=html_body)
    if kind in (SMTP, MS_GRAPH):
        raise MailerNotConfigured(f"Sending with {kind} is not set up on this server yet (QUOTATION_MAIL_PROVIDER).")
    raise MailerNotConfigured(f"Unknown QUOTATION_MAIL_PROVIDER {kind!r}; use one of {', '.join(PROVIDERS)}.")


# ----- Gmail API -------------------------------------------------------------------------------------------------

def mailbox_user():
    """The portal user signed in with Google as the shared mailbox, whose token sends the email."""
    address = mailbox_address()
    if not address:
        raise MailerNotConfigured("QUOTATION_MAILBOX_ADDRESS is not set.")
    User = get_user_model()
    user = User.objects.filter(email__iexact=address, is_active=True).order_by("id").first()
    if user is None:
        try:
            from allauth.socialaccount.models import SocialAccount

            account = (SocialAccount.objects.filter(provider="google", extra_data__email__iexact=address)
                       .select_related("user").first())
            user = account.user if account and account.user.is_active else None
        except Exception:  # allauth missing or the JSON lookup is not supported by the database
            user = None
    if user is None:
        raise MailerNotConfigured(f"{address} has not signed in to the portal with Google yet, so the portal "
                                  "cannot send from it. Sign in once as that mailbox.")
    return user


def gmail_service():
    from dashboard.gmail_service import GmailService

    return GmailService(mailbox_user())


def _sent_message_id(gmail, gmail_id: str, fallback: str) -> str:
    """Gmail may replace our Message-ID; read back the one the customer actually receives."""
    try:
        meta = gmail.service.users().messages().get(userId="me", id=gmail_id, format="metadata",
                                                    metadataHeaders=["Message-ID"]).execute()
        for header in (meta or {}).get("payload", {}).get("headers", []) or []:
            if str(header.get("name") or "").lower() == "message-id" and header.get("value"):
                return str(header["value"]).strip()
    except Exception as exc:  # the email went out; keep our own id
        logger.warning("Quotation mailer: could not read back the Message-ID of %s: %s", gmail_id, exc)
    return fallback


def _send_gmail(*, to, cc, bcc, subject, body, attachments, reply_to_thread, html_body) -> SentMail:
    from dashboard.gmail_service import GmailAPIError

    gmail = gmail_service()
    message = build_message(to=to, cc=cc, bcc=bcc, subject=subject, body=body, attachments=attachments,
                            reply_to_thread=reply_to_thread, html_body=html_body, include_bcc_header=True)
    ref = reply_to_thread if reply_to_thread is not None and reply_to_thread.is_set else None
    references = str(message.get("References") or "")
    if gmail._local_demo_enabled():  # local development: nothing leaves the machine
        logger.info("Local demo: not sending %r to %s", str(message["Subject"]), message["To"])
        return SentMail(message_id=str(message["Message-ID"]), provider_id=f"demo-{timezone.now():%Y%m%d%H%M%S%f}",
                        thread_id=(ref.gmail_thread_id if ref else "") or "", references=references, local_demo=True)
    if not gmail.service and not gmail.authenticate():
        raise MailerError(f"The portal could not sign in to {mailbox_address()}. Sign in again as that mailbox.")
    payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if ref is not None and ref.gmail_thread_id:
        payload["threadId"] = ref.gmail_thread_id
    try:
        result = gmail.service.users().messages().send(userId="me", body=payload).execute() or {}
    except GmailAPIError as exc:
        raise MailerError(f"Gmail could not send the email: {exc}") from exc
    except Exception as exc:  # googleapiclient HttpError, network errors
        raise MailerError(f"Gmail could not send the email: {exc}") from exc
    gmail_id = str(result.get("id") or "")
    return SentMail(message_id=_sent_message_id(gmail, gmail_id, str(message["Message-ID"])) if gmail_id
                    else str(message["Message-ID"]), provider_id=gmail_id,
                    thread_id=str(result.get("threadId") or ""), references=references)

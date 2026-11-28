"""The customer's email thread of an enquiry, kept on EnquiryContact, and the shared mailbox it comes from.

- recent_emails: the shared mailbox's recent inbox emails, for "Link enquiry email" on the Enquiry Board.
- link_email: save the picked email's thread (Message-ID, References, subject, Gmail threadId) on the enquiry.
- thread_for / latest_in_thread: where the next email replies (refreshed from Gmail when possible, so a reply
  from the customer after the link is replied to, not skipped).
- remember_sent: after every email the portal sends in the thread, it becomes the thread's latest message, so
  the proforma invoice and every later email chain onto it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

from enquiries import customer_emails
from enquiries.models import EnquiryContact
from enquiries.thread_check import normalize_subject

from . import mailer
from .models import QuotationEvent

logger = logging.getLogger(__name__)

RECENT_LIMIT = 25


class ThreadError(Exception):
    """Something staff can fix (shown on the page)."""


def contact_for(enquiry_id: str) -> Optional[EnquiryContact]:
    if not enquiry_id:
        return None
    return EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()


def contact_emails(enquiry_id: str) -> List[str]:
    """The enquiry's customer addresses: the main one first, then the others."""
    contact = contact_for(enquiry_id)
    if contact is None:
        return []
    out = [contact.customer_email.strip().lower()] if contact.customer_email else []
    return out + [a for a in customer_emails.split(contact.other_customer_emails) if a not in out]


def save_contact_emails(enquiry_id: str, addresses: List[str], user=None) -> None:
    """The CRO typed the customer's address because the enquiry had none: keep it on the enquiry."""
    if not enquiry_id or not addresses:
        return
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    if contact.customer_email:
        return
    contact.customer_email = addresses[0]
    contact.updated_by = user if getattr(user, "is_authenticated", False) else None
    contact.save(update_fields=["customer_email", "updated_by", "updated_at"])
    customer_emails.save_others(enquiry_id, addresses[1:customer_emails.MAX_OTHER + 1], user=user)


def thread_for(enquiry_id: str) -> Optional[mailer.ThreadRef]:
    contact = contact_for(enquiry_id)
    if contact is None or not contact.has_email_thread:
        return None
    return mailer.ThreadRef(message_id=contact.email_message_id, references=contact.email_references,
                            subject=contact.email_subject, gmail_thread_id=contact.gmail_thread_id,
                            graph_message_id=contact.graph_message_id,
                            graph_conversation_id=contact.graph_conversation_id)


def _ref_from_message(message: Dict[str, Any], *, subject: str = "") -> mailer.ThreadRef:
    return mailer.ThreadRef(message_id=str(message.get("message_id_header") or "").strip(),
                            references=" ".join(str(message.get("references") or "").split()),
                            subject=normalize_subject(subject or message.get("subject") or ""),
                            gmail_thread_id=str(message.get("thread_id") or ""))


def latest_in_thread(ref: mailer.ThreadRef) -> Tuple[mailer.ThreadRef, Optional[Dict[str, Any]]]:
    """(the thread's latest message as a ThreadRef, that message) from Gmail; the stored ref when Gmail cannot
    be read. The original subject is kept so the reply never changes it."""
    if mailer.provider() != mailer.GMAIL_API or not ref.gmail_thread_id:
        return ref, None
    try:
        messages = mailer.gmail_service().get_thread_messages(ref.gmail_thread_id, limit=50) or []
    except Exception as exc:  # Gmail down or token expired: the stored reference still threads correctly
        logger.warning("Quotations: could not read Gmail thread %s: %s", ref.gmail_thread_id, exc)
        return ref, None
    for message in reversed(messages):
        if message.get("message_id_header"):
            latest = _ref_from_message(message, subject=ref.subject)
            latest.gmail_thread_id = ref.gmail_thread_id
            return latest, message
    return ref, None


def recent_emails(query: str = "", limit: int = RECENT_LIMIT) -> List[Dict[str, Any]]:
    """Recent inbox emails of the shared mailbox: id, thread, sender, subject, date, snippet."""
    if mailer.provider() != mailer.GMAIL_API:
        raise ThreadError(f"Listing the mailbox is not available with {mailer.provider()} yet.")
    words = " ".join(str(query or "").split())[:200]
    try:
        rows = mailer.gmail_service().search_messages(f"in:inbox {words}".strip(), limit=limit) or []
    except mailer.MailerError as exc:
        raise ThreadError(str(exc)) from exc
    except Exception as exc:
        raise ThreadError(f"The mailbox could not be read: {exc}") from exc
    return [{"id": row.get("id"), "thread_id": row.get("thread_id"), "sender": row.get("sender") or "",
             "subject": row.get("subject") or "(no subject)", "date": row.get("parsed_date") or row.get("date"),
             "snippet": row.get("snippet") or ""} for row in rows if row.get("id")]


def link_email(enquiry_id: str, gmail_message_id: str, *, user) -> EnquiryContact:
    """Save the picked email's thread on the enquiry, and the sender as its customer address if it has none."""
    if not enquiry_id or not gmail_message_id:
        raise ThreadError("Pick the customer's email.")
    try:
        message = mailer.gmail_service().get_message(gmail_message_id)
    except mailer.MailerError as exc:
        raise ThreadError(str(exc)) from exc
    except Exception as exc:
        raise ThreadError(f"That email could not be read from the mailbox: {exc}") from exc
    ref = _ref_from_message(message or {})
    if not ref.message_id:
        raise ThreadError("That email has no Message-ID, so replies could not be threaded. Pick another one.")
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    contact.email_message_id, contact.email_references = ref.message_id, ref.references
    contact.email_subject, contact.gmail_thread_id = ref.subject[:500], ref.gmail_thread_id
    contact.email_linked_at = timezone.now()
    contact.email_linked_by = user if getattr(user, "is_authenticated", False) else None
    from email.utils import parseaddr

    sender = parseaddr(str(message.get("reply_to") or message.get("sender") or ""))[1].strip().lower()
    if sender and not contact.customer_email and not sender.endswith("@" + mailer.mailbox_address().rsplit("@", 1)[-1]):
        contact.customer_email = sender
    contact.updated_by = contact.email_linked_by
    contact.save()
    QuotationEvent.objects.create(
        enquiry_id=str(enquiry_id), action=QuotationEvent.ACTION_EMAIL_LINKED, user=contact.email_linked_by,
        comment=f"“{ref.subject}” from {sender or 'the customer'}",
        details={"message_id": ref.message_id, "thread_id": ref.gmail_thread_id, "subject": ref.subject})
    return contact


def unlink_email(enquiry_id: str) -> None:
    EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).update(
        email_message_id="", email_references="", email_subject="", gmail_thread_id="", graph_message_id="",
        graph_conversation_id="", email_linked_at=None, email_linked_by=None)


def remember_sent(enquiry_id: str, sent: mailer.SentMail, *, subject: str) -> None:
    """The email just sent is now the thread's latest message: the next one replies to it."""
    if not enquiry_id or not sent.message_id:
        return
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    contact.email_message_id = sent.message_id[:255]
    contact.email_references = sent.references
    if sent.thread_id:
        contact.gmail_thread_id = sent.thread_id[:255]
    if not contact.email_subject:
        contact.email_subject = normalize_subject(subject)[:500]
    contact.save(update_fields=["email_message_id", "email_references", "gmail_thread_id", "email_subject",
                                "updated_at"])

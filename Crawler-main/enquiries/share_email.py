"""Email the quotation share link to the customer: link + password in the body, quotation PDF and
the documents the employee ticks attached. Sent from the signed-in employee's Gmail."""
from __future__ import annotations

import base64
import io
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from django.contrib.auth.hashers import check_password
from django.core import signing
from django.urls import reverse

from dashboard.follow_up_templates import PAYMENT_STAGE, QUOTATION_STAGE

from . import payment_terms, services
from .models import EnquiryContact, FollowUpSent, FollowUpStart, ShareLink

# What is being sent: label, the follow-up stage it starts, and the subject word.
DOCUMENTS = {
    "quotation": {"label": "Quotation", "stage": QUOTATION_STAGE, "subject": "Quotation"},
    "proforma": {"label": "Proforma invoice", "stage": PAYMENT_STAGE, "subject": "Proforma Invoice"},
}

# Gmail refuses messages over 25 MB; base64 makes attachments about a third bigger.
MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024


logger = logging.getLogger(__name__)


class ShareEmailError(ValueError):
    """Something the employee can fix (shown on the page)."""


_VOUCHER_LABEL = re.compile(r"voucher\s*(?:no|number|#)\.?", re.I)
_VOUCHER_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9/\\_.\-]*")
_DATE_TOKEN = re.compile(r"^\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}$")


def _voucher_token(text: str) -> str:
    """The first word in `text` that can be a voucher number: has a digit and is not a date."""
    for token in _VOUCHER_TOKEN.findall(text or ""):
        token = token.rstrip(".-_/\\")
        if any(ch.isdigit() for ch in token) and not _DATE_TOKEN.match(token) and len(token) <= 40:
            return token
    return ""


def voucher_number_from_text(text: str) -> str:
    """The Voucher No. printed on a quotation, or '' when it cannot be found.

    Handles "Voucher No.: AG/25-26/014" on one line, and the table layout (as printed by Tally) where
    the number is on a line below the "Voucher No." heading, often next to the date.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        label = _VOUCHER_LABEL.search(line)
        if not label:
            continue
        found = _voucher_token(line[label.end():])
        if found:
            return found
        for below in [l for l in lines[index + 1:] if l][:3]:
            found = _voucher_token(below)
            if found:
                return found
    return ""


def voucher_number(pdf_bytes: bytes) -> str:
    """Read the Voucher No. from the quotation PDF's text (first two pages). '' for scanned PDFs or errors."""
    if not pdf_bytes:
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
    except Exception as exc:  # a broken or protected PDF must never stop the email
        logger.info("Could not read the quotation PDF for its Voucher No.: %s", exc)
        return ""
    return voucher_number_from_text(text)


def load_share(share_id: str) -> Tuple[ShareLink, Dict[str, Any]]:
    """The share link and its signed data (fields, quotation PDF, password hash)."""
    record = ShareLink.objects.filter(id=str(share_id or "")).first()
    if record is None:
        raise ShareEmailError("This share link no longer exists. Please create it again.")
    try:
        data = signing.loads(record.token, salt="enquiry-share")
    except signing.BadSignature as exc:
        raise ShareEmailError("This share link is no longer valid. Please create it again.") from exc
    return record, data


def share_url(request, record: ShareLink) -> str:
    return request.build_absolute_uri(
        reverse("enquiries:share_pretty", args=[record.brand, record.enq_no, record.id]))


def password_matches(data: Mapping[str, Any], password: str) -> bool:
    password_hash = data.get("password_hash")
    return bool(password and password_hash and check_password(password, password_hash))


def quotation_attachment(data: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "filename": data.get("pdf_filename") or "quotation.pdf",
        "mime_type": data.get("pdf_content_type") or "application/pdf",
        "content": base64.b64decode(data.get("pdf_content_b64") or b""),
    }


def enquiry_documents(enquiry: Mapping[str, Any], *, skip_name: str = "") -> List[Dict[str, Any]]:
    """The enquiry's uploaded files the employee can choose to attach (the quotation PDF itself left out)."""
    documents, seen = [], set()
    skip = (skip_name or "").strip().lower()
    for raw in (enquiry.get("general_attachments") or enquiry.get("attachments") or []):
        try:
            item = dict(raw)
        except (TypeError, ValueError):
            continue
        attachment_id = str(item.get("attachment_id") or "").strip()
        name = str(item.get("file_name") or item.get("filename") or "").strip()
        if not attachment_id or attachment_id in seen or (skip and name.lower() == skip):
            continue
        seen.add(attachment_id)
        documents.append({
            "id": attachment_id,
            "name": name or f"Document {attachment_id}",
            "size": item.get("size") or item.get("file_size"),
            "download_url": item.get("download_url") or "",
        })
    return documents


def customer_email(enquiry_id: str, enquiry: Mapping[str, Any]) -> str:
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("customer_email").first()
    return ((contact.customer_email if contact else "") or str(enquiry.get("customer_email") or "")).strip()


def default_message(enquiry_id: str, enquiry: Mapping[str, Any], url: str, password: str,
                    voucher_no: str = "", document: str = "quotation") -> Tuple[str, str]:
    """Subject and body for the quotation (or proforma invoice) email.

    With a Voucher No. (read from the PDF) the subject is "Quotation <Voucher No.> - <title>";
    without one it uses the enquiry reference.
    """
    title = str(enquiry.get("subject") or enquiry.get("project_name") or "").strip()
    reference = str(enquiry.get("enq_number") or enquiry_id or "").strip()
    voucher_no = (voucher_no or "").strip()
    about = f' for "{title}"' if title else ""
    if document == "proforma":
        subject = f"Proforma Invoice {voucher_no or reference}" + (f" - {title}" if title else "")
        numbers = (f"proforma invoice no. {voucher_no}, reference {reference}" if voucher_no
                   else f"reference {reference}")
        body = (
            "Dear Sir/Madam,\n\n"
            f"Thank you for your order. Please find attached our proforma invoice{about} ({numbers}), "
            "along with the supporting documents.\n\n"
            "Kindly confirm the order and arrange the payment as per the agreed terms, so that we can "
            "schedule production.\n\n"
            "You can also view the proforma invoice online using the link below:\n"
            f"{url}\n"
            f"Password: {password}\n\n"
            "Please let us know if you have any questions.\n\n"
            "Best regards,\n"
            "AVIGIRI Team"
        )
        return subject, body
    subject = f"Quotation {voucher_no or reference}" + (f" - {title}" if title else "")
    numbers = f"quotation no. {voucher_no}, reference {reference}" if voucher_no else f"reference {reference}"
    body = (
        "Dear Sir/Madam,\n\n"
        f"Thank you for your enquiry. Please find attached our quotation{about} ({numbers}), "
        "along with the supporting documents.\n\n"
        "You can also view the quotation online and accept or reject it using the link below:\n"
        f"{url}\n"
        f"Password: {password}\n\n"
        "Please let us know if you have any questions.\n\n"
        "Best regards,\n"
        "AVIGIRI Team"
    )
    return subject, body


def existing_thread(enquiry_id: str) -> Optional[Dict[str, str]]:
    """The enquiry's email thread with the customer, if one is known: the newest of the quotation / PI /
    rejection email that started follow-ups and the last sent follow-up. New emails reply in it."""
    start = (FollowUpStart.objects.filter(enquiry_id=str(enquiry_id)).exclude(gmail_thread_id="")
             .order_by("-started_at").first())
    sent = (FollowUpSent.objects.filter(enquiry_id=str(enquiry_id), status=FollowUpSent.STATUS_SENT)
            .exclude(gmail_thread_id="").order_by("-sent_at").first())
    known = [(row.started_at, row.gmail_thread_id, row.subject) for row in (start,) if row]
    known += [(row.sent_at, row.gmail_thread_id, row.subject) for row in (sent,) if row]
    if not known:
        return None
    _, thread_id, subject = max(known, key=lambda item: item[0])
    return {"thread_id": thread_id, "subject": subject or ""}


def form_defaults(request, record: ShareLink, data: Mapping[str, Any], password: str,
                  enquiry: Mapping[str, Any]) -> Dict[str, Any]:
    """What the 'Send to customer' form starts with."""
    enquiry_id = str(data.get("enquiry_id") or "")
    try:
        pdf_bytes = base64.b64decode(data.get("pdf_content_b64") or b"")
    except (ValueError, TypeError):
        pdf_bytes = b""
    voucher_no = voucher_number(pdf_bytes)
    url = share_url(request, record)
    messages = {key: dict(zip(("subject", "body"), default_message(enquiry_id, enquiry, url, password, voucher_no,
                                                                   document=key)))
                for key in DOCUMENTS}
    thread = existing_thread(enquiry_id) if enquiry_id else None
    return {
        "share_id": record.id,
        "enquiry_id": enquiry_id,
        "voucher_no": voucher_no,  # '' when the quotation PDF has no readable Voucher No.
        "document": "quotation",
        "documents_choices": [(key, item["label"]) for key, item in DOCUMENTS.items()],
        "messages": messages,
        # Known thread with this customer: the email is sent as a reply in it (same subject).
        "thread": thread,
        "to": customer_email(enquiry_id, enquiry),
        "cc": payment_terms.default_cc(enquiry_id),
        "subject": (thread or {}).get("subject") or messages["quotation"]["subject"],
        "body": messages["quotation"]["body"],
        "quotation_name": data.get("pdf_filename") or "quotation.pdf",
        "documents": enquiry_documents(enquiry, skip_name=str(data.get("pdf_filename") or "")),
        "selected": [],
    }


def download_documents(api_root: str, enquiry_id: str, documents: List[Mapping[str, Any]],
                       selected_ids: List[str], request=None) -> List[Dict[str, Any]]:
    """Download the ticked enquiry documents from the enquiry service."""
    wanted = [d for d in documents if d["id"] in set(selected_ids)]
    if len(wanted) != len(set(selected_ids)):
        raise ShareEmailError("One of the selected documents is no longer on this enquiry. Please reload the page.")
    files = []
    for document in wanted:
        url = document["download_url"] or f"/enquiries/{enquiry_id}/attachments/{document['id']}"
        try:
            content, filename, content_type = services.download_attachment(api_root, url, request=request)
        except services.EnquiryServiceError as exc:
            raise ShareEmailError(f"Could not download {document['name']}: {exc}") from exc
        files.append({"filename": filename or document["name"], "mime_type": content_type or "application/octet-stream",
                      "content": content})
    return files


def validate_recipients(to: str, cc_raw: str) -> Tuple[str, List[str]]:
    to = (to or "").strip().lower()
    addresses, error = payment_terms.parse_cc(to)
    if not to or error or len(addresses) != 1:
        raise ShareEmailError("Please enter one valid customer email address in To.")
    cc, error = payment_terms.parse_cc(cc_raw, exclude=to)
    if error:
        raise ShareEmailError(error)
    return to, cc


def check_size(attachments: List[Mapping[str, Any]]) -> None:
    total = sum(len(a.get("content") or b"") for a in attachments)
    if total > MAX_ATTACHMENT_BYTES:
        raise ShareEmailError(f"The attachments add up to {total / (1024 * 1024):.1f} MB; Gmail allows about "
                              f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB. Please untick some documents.")


def remember_recipients(enquiry_id: str, to: str, cc: List[str], user) -> None:
    """Keep the customer email (if none was saved) and the CC for next time."""
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    fields = []
    if not contact.customer_email:
        contact.customer_email = to
        fields.append("customer_email")
    if cc and contact.follow_up_cc != ", ".join(cc):
        contact.follow_up_cc = ", ".join(cc)
        fields.append("follow_up_cc")
    if fields:
        contact.updated_by = user if getattr(user, "is_authenticated", False) else None
        contact.save(update_fields=fields + ["updated_by", "updated_at"])

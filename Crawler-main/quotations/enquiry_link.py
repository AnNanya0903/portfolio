"""A quotation and the enquiry it answers (ENQ-NNNN).

The enquiries live in the enquiry service (its own database), so a quotation keeps the checked ENQ number in
Quotation.enquiry_id rather than a database key. This module:

- checks and looks up enquiry numbers in the service (`fetch`, `search` for the "Enquiry number" picker);
- changes the enquiry of a quotation, with a line in the tracker (`change_enquiry`);
- merges a duplicate enquiry into the one to keep (`merge_enquiries`, administrators only);
- suggests the enquiry of quotations entered before they were linked (`suggest_for`), for review.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import EnquiryLinkSuggestion, Quotation, QuotationEvent

logger = logging.getLogger(__name__)

ENQ_RE = re.compile(r"^ENQ-\d+$", re.IGNORECASE)
_OPTIONS_CACHE_KEY = "quotations:enquiry-options:v1"
_OPTIONS_CACHE_SECONDS = 60


class EnquiryNotFound(Exception):
    """No enquiry with that number in the enquiry service."""


class EnquiryUnavailable(Exception):
    """The enquiry service could not be reached; nothing was checked."""


class LinkError(Exception):
    """A link, merge or confirmation that cannot be done; the message is safe to show."""


def normalize(value: Any) -> str:
    """'enq-0012 ' -> 'ENQ-0012'; '' for anything that is not an enquiry number."""
    text = "".join(str(value or "").split()).upper()
    return text if ENQ_RE.match(text) else ""


def _api_root() -> str:
    return str(getattr(settings, "ENQUIRY_API_ROOT", "http://localhost:8405") or "").rstrip("/")


def fetch(enquiry_id: str, request=None) -> Dict[str, Any]:
    """The enquiry from the service. Raises EnquiryNotFound or EnquiryUnavailable."""
    from enquiries import services

    number = normalize(enquiry_id)
    if not number:
        raise EnquiryNotFound(f"“{enquiry_id}” is not an enquiry number (ENQ-0000).")
    try:
        return services.fetch_enquiry(_api_root(), number, request=request)
    except services.EnquiryServiceError as exc:
        if str(exc).startswith("Service returned 404"):
            raise EnquiryNotFound(f"There is no enquiry {number}.") from exc
        raise EnquiryUnavailable(f"The enquiry list could not be reached, so {number} was not checked. "
                                 "Please try again in a minute.") from exc


def check(enquiry_id: str, request=None) -> str:
    """The normalized number of an enquiry that exists. Raises LinkError with a message for the form."""
    try:
        return str(fetch(enquiry_id, request=request).get("enquiry_id") or normalize(enquiry_id))
    except (EnquiryNotFound, EnquiryUnavailable) as exc:
        raise LinkError(str(exc)) from exc


def _option(row: Mapping[str, Any]) -> Dict[str, Any]:
    received = row.get("received_at_dt") or row.get("received_at")
    return {
        "enquiry_id": str(row.get("enquiry_id") or ""),
        "customer_name": str(row.get("customer_name") or ""),
        "subject": str(row.get("subject") or ""),
        "status": str(row.get("status") or ""),
        "customer_email": str(row.get("customer_email") or ""),
        "received_at": received.isoformat() if isinstance(received, datetime) else str(received or ""),
    }


def all_enquiries(request=None, *, fresh: bool = False) -> List[Dict[str, Any]]:
    """Every enquiry (number, customer, subject, status, received), newest first. Cached for a minute.
    Raises EnquiryUnavailable."""
    from enquiries import services

    if not fresh:
        cached = cache.get(_OPTIONS_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        rows = services.list_enquiries(_api_root(), request=request)
    except services.EnquiryServiceError as exc:
        raise EnquiryUnavailable("The enquiry list could not be reached. Please try again in a minute.") from exc
    options = [_option(row) for row in rows if row.get("enquiry_id")]
    options.sort(key=lambda o: (o["received_at"], o["enquiry_id"]), reverse=True)
    cache.set(_OPTIONS_CACHE_KEY, options, _OPTIONS_CACHE_SECONDS)
    return options


def search(query: str, request=None, limit: int = 20) -> List[Dict[str, Any]]:
    """Enquiries whose number, customer or subject contain every word typed."""
    words = [w for w in str(query or "").lower().split() if w][:6]
    out = []
    for option in all_enquiries(request):
        haystack = f"{option['enquiry_id']} {option['customer_name']} {option['subject']}".lower()
        if all(w in haystack for w in words):
            out.append(option)
            if len(out) >= limit:
                break
    return out


def label(option: Mapping[str, Any]) -> str:
    parts = [option.get("enquiry_id"), option.get("customer_name"), option.get("subject")]
    return " · ".join(str(p) for p in parts if p)


# ----- change the enquiry of a quotation -------------------------------------------------------------------------

@transaction.atomic
def change_enquiry(quotation: Quotation, enquiry_id: str, *, user, how: str = "") -> bool:
    """Link the quotation to `enquiry_id` (already checked; '' unlinks it). The tracker says who, when, old -> new.
    Returns False when nothing changed."""
    quotation = Quotation.objects.select_for_update().get(pk=quotation.pk)
    new = normalize(enquiry_id) if enquiry_id else ""
    if enquiry_id and not new:
        raise LinkError(f"“{enquiry_id}” is not an enquiry number (ENQ-0000).")
    old = quotation.enquiry_id or ""
    if new == old:
        return False
    quotation.enquiry_id = new
    quotation.save(update_fields=["enquiry_id", "updated_at"])
    # The quotation's whole history now shows on its (new) enquiry.
    QuotationEvent.objects.filter(quotation=quotation).update(enquiry_id=new)
    QuotationEvent.objects.create(
        quotation=quotation, enquiry_id=new, action=QuotationEvent.ACTION_ENQUIRY_CHANGED, user=user,
        comment=f"{old or 'No enquiry'} → {new or 'No enquiry'}" + (f" ({how})" if how else ""),
        details={"old": old, "new": new, **({"how": how} if how else {})})
    suggestion = EnquiryLinkSuggestion.objects.filter(quotation=quotation).first()
    if suggestion is not None and new and suggestion.status == EnquiryLinkSuggestion.STATUS_PENDING:
        suggestion.status = EnquiryLinkSuggestion.STATUS_CONFIRMED
        suggestion.decided_by = user if getattr(user, "is_authenticated", False) else None
        suggestion.decided_at = timezone.now()
        suggestion.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    return True


# ----- merge a duplicate enquiry ---------------------------------------------------------------------------------

_CONTACT_TEXT_FIELDS = ("customer_email", "follow_up_cc")
_CONTACT_THREAD_FIELDS = ("email_message_id", "email_references", "email_subject", "gmail_thread_id",
                          "graph_message_id", "graph_conversation_id", "email_linked_at", "email_linked_by")


def _merge_contacts(duplicate: str, keep: str) -> None:
    from enquiries import customer_emails
    from enquiries.models import EnquiryContact

    dup = EnquiryContact.objects.filter(enquiry_id=duplicate).first()
    if dup is None:
        return
    kept = EnquiryContact.objects.filter(enquiry_id=keep).first()
    if kept is None:
        dup.enquiry_id = keep
        dup.save()
        return
    for name in _CONTACT_TEXT_FIELDS:
        if not getattr(kept, name) and getattr(dup, name):
            setattr(kept, name, getattr(dup, name))
    if not kept.has_email_thread and dup.has_email_thread:
        for name in _CONTACT_THREAD_FIELDS:
            setattr(kept, name, getattr(dup, name))
    others = customer_emails.split(kept.other_customer_emails)
    main = (kept.customer_email or "").lower()
    for address in [dup.customer_email or ""] + customer_emails.split(dup.other_customer_emails):
        address = address.strip().lower()
        if address and address != main and address not in others:
            others.append(address)
    kept.other_customer_emails = ", ".join(others[:customer_emails.MAX_OTHER])
    kept.save()
    dup.delete()


@dataclass
class MergeResult:
    quotations: List[str]
    deleted: bool
    error: str = ""


def merge_enquiries(duplicate: str, keep: str, *, user, request=None) -> MergeResult:
    """Move everything of the duplicate enquiry onto the one kept (its quotations, the tracker, the customer's
    email thread and addresses), then delete the duplicate in the enquiry service.

    When the service cannot delete it, the records are still moved and `error` says what to do."""
    from enquiries import services

    dup, kept = normalize(duplicate), normalize(keep)
    if not dup or not kept:
        raise LinkError("Both enquiry numbers must look like ENQ-0000.")
    if dup == kept:
        raise LinkError("Pick a different enquiry to keep.")
    try:
        fetch(kept, request=request)
    except EnquiryNotFound as exc:
        raise LinkError(f"The enquiry to keep does not exist: {exc}") from exc
    except EnquiryUnavailable as exc:
        raise LinkError(str(exc)) from exc
    with transaction.atomic():
        moved = list(Quotation.objects.filter(enquiry_id=dup).order_by("id").values_list("number", flat=True))
        Quotation.objects.filter(enquiry_id=dup).update(enquiry_id=kept, updated_at=timezone.now())
        QuotationEvent.objects.filter(enquiry_id=dup).update(enquiry_id=kept)
        EnquiryLinkSuggestion.objects.filter(enquiry_id=dup).update(enquiry_id=kept)
        _merge_contacts(dup, kept)
        QuotationEvent.objects.create(
            enquiry_id=kept, action=QuotationEvent.ACTION_ENQUIRY_MERGED, user=user,
            comment=f"{dup} merged into {kept}" + (f": {', '.join(moved)} moved" if moved else ""),
            details={"duplicate": dup, "kept": kept, "quotations": moved})
    cache.delete(_OPTIONS_CACHE_KEY)
    try:
        services.delete_enquiry(_api_root(), dup, request=request)
    except services.EnquiryServiceError as exc:
        logger.warning("Merge %s -> %s: records moved but the duplicate was not deleted: %s", dup, kept, exc)
        return MergeResult(quotations=moved, deleted=False,
                           error=f"Everything was moved to {kept}, but {dup} could not be deleted ({exc}). "
                                 f"Close {dup} on the board by hand.")
    return MergeResult(quotations=moved, deleted=True)


# ----- suggestions for quotations entered before linking ---------------------------------------------------------

_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|sv)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_CODE_RE = re.compile(r"\bAG[\s_-]*(?:QR|RQ|QF)\b[\w/-]*", re.IGNORECASE)
_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|" \
          r"september|october|november|december"
_DATE_RES = (
    re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?[\s.-]*(?:{_MONTHS})[a-z]*[\s.,-]*(?:\d{{2,4}})?\b", re.IGNORECASE),
    re.compile(rf"\b(?:{_MONTHS})[a-z]*[\s.-]*\d{{1,2}}(?:st|nd|rd|th)?[\s.,-]*\d{{2,4}}\b", re.IGNORECASE),
)
_PUNCT_RE = re.compile(r"[^\w\s]|_")
_COMPANY_WORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "the", "india", "corp",
                  "corporation", "and"}
_GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com", "rediffmail.com",
                    "live.com", "icloud.com", "avigiri.com"}


def clean_subject(text: str) -> str:
    """Re:/Fw:/Fwd:, AG-QR/AG-RQ/AG-QF codes, dates and punctuation removed; lower case, single spaces."""
    text = _PREFIX_RE.sub("", str(text or ""))
    text = _CODE_RE.sub(" ", text)
    for pattern in _DATE_RES:
        text = pattern.sub(" ", text)
    return " ".join(_PUNCT_RE.sub(" ", text).lower().split())


_MS_RE = re.compile(r"(?<![a-z])m\s*/\s*s(?![a-z])\.?", re.IGNORECASE)  # "M/s." (Messrs) before a name


def clean_customer(name: str) -> str:
    words = _PUNCT_RE.sub(" ", _MS_RE.sub(" ", str(name or "")).lower()).split()
    return " ".join(w for w in words if w not in _COMPANY_WORDS)


def _domain(address: str) -> str:
    address = str(address or "").strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    return "" if domain in _GENERIC_DOMAINS else domain


def _contains(a: str, b: str) -> bool:
    return bool(a and b) and (a in b or b in a)


def _quotation_domains(quotation: Quotation) -> set:
    domains = set()
    if quotation.customer_id:
        try:
            for contact in quotation.customer.contacts.all():
                for address in contact.email_addresses():
                    if _domain(address):
                        domains.add(_domain(address))
        except Exception:  # KYC contacts are a nice-to-have here
            pass
    return domains


def _customer_reason(quotation: Quotation, enquiry: Mapping[str, Any], enquiry_emails: Sequence[str],
                     domains: set) -> str:
    ours, theirs = clean_customer(quotation.customer_name), clean_customer(enquiry.get("customer_name") or "")
    if ours and theirs and ours == theirs:
        return "same customer"
    if len(ours) >= 3 and len(theirs) >= 3 and _contains(ours, theirs):
        return "similar customer name"
    shared = domains & {_domain(a) for a in enquiry_emails if _domain(a)}
    if shared:
        return f"same email domain ({sorted(shared)[0]})"
    return ""


def _subject_match(quotation: Quotation, enquiry_subject: str) -> str:
    theirs = clean_subject(enquiry_subject)
    if not theirs:
        return ""
    best = ""
    for text in (quotation.title, quotation.enquiry_reference, f"{quotation.title} {quotation.enquiry_reference}"):
        ours = clean_subject(text)
        if not ours:
            continue
        if ours == theirs:
            return "exact"
        if _contains(ours, theirs):
            best = "contains"
    return best


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if timezone.is_aware(value) else timezone.make_aware(value, dt_timezone.utc)


@dataclass
class Suggestion:
    enquiry: Optional[Dict[str, Any]]
    reason: str
    confidence: str
    candidates: int = 0


def suggest_for(quotation: Quotation, enquiries: Iterable[Mapping[str, Any]],
                emails_by_enquiry: Mapping[str, Sequence[str]]) -> Suggestion:
    """The enquiry this quotation most likely answers: the same customer (name or email domain) AND the same
    subject (exact, or one contains the other) once Re:/Fw:, AG-QR codes, dates and punctuation are removed.
    Among several, the one received closest to the quotation's date."""
    domains = _quotation_domains(quotation)
    when = _aware(quotation.created_at)
    found: List[Tuple[int, float, Dict[str, Any], str, str]] = []
    for enquiry in enquiries:
        number = str(enquiry.get("enquiry_id") or "")
        emails = [enquiry.get("customer_email") or ""] + list(emails_by_enquiry.get(number, ()))
        customer = _customer_reason(quotation, enquiry, emails, domains)
        if not customer:
            continue
        subject = _subject_match(quotation, enquiry.get("subject") or "")
        if not subject:
            continue
        received = _aware(enquiry.get("received_at"))
        gap = abs((received - when).total_seconds()) if received and when else float("inf")
        found.append((0 if subject == "exact" else 1, gap, dict(enquiry), customer, subject))
    if not found:
        return Suggestion(enquiry=None, reason="No enquiry of this customer has a matching subject.", confidence="")
    found.sort(key=lambda item: (item[0], item[1]))
    rank, gap, enquiry, customer, subject = found[0]
    confidence = EnquiryLinkSuggestion.CONFIDENCE_HIGH if subject == "exact" and customer == "same customer" \
        else EnquiryLinkSuggestion.CONFIDENCE_MEDIUM
    if len(found) > 1 and found[1][0] == rank:  # another enquiry matches just as well
        confidence = EnquiryLinkSuggestion.CONFIDENCE_LOW if confidence == EnquiryLinkSuggestion.CONFIDENCE_MEDIUM \
            else EnquiryLinkSuggestion.CONFIDENCE_MEDIUM
    parts = [customer, "same subject" if subject == "exact" else "subject contains the other"]
    if gap != float("inf"):
        parts.append(f"received {int(gap // 86400)} day(s) from the quotation")
    if len(found) > 1:
        parts.append(f"{len(found)} enquiries matched, closest date chosen")
    return Suggestion(enquiry=enquiry, reason="; ".join(parts), confidence=confidence, candidates=len(found))


def refresh_suggestions(enquiries: Sequence[Mapping[str, Any]]) -> List[EnquiryLinkSuggestion]:
    """Suggest an enquiry for every quotation without one. Never links anything. Safe to run again: a linked
    quotation is skipped, and a rejected suggestion stays rejected unless a different enquiry is found now."""
    from enquiries.models import EnquiryContact
    from enquiries import customer_emails

    emails: Dict[str, List[str]] = {}
    for contact in EnquiryContact.objects.exclude(customer_email="", other_customer_emails=""):
        emails[contact.enquiry_id] = [contact.customer_email] + customer_emails.split(contact.other_customer_emails)
    rows = []
    for quotation in Quotation.objects.filter(enquiry_id="").select_related("customer").order_by("id"):
        result = suggest_for(quotation, enquiries, emails)
        number = str((result.enquiry or {}).get("enquiry_id") or "")
        values = {"enquiry_id": number, "enquiry_customer": str((result.enquiry or {}).get("customer_name") or "")[:255],
                  "enquiry_subject": str((result.enquiry or {}).get("subject") or "")[:500],
                  "reason": result.reason[:500], "confidence": result.confidence}
        row = EnquiryLinkSuggestion.objects.filter(quotation=quotation).first()
        if row is None:
            row = EnquiryLinkSuggestion.objects.create(quotation=quotation, **values)
        elif row.status == EnquiryLinkSuggestion.STATUS_REJECTED and row.enquiry_id == number:
            pass  # the same answer was already turned down
        else:
            for key, value in values.items():
                setattr(row, key, value)
            row.status = EnquiryLinkSuggestion.STATUS_PENDING
            row.save()
        rows.append(row)
    return rows


def confirm_suggestion(suggestion: EnquiryLinkSuggestion, *, user, enquiry_id: str = "", request=None) -> str:
    """Link the quotation to the suggested enquiry, or to `enquiry_id` picked instead. Returns the ENQ linked."""
    wanted = enquiry_id or suggestion.enquiry_id
    if not wanted:
        raise LinkError("Pick the enquiry to link.")
    number = check(wanted, request=request)
    change_enquiry(suggestion.quotation, number, user=user,
                   how="suggestion confirmed" if number == suggestion.enquiry_id else "picked on review")
    suggestion.refresh_from_db()
    suggestion.status = EnquiryLinkSuggestion.STATUS_CONFIRMED
    suggestion.enquiry_id = number
    suggestion.decided_by, suggestion.decided_at = user, timezone.now()
    suggestion.save()
    return number


def reject_suggestion(suggestion: EnquiryLinkSuggestion, *, user) -> None:
    suggestion.status = EnquiryLinkSuggestion.STATUS_REJECTED
    suggestion.decided_by, suggestion.decided_at = user, timezone.now()
    suggestion.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])

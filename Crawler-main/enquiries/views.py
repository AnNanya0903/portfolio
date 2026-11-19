"""Enquiry module views for the MagicWand Django shell."""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import string
import math
from decimal import Decimal
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple
from urllib.parse import quote, urlencode

from django import forms as django_forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.core import signing
from django.db.models import Max
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.dateparse import parse_date
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from allauth.socialaccount.models import SocialAccount

from . import forms, services
from .models import CustomerStatusEmail, EnquiryContact, FollowUpSent, FollowUpStart, GmailLeadAnalysis, ShareLink
from . import customer_emails, negotiation, payment_terms, share_email, status_emails
from .constants import (
    COSTING_FIELD_LAYOUT,
    COSTING_FIELD_ORDER,
    COLUMN_MATRIX_ROWS,
    COMPOUNDING_FIELD_LAYOUT,
    COMPOUNDING_FIELD_ORDER,
    DESIGN_FIELD_LAYOUT,
    DESIGN_FIELD_ORDER,
    DETAIL_FIELD_LABELS,
    PRODUCTION_FIELD_LAYOUT,
    PRODUCTION_FIELD_ORDER,
    PRIMARY_CREATE_FIELDS,
    PRIMARY_UPDATE_FIELDS,
    TEXTAREA_FIELDS,
    STATUS_CHOICES,
)
from .pdf_preview import generate_preview_pdf
from dashboard.follow_up_templates import MARKETING_STAGE, PAYMENT_STAGE, STAGE_CHOICES as FOLLOW_UP_STAGE_CHOICES
from kyc import links as kyc_links
from dashboard.gmail_service import GmailService, GmailAPIError
from dashboard.ai_service import AIReplyRequest, AIReplyService, AIServiceError

SHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
_FORM_FIELD_LOOKUP: Dict[str, str] | None = None
_FORM_LABEL_LOOKUP: Dict[str, str] | None = None


class GoogleSheetImportError(RuntimeError):
    """Raised when a Google Sheet cannot be converted into an enquiry."""

logger = logging.getLogger(__name__)

GMAIL_LEAD_HEURISTIC_MODEL = "lead-heuristic-v2"

SUMMARY_PERIOD_CHOICES = {
    "all": "All enquiries",
    "weekly": "Last 7 days",
    "monthly": "Last 30 days",
    "custom": "Custom date range",
}


# The enquiry service works in UTC; the summary shows times in Indian time.
SUMMARY_DISPLAY_TIME_ZONE = "Asia/Kolkata"
SUMMARY_DISPLAY_UTC_OFFSET_MINUTES = 330  # +05:30; India has no daylight saving. Used to cut custom dates into days.
_SUMMARY_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _tidy_summary(data: MutableMapping[str, Any]) -> None:
    """Make the service's summary read correctly on the page.

    - generated_at comes as naive UTC: mark it as UTC so it is shown in Indian time;
    - priorities in their natural order (high, medium, low) instead of alphabetical;
    - closed_status_enquiries: enquiries whose status is "closed", so the counts on the page add up to
      the total (the service's closed_enquiries means "not open" and already includes accepted etc.).
    """
    generated = data.get("generated_at_dt")
    if isinstance(generated, datetime) and timezone.is_naive(generated):
        data["generated_at_dt"] = timezone.make_aware(generated, timezone.utc)
    metrics = data.get("metrics")
    if not isinstance(metrics, MutableMapping):
        return
    by_priority = metrics.get("by_priority")
    if isinstance(by_priority, Mapping):
        metrics["by_priority"] = dict(sorted(
            by_priority.items(),
            key=lambda item: (_SUMMARY_PRIORITY_ORDER.get(str(item[0]).strip().lower(), 9), str(item[0])),
        ))
    by_status = metrics.get("by_status")
    try:
        if isinstance(by_status, Mapping):
            closed = int(by_status.get("closed") or 0)
        else:
            closed = max(
                int(metrics.get("total_enquiries") or 0) - int(metrics.get("open_enquiries") or 0)
                - sum(int(metrics.get(key) or 0)
                      for key in ("accepted_enquiries", "rejected_enquiries", "converted_enquiries")),
                0,
            )
    except (TypeError, ValueError):
        closed = 0
    metrics["closed_status_enquiries"] = closed


def _normalize_summary_filter_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _resolve_summary_filter_bounds(
    period: str,
    *,
    start_date: str = "",
    end_date: str = "",
) -> tuple[Optional[datetime], Optional[datetime]]:
    now = timezone.now()

    if period == "weekly":
        return now - timedelta(days=7), now
    if period == "monthly":
        return now - timedelta(days=30), now
    if period != "custom":
        return None, None

    # The dates are Indian days, the same as the times the report shows: 12:00 AM to 11:59 PM IST.
    # (Cutting them at UTC midnight, which is 5:30 AM IST, put enquiries on the wrong day.)
    day_zone = timezone.get_fixed_timezone(SUMMARY_DISPLAY_UTC_OFFSET_MINUTES)
    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None
    if start_date:
        parsed_start = date.fromisoformat(start_date)
        range_start = datetime.combine(parsed_start, datetime.min.time(), tzinfo=day_zone)
    if end_date:
        parsed_end = date.fromisoformat(end_date)
        range_end = datetime.combine(parsed_end, datetime.max.time(), tzinfo=day_zone)
    return range_start, range_end


def _filter_summary_enquiries(
    enquiries: Iterable[Mapping[str, Any]],
    *,
    period: str,
    start_date: str = "",
    end_date: str = "",
) -> List[Dict[str, Any]]:
    range_start, range_end = _resolve_summary_filter_bounds(
        period,
        start_date=start_date,
        end_date=end_date,
    )
    filtered: List[Dict[str, Any]] = []
    for enquiry in enquiries:
        item = dict(enquiry)
        received_at = _normalize_summary_filter_datetime(item.get("received_at_dt"))
        if range_start and (received_at is None or received_at < range_start):
            continue
        if range_end and (received_at is None or received_at > range_end):
            continue
        filtered.append(item)
    filtered.sort(
        key=lambda item: _normalize_summary_filter_datetime(item.get("received_at_dt")) or datetime.min.replace(
            tzinfo=timezone.get_current_timezone()
        ),
        reverse=True,
    )
    return filtered


def _gmail_web_message_url(user, message_id: str) -> str:
    authuser = None
    try:
        account = SocialAccount.objects.filter(user=user, provider="google").first()
        if account and isinstance(account.extra_data, dict):
            authuser = (account.extra_data.get("email") or "").strip() or None
    except Exception:
        authuser = None

    base = "https://mail.google.com/mail/u/0/"
    if authuser:
        base = f"{base}?authuser={quote(authuser)}"

    return f"{base}#inbox/{message_id}"


def _build_enquiry_follow_up_query(enquiry: Mapping[str, Any]) -> str:
    subject = str(enquiry.get("subject") or "").strip().replace('"', "")
    customer_name = str(enquiry.get("customer_name") or "").strip().replace('"', "")
    terms: List[str] = []
    if subject:
        terms.append(f'subject:"{subject}"')
    if customer_name and customer_name.lower() not in subject.lower():
        terms.append(f'"{customer_name}"')
    return " ".join(term for term in terms if term).strip()


def _get_google_reauth_url() -> str:
    try:
        return reverse("socialaccount_login", args=("google",))
    except Exception:
        return "/accounts/google/login/?process=login"


def _parse_ai_json_payload(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    try:
        decoded = json.loads(cleaned)
        if isinstance(decoded, dict):
            return decoded
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    snippet = cleaned[start : end + 1]
    try:
        decoded = json.loads(snippet)
    except Exception:
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}


def _build_lead_prompt(message: Mapping[str, Any]) -> str:
    subject = str(message.get("subject") or "").strip()
    sender = str(message.get("sender") or message.get("from") or "").strip()
    snippet = str(message.get("snippet") or "").strip()
    body = str(message.get("body_text") or "").strip()
    if body:
        body = body[:3000]

    attachments = message.get("attachments") or []
    attachment_lines: List[str] = []
    if isinstance(attachments, list):
        for entry in attachments[:10]:
            if not isinstance(entry, Mapping):
                continue
            filename = str(entry.get("filename") or "").strip()
            mime_type = str(entry.get("mimeType") or entry.get("mime_type") or "").strip()
            if filename:
                attachment_lines.append(f"- {filename} ({mime_type or 'unknown'})")

    attachment_block = "\n".join(attachment_lines) if attachment_lines else "(none)"

    return (
        "You are a sales lead qualification assistant for a manufacturing company.\n"
        "Decide whether this inbound email is a qualified sales lead / RFQ / enquiry that should be tracked.\n"
        "Treat these as sales leads when they are inbound customer or prospect requests for quotation, pricing, "
        "drawing review, product development, samples, mould/die charges, feasibility, lead time, material "
        "selection, purchase orders, or gasket/profile requirements.\n\n"
        "Return ONLY valid JSON with these keys:\n"
        "- is_lead: boolean\n"
        "- lead_score: integer 0-100\n"
        "- reason: short string (<=200 chars)\n"
        "- suggested_next_step: short string (<=120 chars)\n\n"
        "Email:\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Snippet: {snippet}\n"
        f"Body: {body}\n"
        f"Attachments:\n{attachment_block}\n"
    )


def _build_gmail_lead_queries(user_email: str | None = None) -> List[str]:
    custom_query = (os.getenv("ENQUIRY_EMAIL_SEARCH_QUERY") or "").strip()
    if custom_query:
        return [custom_query]

    sender_exclusion = " -from:me"
    if user_email:
        sender_exclusion += f" -from:{user_email}"
    base_filters = f"has:attachment filename:pdf -in:spam -in:trash -category:promotions -category:social{sender_exclusion}"
    lead_terms = (
        '(rfq OR quotation OR quote OR enquiry OR inquiry OR gasket OR rubber OR seal '
        'OR profile OR feasibility OR drawing OR "curtain wall")'
    )

    return [
        f"{base_filters} {lead_terms} newer_than:365d".strip(),
        f"{base_filters} newer_than:365d".strip(),
        base_filters.strip(),
    ]


def _gmail_attachment_is_pdf(attachment: Mapping[str, Any]) -> bool:
    filename = str(attachment.get("filename") or attachment.get("name") or "").strip().lower()
    mime_type = str(
        attachment.get("mimeType")
        or attachment.get("mime_type")
        or attachment.get("contentType")
        or attachment.get("content_type")
        or ""
    ).strip().lower()
    return filename.endswith(".pdf") or mime_type.endswith("/pdf") or mime_type.endswith("+pdf")


def _gmail_message_has_pdf_attachment(message: Mapping[str, Any]) -> bool:
    attachments = message.get("attachments") or []
    if not isinstance(attachments, list):
        return False
    return any(isinstance(item, Mapping) and _gmail_attachment_is_pdf(item) for item in attachments)


def _coerce_candidate_received_at(value: Any) -> timezone.datetime:
    """A timezone-aware date to sort Gmail lead candidates by. A date without a time zone is taken as UTC, and a
    missing or unreadable one sorts last, so naive and aware dates never meet in the sort."""
    if isinstance(value, timezone.datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value, timezone.utc)
    return timezone.make_aware(timezone.datetime(1970, 1, 1), timezone.utc)


def _search_gmail_lead_candidates(
    gmail_service: GmailService,
    *,
    user_email: str | None = None,
    candidate_limit: int = 50,
) -> List[Dict[str, Any]]:
    per_query_limit = max(candidate_limit, 25)
    collected: Dict[str, Dict[str, Any]] = {}

    for query in _build_gmail_lead_queries(user_email):
        messages = gmail_service.search_messages(query, limit=per_query_limit)
        for message in messages:
            message_id = str(message.get("id") or "").strip()
            if not message_id or message_id in collected:
                continue
            collected[message_id] = {
                "message_id": message_id,
                "mailbox": None,
                "sender": (message.get("sender") or ""),
                "subject": (message.get("subject") or ""),
                "snippet": (message.get("snippet") or ""),
                "received_at": message.get("parsed_date"),
            }

    candidates = list(collected.values())
    candidates.sort(key=lambda row: _coerce_candidate_received_at(row.get("received_at")), reverse=True)
    return candidates[:candidate_limit]


def _score_gmail_lead_heuristically(
    message: Mapping[str, Any],
    *,
    user_email: str | None = None,
) -> Dict[str, Any]:
    subject = str(message.get("subject") or "").strip()
    sender = str(message.get("sender") or message.get("from") or "").strip()
    snippet = str(message.get("snippet") or "").strip()
    body = str(message.get("body_text") or "").strip()
    subject_lower = subject.lower()
    combined_lower = " ".join(part for part in [subject, snippet, body] if part).lower()

    attachments = message.get("attachments") or []
    attachment_names: List[str] = []
    if isinstance(attachments, list):
        for item in attachments:
            if not isinstance(item, Mapping):
                continue
            filename = str(item.get("filename") or "").strip()
            if filename:
                attachment_names.append(filename.lower())

    signals: List[str] = []
    blockers: List[str] = []
    score = 0
    intent_score = 0
    relevance_score = 0
    technical_score = 0
    negative_score = 0

    intent_weights = [
        ("request for quotation", 45, "RFQ"),
        ("rfq", 45, "RFQ"),
        ("quotation", 34, "quotation"),
        ("quote", 32, "quote"),
        ("enquiry", 34, "enquiry"),
        ("inquiry", 34, "inquiry"),
        ("purchase order", 38, "purchase order"),
        ("po", 30, "PO"),
        ("requirement", 28, "requirement"),
        ("required", 24, "required"),
        ("require", 24, "require"),
        ("requesting", 24, "requesting"),
        ("request", 18, "request"),
        ("pricing", 22, "pricing"),
        ("price", 18, "price"),
        ("sample", 20, "sample"),
        ("development", 18, "development"),
        ("feasibility", 32, "feasibility"),
        ("lead time", 18, "lead time"),
        ("mould", 16, "mould"),
        ("mold", 16, "mold"),
        ("die", 16, "die"),
        ("manufacture", 16, "manufacture"),
        ("supply", 16, "supply"),
    ]
    relevance_weights = [
        ("gasket", 38, "gasket"),
        ("rubber", 34, "rubber"),
        ("seal", 26, "seal"),
        ("sealing", 26, "sealing"),
        ("profile", 26, "profile"),
        ("extrusion", 24, "extrusion"),
        ("epdm", 22, "EPDM"),
        ("silicone", 20, "silicone"),
        ("neoprene", 20, "neoprene"),
        ("o-ring", 22, "O-ring"),
        ("oring", 22, "O-ring"),
        ("washer", 16, "washer"),
        ("curtain wall", 22, "curtain wall"),
        ("glazing", 18, "glazing"),
        ("window", 16, "window"),
        ("door", 16, "door"),
    ]
    technical_weights = [
        ("drawing", 20, "drawing"),
        ("cad", 18, "CAD"),
        ("dxf", 18, "DXF"),
        ("dwg", 18, "DWG"),
        ("step", 16, "STEP"),
        ("stp", 16, "STP"),
        ("specification", 16, "specification"),
        ("dimension", 14, "dimension"),
        ("section", 12, "section"),
        ("part no", 10, "part number"),
        ("part number", 10, "part number"),
    ]
    negative_weights = [
        ("invoice", 35, "invoice"),
        ("tax invoice", 35, "tax invoice"),
        ("payment", 30, "payment"),
        ("receipt", 30, "receipt"),
        ("statement", 28, "statement"),
        ("ledger", 28, "ledger"),
        ("delivery challan", 30, "delivery challan"),
        ("packing list", 28, "packing list"),
        ("eway bill", 28, "eway bill"),
        ("e-way bill", 28, "e-way bill"),
        ("certificate", 24, "certificate"),
        ("test report", 28, "test report"),
        ("inspection report", 28, "inspection report"),
        ("catalogue", 24, "catalogue"),
        ("catalog", 24, "catalog"),
        ("brochure", 24, "brochure"),
        ("newsletter", 30, "newsletter"),
        ("resume", 30, "resume"),
        ("cv", 30, "CV"),
    ]

    def apply_weights(weights: List[Tuple[str, int, str]], *, whole_word: bool = False) -> int:
        subtotal = 0
        for needle, weight, label in weights:
            if whole_word and len(needle) <= 3:
                matched = re.search(rf"\b{re.escape(needle)}\b", combined_lower) is not None
            else:
                matched = needle in combined_lower
            if matched:
                boost = 8 if needle in subject_lower else 0
                subtotal += weight + boost
                signals.append(label)
        return subtotal

    intent_score += apply_weights(intent_weights, whole_word=True)
    relevance_score += apply_weights(relevance_weights)
    technical_score += apply_weights(technical_weights, whole_word=True)

    for needle, weight, label in negative_weights:
        if len(needle) <= 3:
            matched = re.search(rf"\b{re.escape(needle)}\b", combined_lower) is not None
        else:
            matched = needle in combined_lower
        if matched:
            negative_score += weight
            blockers.append(label)

    if re.search(r"\b(ap|agr|agm|cw)[- ]?\d{2,6}\b", combined_lower):
        technical_score += 12
        signals.append("part/code reference")

    score += intent_score + relevance_score + technical_score

    if attachment_names:
        score += 8
        technical_score += 8
        signals.append("attachment")

        if any(name.endswith(ext) for name in attachment_names for ext in (".pdf", ".xlsx", ".xls", ".dwg", ".dxf", ".step", ".stp", ".docx")):
            score += 12
            technical_score += 12
            signals.append("technical attachment")

    sender_email = ""
    sender_match = re.search(r"<([^>]+)>", sender)
    if sender_match:
        sender_email = sender_match.group(1).strip().lower()
    elif "@" in sender:
        sender_email = sender.strip().lower()
    user_domain = (user_email or "").split("@")[-1].lower() if user_email and "@" in user_email else ""
    sender_domain = sender_email.split("@")[-1].lower() if sender_email and "@" in sender_email else ""
    if sender_domain and user_domain and sender_domain != user_domain:
        score += 10
        signals.append("external sender")

    deduped_signals = list(dict.fromkeys(signals))
    if negative_score:
        score -= min(35, negative_score)

    has_buying_intent = intent_score >= 24
    has_relevant_product = relevance_score >= 20
    has_technical_context = technical_score >= 24
    strongly_negative = negative_score >= 30 and intent_score < 45
    if not has_buying_intent:
        blockers.append("no buying intent")
    if not has_relevant_product:
        blockers.append("no gasket/rubber relevance")
    if not has_technical_context:
        blockers.append("no technical requirement")

    deduped_blockers = list(dict.fromkeys(blockers))
    score = max(0, min(score, 100))
    is_lead = (
        score >= 65
        and has_buying_intent
        and has_relevant_product
        and has_technical_context
        and not strongly_negative
    )
    return {
        "is_lead": is_lead,
        "lead_score": score,
        "reason": (
            f"Heuristic lead detection matched: {', '.join(deduped_signals[:4])}."
            if is_lead and deduped_signals
            else f"Rejected by lead filter: {', '.join(deduped_blockers[:3])}."
        )[:220],
        "next_step": "Review the email and qualify the enquiry." if is_lead else "",
        "signals": deduped_signals,
        "blockers": deduped_blockers,
    }


def _collect_field_groups(form: forms.EnquiryBaseForm) -> Dict[str, List[Dict[str, Any]]]:
    design = [
        {"field": form[name], "column_class": DESIGN_FIELD_LAYOUT.get(name, "col-md-4")}
        for name in DESIGN_FIELD_ORDER
        if name in form.fields
    ]
    production = [
        {"field": form[name], "column_class": PRODUCTION_FIELD_LAYOUT.get(name, "col-md-4")}
        for name in PRODUCTION_FIELD_ORDER
        if name in form.fields
    ]
    compounding = [
        {"field": form[name], "column_class": COMPOUNDING_FIELD_LAYOUT.get(name, "col-md-4")}
        for name in COMPOUNDING_FIELD_ORDER
        if name in form.fields
    ]
    costing = [
        {"field": form[name], "column_class": COSTING_FIELD_LAYOUT.get(name, "col-md-4")}
        for name in COSTING_FIELD_ORDER
        if name in form.fields
    ]
    return {
        "design": design,
        "production": production,
        "compounding": compounding,
        "costing": costing,
    }

FIELD_LAYOUT_OVERRIDES: Dict[str, str] = {}
FIELD_LAYOUT_OVERRIDES.update(DESIGN_FIELD_LAYOUT)
FIELD_LAYOUT_OVERRIDES.update(PRODUCTION_FIELD_LAYOUT)
FIELD_LAYOUT_OVERRIDES.update(COMPOUNDING_FIELD_LAYOUT)
FIELD_LAYOUT_OVERRIDES.update(COSTING_FIELD_LAYOUT)

PREVIEW_SKIP_FIELDS = {"attachments", "profile"}
PDF_EXTENSION = ".pdf"

EVENT_FILTERS = [
    ("all", "All statuses"),
    ("pending", "Pending"),
    ("created", "Created"),
    ("skipped", "Skipped"),
    ("error", "Error"),
]

PRIORITY_CHOICES = [
    ("high", "High"),
    ("medium", "Medium"),
    ("low", "Low"),
]
DEFAULT_PRIORITY = "medium"
CONVERTED_STATUS = "converted"
STATUS_VALUE_SET = {value for value, _ in STATUS_CHOICES}
NON_CONVERTED_STATUS_SET = {value for value in STATUS_VALUE_SET if value != CONVERTED_STATUS}
CONVERSION_SESSION_KEY = "enquiry_converted_previous_statuses"


def _attachment_is_pdf(attachment: Mapping[str, Any]) -> bool:
    file_name = str(attachment.get("file_name") or "").lower()
    content_type = str(attachment.get("content_type") or "").lower()
    if file_name.endswith(PDF_EXTENSION):
        return True
    return content_type.endswith("/pdf") or content_type.endswith("+pdf")


def _attach_primary_pdf_link(enquiry: MutableMapping[str, Any]) -> None:
    if enquiry.get("sheet_url"):
        return
    attachments = enquiry.get("general_attachments") or []
    if not attachments:
        return
    enquiry_id = enquiry.get("enquiry_id")
    if not enquiry_id:
        return
    for attachment in attachments:
        try:
            attachment_mapping = dict(attachment)
        except Exception:
            attachment_mapping = {}
        if not attachment_mapping:
            continue
        if not _attachment_is_pdf(attachment_mapping):
            continue
        attachment_id = attachment_mapping.get("attachment_id")
        if not attachment_id:
            continue
        pdf_url = reverse("enquiries:download_attachment", args=[enquiry_id, attachment_id])
        enquiry["primary_pdf_url"] = f"{pdf_url}?inline=1"
        enquiry["primary_pdf_attachment"] = attachment_mapping
        break


def _first_pdf_name(record: Mapping[str, Any] | None) -> str:
    """File name of the PDF already attached to the enquiry (used for the share link if none is uploaded)."""
    for attachment in (record or {}).get("general_attachments") or (record or {}).get("attachments") or []:
        try:
            attachment_mapping = dict(attachment)
        except Exception:
            continue
        if attachment_mapping and _attachment_is_pdf(attachment_mapping):
            return str(attachment_mapping.get("file_name") or "PDF attached to this enquiry")
    return ""


def _record_has_pdf(record: Mapping[str, Any] | None) -> bool:
    if not record:
        return False
    attachments = record.get("general_attachments") or record.get("attachments") or []
    for attachment in attachments:
        try:
            attachment_mapping = dict(attachment)
        except Exception:
            attachment_mapping = {}
        if not attachment_mapping:
            continue
        if _attachment_is_pdf(attachment_mapping):
            return True
    return False


def _download_primary_pdf(
    api_root: str,
    enquiry_id: str,
    record: Mapping[str, Any],
    request: Optional[HttpRequest],
    subdivision_id: Optional[str] = None,
) -> Optional[Tuple[bytes, str, str]]:
    attachments = record.get("general_attachments") or record.get("attachments") or []
    for attachment in attachments:
        try:
            attachment_mapping = dict(attachment)
        except Exception:
            attachment_mapping = {}
        if not attachment_mapping or not _attachment_is_pdf(attachment_mapping):
            continue
        download_url = attachment_mapping.get("download_url") or ""
        attachment_id = attachment_mapping.get("attachment_id")
        if not download_url and attachment_id:
            if subdivision_id:
                download_url = f"/enquiries/{enquiry_id}/subdivisions/{subdivision_id}/attachments/{attachment_id}"
            else:
                download_url = f"/enquiries/{enquiry_id}/attachments/{attachment_id}"
        if not download_url:
            continue
        try:
            content, filename, content_type = services.download_attachment(api_root, download_url, request=request)
        except services.EnquiryServiceError:
            continue
        # The service sends no file name: use the enquiry's own name for the file, and the real PDF type.
        name = share_email.proper_file_name(filename, preferred=str(attachment_mapping.get("file_name") or ""),
                                            content=content)
        return content, name, share_email.proper_content_type(content_type, name, content)
    return None


def _extract_rejection_reason(remarks: Any) -> str:
    text = str(remarks or "").strip()
    if not text:
        return ""

    marker = "Rejection reason (via share link):"
    matched_lines = [
        line[len(marker) :].strip()
        for line in text.splitlines()
        if line.strip().startswith(marker)
    ]
    if matched_lines:
        return matched_lines[-1]
    return text


def _normalise_field_name(name: str) -> str:
    return slugify(name or "").replace("-", "_")


def _form_field_lookup() -> Dict[str, str]:
    global _FORM_FIELD_LOOKUP
    if _FORM_FIELD_LOOKUP is None:
        form = forms.EnquiryCreateForm()
        _FORM_FIELD_LOOKUP = {_normalise_field_name(field_name): field_name for field_name in form.fields.keys()}
    return _FORM_FIELD_LOOKUP


def _form_label_lookup() -> Dict[str, str]:
    global _FORM_LABEL_LOOKUP
    if _FORM_LABEL_LOOKUP is None:
        _FORM_LABEL_LOOKUP = {
            _normalise_field_name(label): field_name for field_name, label in DETAIL_FIELD_LABELS.items()
        }
    return _FORM_LABEL_LOOKUP


def _extract_sheet_id(sheet_url: str) -> str:
    match = SHEET_URL_RE.search(sheet_url)
    if not match:
        raise GoogleSheetImportError("Unable to determine spreadsheet identifier from the provided link.")
    return match.group(1)


def _rows_to_mapping(rows: List[List[Any]]) -> Dict[str, Any]:
    if not rows:
        return {}

    mapping: Dict[str, Any] = {}
    header = rows[0]
    if len(rows) >= 2 and len(header) >= 2:
        data_row = rows[1]
        header_mapping: Dict[str, Any] = {}
        for index, raw_key in enumerate(header):
            key = str(raw_key or "").strip()
            if not key:
                continue
            value = data_row[index] if index < len(data_row) else ""
            header_mapping[key] = value
        if header_mapping:
            return header_mapping

    for row in rows:
        if not row:
            continue
        key = str(row[0] or "").strip()
        if not key:
            continue
        value = row[1] if len(row) > 1 else ""
        mapping[key] = value
    return mapping


def _build_enquiry_payload_from_sheet(user: Any, sheet_url: str) -> Dict[str, Any]:
    spreadsheet_id = _extract_sheet_id(sheet_url)
    gmail_service = GmailService(user)
    credentials = gmail_service.build_authorized_credentials()
    sheets_service = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range="Sheet1!A1:Z200")
        .execute()
    )
    rows = result.get("values", [])
    sheet_mapping = _rows_to_mapping(rows)
    if not sheet_mapping:
        raise GoogleSheetImportError("No readable data was found in the Google Sheet.")

    normalized_data: Dict[str, Any] = {}
    field_lookup = _form_field_lookup()
    label_lookup = _form_label_lookup()
    for raw_key, raw_value in sheet_mapping.items():
        normalized_name = _normalise_field_name(raw_key)
        field_name = field_lookup.get(normalized_name) or label_lookup.get(normalized_name)
        if field_name:
            normalized_data[field_name] = raw_value

    if not normalized_data:
        raise GoogleSheetImportError("The sheet does not contain any recognised enquiry fields.")

    normalized_data.setdefault("remarks", f"Imported via Google Sheet ({sheet_url})")
    normalized_data.setdefault("description", normalized_data.get("remarks"))

    form = forms.EnquiryCreateForm(normalized_data)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))
        message = first_error[0] if first_error else "Unknown validation error."
        raise GoogleSheetImportError(f"Unable to import sheet: {message}")

    return form.build_payload()


def _normalise_column_entry(entry: Any, group: str, index: int) -> Dict[str, Any]:
    prefix = "D" if group == "die" else "M"
    data: Dict[str, Any]
    if isinstance(entry, Mapping):
        data = dict(entry)
    else:
        try:
            data = dict(entry)
        except Exception:
            data = {}

    code = str(data.get("code") or "").strip()
    if not code:
        code = f"{prefix}{index + 1}"
    title = str(data.get("title") or "").strip() or code

    values_raw = data.get("values")
    if isinstance(values_raw, Mapping):
        values = {str(key): values_raw[key] for key in values_raw.keys()}
    else:
        values = {}

    image_ids_raw = data.get("image_ids")
    if isinstance(image_ids_raw, (list, tuple)):
        image_ids = [str(item) for item in image_ids_raw if item]
    else:
        image_ids = []

    return {
        "code": code,
        "title": title,
        "values": values,
        "image_ids": image_ids,
        "group": group,
    }


def _build_column_matrix_display(enquiry_data: Mapping[str, Any]) -> Dict[str, Any]:
    columns: List[Dict[str, Any]] = []
    for group, key in (("die", "extrusion_die_columns"), ("mold", "mold_columns")):
        entries = enquiry_data.get(key) or []
        for index, entry in enumerate(entries):
            columns.append(_normalise_column_entry(entry, group, index))

    rows: List[Dict[str, Any]] = []
    for row_def in COLUMN_MATRIX_ROWS:
        applies_to = set(row_def.get("applies_to") or ())
        row_type = row_def.get("type", "text")
        cells: List[Dict[str, Any]] = []
        for column in columns:
            enabled = not applies_to or column["group"] in applies_to
            value = column["values"].get(row_def["key"]) if enabled else None
            cells.append({"enabled": enabled, "value": value})
        rows.append({"label": row_def["label"], "type": row_type, "cells": cells})

    return {"columns": columns, "rows": rows}


def _format_preview_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        decimal_value = Decimal(str(value))
        normalized = decimal_value.normalize()
        text = format(normalized, "f").rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _build_preview_field_entries(
    form: django_forms.Form,
    data: Dict[str, Any],
    *,
    hidden_names: Iterable[str] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    hidden_set = set(hidden_names)
    field_entries: List[Dict[str, Any]] = []
    additional_values: Dict[str, str] = {}

    for name, field in form.fields.items():
        if name in PREVIEW_SKIP_FIELDS:
            continue

        raw_value = data.get(name, form.initial.get(name, field.initial))

        if isinstance(field.widget, django_forms.widgets.HiddenInput) or name in hidden_set:
            additional_values[name] = _format_preview_value(raw_value)
            continue

        entry: Dict[str, Any] = {
            "name": name,
            "label": field.label or DETAIL_FIELD_LABELS.get(name, name.replace("_", " ").title()),
        }

        widget = field.widget

        # Capture per-field errors if present on the bound form
        errors_for_field = []
        try:
            if getattr(form, "errors", None) and name in form.errors:
                # form.errors[name] is an ErrorList
                errors_for_field = [str(e) for e in form.errors.getlist(name)]
        except Exception:
            errors_for_field = []

        if isinstance(widget, django_forms.Textarea) or name in TEXTAREA_FIELDS:
            entry["type"] = "textarea"
            entry["rows"] = widget.attrs.get("rows", 3)
            entry["value"] = _format_preview_value(raw_value)
            entry["input_class"] = "form-control"
        elif isinstance(widget, django_forms.Select):
            entry["type"] = "select"
            entry["choices"] = list(field.choices)
            entry["value"] = "" if raw_value in (None, "") else str(raw_value)
            entry["input_class"] = "form-select"
        elif isinstance(widget, django_forms.CheckboxInput) or isinstance(field, django_forms.BooleanField):
            entry["type"] = "checkbox"
            entry["checked"] = bool(raw_value)
            entry["value"] = widget.attrs.get("value", "on")
            entry["input_class"] = "form-check-input"
        elif isinstance(field, django_forms.DecimalField):
            entry["type"] = "number"
            entry["step"] = widget.attrs.get("step") or "any"
            entry["value"] = _format_preview_value(raw_value)
            entry["input_class"] = "form-control"
        elif isinstance(field, django_forms.IntegerField):
            entry["type"] = "number"
            entry["step"] = widget.attrs.get("step") or "1"
            entry["value"] = _format_preview_value(raw_value)
            entry["input_class"] = "form-control"
        else:
            # Respect explicit widget 'type' attribute (e.g., date) if present
            explicit_type = widget.attrs.get("type")
            entry["type"] = explicit_type or getattr(widget, "input_type", None) or "text"
            entry["value"] = _format_preview_value(raw_value)
            entry["input_class"] = "form-control"

        entry["column_class"] = FIELD_LAYOUT_OVERRIDES.get(
            name,
            "col-12" if entry["type"] == "textarea" else "col-12 col-md-6 col-xl-4",
        )

        if field.help_text:
            entry["help_text"] = field.help_text
        entry["required"] = field.required

        excluded_attrs = {"class", "type"}
        if entry["type"] == "textarea":
            excluded_attrs.add("rows")
        if entry["type"] == "number":
            excluded_attrs.add("step")

        extra_attrs: Dict[str, Any] = {}
        for attr_name, attr_value in widget.attrs.items():
            if attr_name in excluded_attrs:
                continue
            extra_attrs[attr_name] = attr_value

        entry["attrs"] = extra_attrs

        if errors_for_field:
            entry["errors"] = errors_for_field
            # Append Bootstrap invalid class for visual highlight on inputs
            try:
                entry["input_class"] = f"{entry['input_class']} is-invalid"
            except Exception:
                pass

        field_entries.append(entry)

    return field_entries, additional_values


def _get_api_root() -> str:
    return getattr(settings, "ENQUIRY_API_ROOT", "http://localhost:8405").rstrip("/")


def _financial_year_bounds(fin_year_label: str) -> tuple:
    """Return (start_date, end_date) for an Indian financial year label like '2024-25'.

    Indian FY runs April 1 to March 31. Label '2024-25' means 1 April 2024 – 31 March 2025.
    """
    parts = fin_year_label.strip().split("-")
    if len(parts) != 2:
        raise ValueError("Invalid financial year format.")
    start_year = int(parts[0])
    end_year = int(parts[1])
    if end_year != (start_year + 1) % 100 and end_year != start_year + 1:
        raise ValueError("Invalid financial year range.")
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def _financial_year_label_for_date(value) -> str:
    """Return the Indian financial year label for a date/datetime value."""
    d = value.date() if hasattr(value, "date") else value
    if d.month >= 4:
        return f"{d.year}-{str(d.year + 1)[2:]}"
    return f"{d.year - 1}-{str(d.year)[2:]}"


def _generate_financial_year_options(dates: list) -> list:
    """Generate distinct financial year labels covering the given dates."""
    labels = set()
    for value in dates:
        if value is None:
            continue
        try:
            d = value.date() if hasattr(value, "date") else value
        except AttributeError:
            continue
        labels.add(_financial_year_label_for_date(d))
    return sorted(labels)


# Process: open (enquiry, feasibility, quotation, PI) -> accepted (order accepted, payment pending)
# -> converted (payment received). Accepted orders only get "Payment pending" follow-ups.
FOLLOW_UP_CLOSED_STATUSES = {"closed", "converted", "rejected"}


def _payment_status(enquiry_id: str) -> str:
    """Payment status of an accepted order: pending (default), partial or paid."""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("payment_status").first()
    return (contact.payment_status if contact else "") or EnquiryContact.PAYMENT_PENDING


def _conversion_block_reason(enquiry_id: str, old_status: Any, new_status: Any) -> str:
    """An order becomes Converted only when its payment is recorded as Paid (Mark as paid on the board)."""
    old_status = str(old_status or "").strip().lower()
    new_status = str(new_status or "").strip().lower()
    if new_status != CONVERTED_STATUS or old_status == CONVERTED_STATUS:
        return ""
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("payment_status").first()
    if contact and contact.payment_status == EnquiryContact.PAYMENT_PAID:
        return ""
    return (
        "An order becomes Converted only when the payment is received. Set the status to Accepted, "
        "then use “Mark as paid” on the enquiries board."
    )


def _payment_follow_up_count(enquiry_id: str) -> int:
    return FollowUpSent.objects.filter(
        enquiry_id=str(enquiry_id), status=FollowUpSent.STATUS_SENT, stage=PAYMENT_STAGE
    ).count()


def _follow_up_summaries(enquiry_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Per enquiry: where its follow-ups stand (current stage only), the last email, and when the next is due.

    A quotation or proforma invoice emailed with "Send to customer" counts from that moment: the board shows
    the first follow-up as due 2 days later, before any follow-up has been sent. Marketing's own emails
    (after a rejection) are not shown here; the Marketing portal shows those.
    """
    from . import auto_follow_up

    today = timezone.localdate()
    summaries: Dict[str, Dict[str, Any]] = {}
    ids = [str(i) for i in enquiry_ids]
    for enquiry_id, cycle in auto_follow_up.cycles(ids, include_marketing=False).items():
        latest, start = cycle.latest, cycle.start
        sender = latest.sent_by if latest is not None else (start.started_by if start is not None else None)
        due = cycle.next_on
        summaries[enquiry_id] = {
            "count": cycle.count,
            "last_sent_at": cycle.last_sent_at,
            "last_sent_by": (sender.get_full_name() or sender.username) if sender else "",
            "due": due,
            "due_state": None if due is None else ("overdue" if due < today else "today" if due == today else "later"),
            "last_auto": bool(latest is not None and latest.source == FollowUpSent.SOURCE_AUTO),
            "last_stage": cycle.stage,
            "last_cc": cycle.cc,
            # "Quotation sent" / "Proforma invoice sent" while no follow-up has gone out yet.
            "started_with": ((dict(FOLLOW_UP_STAGE_CHOICES).get(cycle.stage) or "Sent")
                             if cycle.count == 0 else ""),
        }
    return summaries


def _follow_up_stopped_reason(enquiry: Mapping[str, Any], sent_count: int) -> str:
    """Why no more follow-ups should be sent for this enquiry, or '' if one is fine.

    For an accepted order only payment follow-ups count, and they stop once it is paid.
    """
    status = str(enquiry.get("status") or "").strip().lower()
    if status == "accepted":
        enquiry_id = str(enquiry.get("enquiry_id") or "")
        if _payment_status(enquiry_id) == EnquiryContact.PAYMENT_PAID:
            return f"Payment for order #{enquiry_id} has been received, so no follow-up is needed."
        sent_count = _payment_follow_up_count(enquiry_id)
    elif status in FOLLOW_UP_CLOSED_STATUSES:
        return f"Enquiry #{enquiry.get('enquiry_id')} is {status}, so no follow-up is needed."
    limit = int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
    if limit and sent_count >= limit:
        return (
            f"Enquiry #{enquiry.get('enquiry_id')} has already been followed up {sent_count} times. "
            "Please call the customer or update the enquiry status instead."
        )
    return ""


def _save_customer_email(enquiry_id: Optional[str], email: str, user) -> None:
    """Remember the customer email typed when creating an enquiry (stored in MagicWand's own database)."""
    if not enquiry_id or not email:
        return
    EnquiryContact.objects.update_or_create(
        enquiry_id=str(enquiry_id),
        defaults={"customer_email": email, "updated_by": user if getattr(user, "is_authenticated", False) else None},
    )


@require_POST
def toggle_auto_follow_up(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    """Pause or resume automatic follow-ups for one enquiry (button on the board)."""
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    contact.auto_follow_up_paused = not contact.auto_follow_up_paused
    contact.updated_by = request.user
    contact.save(update_fields=["auto_follow_up_paused", "updated_by", "updated_at"])
    state = "paused" if contact.auto_follow_up_paused else "resumed"
    messages.success(request, f"Automatic follow-ups {state} for enquiry #{enquiry_id}.")
    next_url = request.POST.get("next") or ""
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = reverse("enquiries:board")
    return redirect(next_url)


def _board_update_customer(request: HttpRequest, api_root: str, enquiry_id: str) -> None:
    """Set the customer name (enquiry service) and email (MagicWand database) from the board."""
    name = " ".join((request.POST.get("customer_name") or "").split())[:255]
    email = (request.POST.get("customer_email") or "").strip().lower()
    try:
        kyc_customer = _kyc_customer_from_post(request)
    except ValueError as exc:
        messages.error(request, str(exc))
        return
    if kyc_customer is not None:  # a picked customer code fills in what was left empty
        name = name or kyc_customer.name[:255]
        email = email or kyc_links.contact_email(kyc_customer)
    if not name:
        messages.error(request, "Please enter the customer name.")
        return
    if email and not _is_valid_email(email):
        messages.error(request, "Please enter a valid customer email address.")
        return
    # Only the Edit customer form sends the other-email boxes; "Link to code" keeps what was saved.
    edit_others = "other_customer_emails_sent" in request.POST
    others, others_error = customer_emails.parse(request.POST.getlist("other_customer_email"), main=email)
    if others_error:
        messages.error(request, others_error)
        return
    if others and not email:
        email, others = others[0], others[1:]  # the first address given becomes the main contact
    try:
        services.update_enquiry(api_root, enquiry_id, {"customer_name": name}, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to update the customer: {exc}")
        return
    if "customer_code" in request.POST:  # an emptied code removes the link
        kyc_links.set_link(enquiry_id, kyc_customer, request.user)
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first()
    if email or (contact and contact.customer_email):
        EnquiryContact.objects.update_or_create(
            enquiry_id=str(enquiry_id),
            defaults={"customer_email": email,
                      "updated_by": request.user if getattr(request.user, "is_authenticated", False) else None},
        )
    if edit_others:
        customer_emails.save_others(enquiry_id, others, request.user)
    detail = f" ({', '.join([email] + others)})" if email else ""
    if kyc_customer is not None:
        detail += f", customer code {kyc_customer.code}"
    messages.success(request, f"Customer for enquiry #{enquiry_id} set to {name}{detail}.")


def _kyc_customer_from_post(request: HttpRequest):
    """The KYC customer whose code was picked on the form; None when no code was given."""
    code = (request.POST.get("customer_code") or "").strip()
    if not code:
        return None
    customer = kyc_links.customer_for_code(code)
    if customer is None:
        raise ValueError(f"Customer code {kyc_links.normalize_code(code)} is not in KYC. "
                         "Pick it from the list, or leave the code empty.")
    return customer


def _saved_customer_email(enquiry_id: str) -> str:
    contact = EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).only("customer_email").first()
    return (contact.customer_email if contact else "") or ""


def _attach_order_tracking(enquiry: MutableMapping[str, Any], contact: Optional[EnquiryContact],
                           emails: Mapping[str, CustomerStatusEmail]) -> None:
    """Payment (accepted / converted orders) and the automatic rejection email, for the board."""
    status = str(enquiry.get("status") or "").strip().lower()
    if status in ("accepted", "converted"):
        # Converted means the payment was received.
        due = payment_terms.credit_due_date(contact)
        payment = EnquiryContact.PAYMENT_PAID if status == "converted" else (
            (contact.payment_status if contact else "") or EnquiryContact.PAYMENT_PENDING)
        enquiry["payment"] = {
            "status": payment,
            "label": dict(EnquiryContact.PAYMENT_STATUS_CHOICES)[payment],
            # Only the status is shown. The amount received, the entered due date, the paid-on date and the
            # reference are confidential and are not sent to the page. "due" is only the end of a credit period
            # (worked out from the credit days and start date), which the follow-up chip needs.
            "due": due if payment == EnquiryContact.PAYMENT_CREDIT else None,
            "overdue": bool(due and payment != EnquiryContact.PAYMENT_PAID and due < timezone.localdate()),
            "credit_days": contact.credit_period_days if contact else None,
            "credit_starts_on": contact.credit_starts_on if contact else None,
            "in_credit": bool(payment == EnquiryContact.PAYMENT_CREDIT and due and due > timezone.localdate()),
        }
    else:
        enquiry["payment"] = None
    shown = []
    for kind, label in _STATUS_EMAIL_LABELS:
        row = emails.get(kind)
        if row is None:
            continue
        can_retry = row.status != CustomerStatusEmail.STATUS_SENT and _status_email_allowed(kind, status) == ""
        # A failed email is only worth showing while it still applies (e.g. rejection while rejected).
        if row.status == CustomerStatusEmail.STATUS_SENT or can_retry:
            shown.append({"kind": kind, "label": label, "status": row.status, "note": row.note,
                          "when": row.sent_at or row.updated_at, "can_retry": can_retry})
    enquiry["status_emails"] = shown


def _board_update_payment(request: HttpRequest, api_root: str, enquiry_id: str) -> None:
    value = (request.POST.get("payment_status") or "").strip()
    if value not in {key for key, _ in EnquiryContact.PAYMENT_STATUS_CHOICES}:
        messages.error(request, "Unknown payment status.")
        return
    due = None
    # "Credit period" in the Payment list: payment is due N days after the invoice / dispatch date.
    credit_days, credit_start = None, None
    if value == EnquiryContact.PAYMENT_CREDIT:
        credit_raw = (request.POST.get("credit_period_days") or "").strip()
        if not credit_raw.isdigit() or not 1 <= int(credit_raw) <= payment_terms.MAX_CREDIT_DAYS:
            messages.error(request, f"Credit period must be a number of days between 1 and {payment_terms.MAX_CREDIT_DAYS}.")
            return
        credit_days = int(credit_raw)
        credit_start_raw = (request.POST.get("credit_starts_on") or "").strip()
        credit_start = parse_date(credit_start_raw) if credit_start_raw else timezone.localdate()
        if credit_start is None:
            messages.error(request, "Please enter a valid credit start date (invoice / dispatch date).")
            return
        due = credit_start + timedelta(days=credit_days)  # the credit period sets the due date
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry: {exc}")
        return
    status = str(enquiry.get("status") or "").strip().lower()
    if status != "accepted":
        messages.error(request, f"Payment can only be updated for accepted orders (enquiry #{enquiry_id} is {status or 'not accepted'}).")
        return
    if value == EnquiryContact.PAYMENT_PAID:
        # Paid: the order is converted.
        try:
            services.update_enquiry(api_root, enquiry_id, {"status": CONVERTED_STATUS}, request=request)
        except services.EnquiryServiceError as exc:
            messages.error(request, f"Payment not saved: could not mark the order as converted ({exc}).")
            return
        conversions = request.session.get(CONVERSION_SESSION_KEY, {})
        if not isinstance(conversions, dict):
            conversions = {}
        conversions[enquiry_id] = "accepted"
        request.session[CONVERSION_SESSION_KEY] = conversions
        request.session.modified = True
    # The confidential amount, due date, paid-on date and reference already stored are left exactly as they are.
    _save_payment(enquiry_id, value, request.user, due=due, credit_days=credit_days, credit_start=credit_start,
                  leave_confidential=True)
    label = dict(EnquiryContact.PAYMENT_STATUS_CHOICES)[value]
    if value == EnquiryContact.PAYMENT_PAID:
        messages.success(request, f"Order #{enquiry_id}: {label}. The status is now Converted.")
        outcome = status_emails.send_status_email(CustomerStatusEmail.KIND_PAYMENT_RECEIVED, enquiry_id, enquiry)
        _report_status_email(request, outcome, "payment received")
    elif credit_days:
        messages.success(request, f"Order #{enquiry_id}: {label} of {credit_days} days. Payment is due on "
                                  f"{due:%d %b %Y}" + (", and payment reminders wait until then."
                                                       if due > timezone.localdate() else "."))
    else:
        messages.success(request, f"Order #{enquiry_id}: {label}.")


def _save_payment(enquiry_id: str, value: str, user, *, amount=None, due=None, paid_on=None, reference="",
                  credit_days=None, credit_start=None, keep_details: bool = False,
                  leave_confidential: bool = False) -> None:
    """Save the payment status. leave_confidential keeps the stored amount, paid-on date and reference as they are
    (the form no longer has them); the due date is only set from a credit period."""
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    contact.payment_status = value
    if leave_confidential:
        if due is not None:
            contact.payment_due_on = due
        contact.credit_period_days = credit_days
        contact.credit_starts_on = credit_start
    elif not keep_details:
        contact.payment_amount_received = amount
        contact.payment_due_on = due
        contact.payment_paid_on = paid_on
        contact.payment_reference = reference
        contact.credit_period_days = credit_days
        contact.credit_starts_on = credit_start
    elif value != EnquiryContact.PAYMENT_PAID:
        contact.payment_paid_on = None
    contact.payment_updated_by = user if getattr(user, "is_authenticated", False) else None
    contact.payment_updated_at = timezone.now()
    contact.save()


_STATUS_EMAIL_LABELS = (
    (CustomerStatusEmail.KIND_ACKNOWLEDGEMENT, "Acknowledgement"),
    (CustomerStatusEmail.KIND_REJECTION, "Reason email"),
    (CustomerStatusEmail.KIND_PAYMENT_RECEIVED, "Payment thank-you"),
    (CustomerStatusEmail.KIND_CLOSING, "Closing email"),
)
# The enquiry status each automatic email belongs to.
_STATUS_EMAIL_STATUS = {
    CustomerStatusEmail.KIND_ACKNOWLEDGEMENT: "open",
    CustomerStatusEmail.KIND_REJECTION: "rejected",
    CustomerStatusEmail.KIND_PAYMENT_RECEIVED: CONVERTED_STATUS,
    CustomerStatusEmail.KIND_CLOSING: "open",
}


def _status_email_allowed(kind: str, status: str) -> str:
    """'' if this automatic email may be (re)sent for an enquiry with this status, else why not."""
    wanted = _STATUS_EMAIL_STATUS.get(kind)
    if wanted is None:
        return "Unknown email type."
    if status != wanted:
        return f"This email is only sent while the enquiry is {wanted}."
    return ""


def _board_retry_status_email(request: HttpRequest, api_root: str, enquiry_id: str) -> None:
    kind = (request.POST.get("kind") or "").strip()
    if kind not in _STATUS_EMAIL_STATUS:
        messages.error(request, "Unknown email type.")
        return
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry: {exc}")
        return
    status = str(enquiry.get("status") or "").strip().lower()
    blocked = _status_email_allowed(kind, status)
    if blocked:
        messages.error(request, f"Enquiry #{enquiry_id}: {blocked}")
        return
    label = dict(_STATUS_EMAIL_LABELS)[kind].lower()
    if kind == CustomerStatusEmail.KIND_CLOSING:
        outcome = status_emails.send_closing_email(enquiry, api_root)
    elif kind == CustomerStatusEmail.KIND_PAYMENT_RECEIVED:
        contact = EnquiryContact.objects.filter(enquiry_id=enquiry_id).first()
        outcome = status_emails.send_status_email(kind, enquiry_id, enquiry)  # no payment reference or date in the email
    else:
        outcome = status_emails.send_status_email(kind, enquiry_id, enquiry,
                                                  reason=_share_reason(enquiry, kind=kind) if kind in _SHARE_REASON_MARKERS else "")
    if outcome.get("result") == "already_sent":
        messages.info(request, f"The {label} email was already sent for enquiry #{enquiry_id}.")
    else:
        _report_status_email(request, outcome, label)


# Board filter "Document": the choice, its label, and words that identify the document in the attached file name.
DOCUMENT_FILTERS = {
    "proforma": ("PROFORMA INVOICE", ("proforma",)),
    "feasibility": ("Feasibility & Quotation Report", ("feasibility", "quotation")),
}


def _has_document(enquiry: Mapping[str, Any], words: tuple) -> bool:
    """True when one of the enquiry's attached files has one of the words in its name (case ignored)."""
    for attachment in enquiry.get("general_attachments") or enquiry.get("attachments") or []:
        name = str((attachment or {}).get("file_name") if isinstance(attachment, Mapping) else "").lower()
        if any(word in name for word in words):
            return True
    return False

# Words in the report's own title (checked inside the PDF). A PDF is decided by its text when it can be read;
# the file name is only used for files whose text can't be read.
DOCUMENT_TITLE_PHRASES = {
    "proforma": ("proforma invoice",),
    "feasibility": ("feasibility & quotation report",),
}
DOCUMENT_TEXT_CACHE_SECONDS = 60 * 60 * 24 * 7


def _pdf_first_pages_text(content: bytes, pages: int = 2) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(pages, len(reader.pages))))
    return " ".join(text.lower().split())


def _attachment_text(enquiry_id: str, attachment: Mapping[str, Any], api_root: str, request) -> Optional[str]:
    """Lower-case text of the first pages of an attached PDF, cached per attachment; None if it can't be read."""
    from django.core.cache import cache

    attachment_id = str(attachment.get("attachment_id") or "")
    if not attachment_id:
        return None
    key = f"enquiry-document-text:{attachment_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    download_path = str(attachment.get("download_url") or "") or f"/enquiries/{enquiry_id}/attachments/{attachment_id}"
    try:
        content, _filename, _content_type = services.download_attachment(api_root, download_path, request=request)
        text = _pdf_first_pages_text(content)
    except Exception:
        return None
    cache.set(key, text, DOCUMENT_TEXT_CACHE_SECONDS)
    return text


def _may_be_pdf(attachment: Mapping[str, Any]) -> bool:
    name = str(attachment.get("file_name") or "").lower()
    content_type = str(attachment.get("content_type") or "").lower()
    return "pdf" in content_type or name.endswith(".pdf") or "." not in name


def _has_document_by_content(enquiry: Mapping[str, Any], key: str, api_root: str, request) -> bool:
    """True when an attached PDF's title (its text) has the document's phrase, or a non-PDF file's name has its word."""
    enquiry_id = str(enquiry.get("enquiry_id") or "")
    phrases = DOCUMENT_TITLE_PHRASES[key]
    words = DOCUMENT_FILTERS[key][1]
    for attachment in enquiry.get("general_attachments") or enquiry.get("attachments") or []:
        if not isinstance(attachment, Mapping):
            continue
        if _may_be_pdf(attachment):
            text = _attachment_text(enquiry_id, attachment, api_root, request)
            if text is not None:
                if any(phrase in text for phrase in phrases):
                    return True
                continue  # readable PDF with a different title: its file name does not count
        name = str(attachment.get("file_name") or "").lower()
        if any(word in name for word in words):
            return True
    return False


@login_required
def board(request: HttpRequest) -> HttpResponse:
    api_root = _get_api_root()

    if request.method == "POST":
        action = request.POST.get("action") or ""
        if action == "assign_enquiry":
            enquiry_id = (request.POST.get("enquiry_id") or "").strip()
            assigned_to = (request.POST.get("assigned_to") or "").strip()
            if not enquiry_id:
                messages.error(request, "Missing enquiry identifier.")
            else:
                payload = {"assigned_to": assigned_to or None}
                try:
                    services.update_enquiry(api_root, enquiry_id, payload, request=request)
                except services.EnquiryServiceError as exc:
                    messages.error(request, f"Unable to update assignment: {exc}")
                else:
                    if assigned_to:
                        messages.success(request, f"Assigned enquiry {enquiry_id} to {assigned_to}.")
                    else:
                        messages.success(request, f"Cleared assignment for enquiry {enquiry_id}.")
        elif action == "update_subject":
            enquiry_id = (request.POST.get("enquiry_id") or "").strip()
            subject = (request.POST.get("subject") or "").strip()
            if not enquiry_id:
                messages.error(request, "Missing enquiry identifier.")
            else:
                payload = {"subject": subject or None}
                try:
                    services.update_enquiry(api_root, enquiry_id, payload, request=request)
                except services.EnquiryServiceError as exc:
                    messages.error(request, f"Unable to update subject: {exc}")
                else:
                    messages.success(request, f"Updated subject for enquiry {enquiry_id}.")
        elif action == "update_customer":
            enquiry_id = (request.POST.get("enquiry_id") or "").strip()
            if not enquiry_id:
                messages.error(request, "Missing enquiry identifier.")
            else:
                _board_update_customer(request, api_root, enquiry_id)
        elif action in {"update_payment", "retry_status_email"}:
            enquiry_id = (request.POST.get("enquiry_id") or "").strip()
            if not enquiry_id:
                messages.error(request, "Missing enquiry identifier.")
            elif action == "update_payment":
                _board_update_payment(request, api_root, enquiry_id)
            else:
                _board_retry_status_email(request, api_root, enquiry_id)
        else:
            messages.error(request, "Unknown action requested.")
        next_url = request.POST.get("next") or ""
        if next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect("enquiries:board")

    query_raw = (request.GET.get("q") or "").strip()
    query = query_raw.lower()
    fin_year = (request.GET.get("fin_year") or "").strip()
    month = (request.GET.get("month") or "").strip()
    status = (request.GET.get("status") or "").strip()

    if status and status not in STATUS_VALUE_SET:
        status = ""
    payment_filter = (request.GET.get("payment") or "").strip()
    if payment_filter not in {"unpaid", "paid"}:
        payment_filter = ""
    document_filter = (request.GET.get("document") or "").strip()
    if document_filter not in DOCUMENT_FILTERS:
        document_filter = ""

    page_size_options = [25, 50, 100]
    try:
        page_size = int(request.GET.get("per_page") or 50)
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in page_size_options:
        page_size = 50

    try:
        page = int(request.GET.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    try:
        enquiries = services.list_enquiries(api_root, request=request)
    except services.EnquiryServiceError as exc:
        enquiries = []
        messages.error(request, f"Unable to load enquiries: {exc}")
    else:
        for enquiry in enquiries:
            _attach_primary_pdf_link(enquiry)

    all_dates = [enquiry.get("received_at_dt") for enquiry in enquiries if enquiry.get("received_at_dt")]
    fin_years = _generate_financial_year_options(all_dates)

    if query:
        filtered = []
        for enquiry in enquiries:
            haystack = " ".join(
                str(value or "")
                for key, value in enquiry.items()
                if isinstance(value, (str, int, float)) and key not in {"sheet_url", "primary_pdf_url"}
            ).lower()
            if query in haystack:
                filtered.append(enquiry)
        enquiries = filtered

    # Status filter
    if status:
        enquiries = [
            enquiry for enquiry in enquiries
            if str(enquiry.get("status") or "").strip().lower() == status
        ]

    # Payment filter: accepted orders still waiting for payment ("unpaid"), or paid / converted ones ("paid").
    if payment_filter:
        def _status_of(e):
            return str(e.get("status") or "").strip().lower()

        accepted_ids = [str(e.get("enquiry_id")) for e in enquiries if _status_of(e) == "accepted"]
        paid_ids = set(
            EnquiryContact.objects.filter(enquiry_id__in=accepted_ids, payment_status=EnquiryContact.PAYMENT_PAID)
            .values_list("enquiry_id", flat=True)
        )
        if payment_filter == "unpaid":
            wanted = set(accepted_ids) - paid_ids
        else:
            wanted = paid_ids | {str(e.get("enquiry_id")) for e in enquiries if _status_of(e) == "converted"}
        enquiries = [e for e in enquiries if str(e.get("enquiry_id")) in wanted]

    # Document filter: only the enquiries with that document attached (matched on the attached file's name).
    if document_filter:
        enquiries = [e for e in enquiries if _has_document_by_content(e, document_filter, api_root, request)]

    # Financial year + month filter
    if fin_year or month:
        try:
            fin_start, fin_end = _financial_year_bounds(fin_year) if fin_year else (None, None)
        except ValueError:
            fin_start, fin_end = None, None

        month_num = None
        if month:
            try:
                month_num = int(month)
                if month_num < 1 or month_num > 12:
                    month_num = None
            except (TypeError, ValueError):
                month_num = None

        filtered_by_date = []
        for enquiry in enquiries:
            received_at = enquiry.get("received_at_dt")
            if received_at is None:
                continue
            received_date = received_at.date() if hasattr(received_at, "date") else received_at

            if fin_start and fin_end:
                if not (fin_start <= received_date <= fin_end):
                    continue
            if month_num is not None:
                if received_date.month != month_num:
                    continue

            filtered_by_date.append(enquiry)
        enquiries = filtered_by_date

    conversion_store = request.session.get(CONVERSION_SESSION_KEY, {})
    if not isinstance(conversion_store, dict):
        conversion_store = {}
    for enquiry in enquiries:
        enquiry_id = enquiry.get("enquiry_id")
        if enquiry_id and enquiry_id in conversion_store:
            enquiry["converted_previous_status"] = conversion_store[enquiry_id]
        if str(enquiry.get("status") or "").strip().lower() == "rejected":
            enquiry["rejection_reason"] = _extract_rejection_reason(enquiry.get("remarks"))
        else:
            # The customer's old rejection comment stays in the remarks, but once the quotation is accepted
            # (or reopened) the board no longer offers "View comments".
            enquiry.pop("rejection_reason", None)

    total_count = len(enquiries)
    total_pages = max(1, int(math.ceil(total_count / page_size))) if page_size else 1
    if page > total_pages:
        page = total_pages

    start_index = (page - 1) * page_size
    end_index = min(total_count, start_index + page_size)
    enquiries_page = enquiries[start_index:end_index]

    # Follow-up history and next-due date per enquiry on this page (shown under the Follow-up button).
    page_ids = [str(e.get("enquiry_id")) for e in enquiries_page if e.get("enquiry_id")]
    if page_ids:
        summaries = _follow_up_summaries(page_ids)
        contacts = {c.enquiry_id: c for c in EnquiryContact.objects.filter(enquiry_id__in=page_ids)}
        paused_ids = {eid for eid, c in contacts.items() if c.auto_follow_up_paused}
        status_email_rows = status_emails.latest_status_emails(page_ids)
        kyc_customers = kyc_links.customers_for(page_ids)
        from quotations import links as quotation_links

        quotation_counts = quotation_links.counts_for_enquiries(page_ids)
        kyc_suggestions = kyc_links.suggestions_for(
            e.get("customer_name") for e in enquiries_page if str(e.get("enquiry_id")) not in kyc_customers)
        for enquiry in enquiries_page:
            enquiry["auto_paused"] = str(enquiry.get("enquiry_id")) in paused_ids
            contact = contacts.get(str(enquiry.get("enquiry_id")))
            enquiry["kyc_customer"] = kyc_customers.get(str(enquiry.get("enquiry_id")))
            enquiry["quotation_count"] = quotation_counts.get(str(enquiry.get("enquiry_id")), 0)
            if enquiry["kyc_customer"] is None:  # same company name as exactly one KYC customer: offer the link
                enquiry["kyc_suggestion"] = kyc_suggestions.get(
                    " ".join(str(enquiry.get("customer_name") or "").split()).lower())
            enquiry["customer_email_saved"] = ((contact.customer_email if contact else "")
                                               or str(enquiry.get("customer_email") or "").strip())
            enquiry["other_customer_emails_saved"] = customer_emails.split(
                contact.other_customer_emails if contact else "")
            _attach_order_tracking(enquiry, contacts.get(str(enquiry.get("enquiry_id"))),
                                   status_email_rows.get(str(enquiry.get("enquiry_id")), {}))
            summary = summaries.get(str(enquiry.get("enquiry_id")))
            # Why the Follow-up button cannot be used (closed/converted/rejected or limit reached), or ''.
            enquiry["follow_up_blocked"] = _follow_up_stopped_reason(enquiry, summary["count"] if summary else 0)
            if summary:
                limit = int(getattr(settings, "FOLLOW_UP_MAX_COUNT", 4) or 0)
                summary = {
                    **summary,
                    "limit": limit,
                    "steps": [n <= summary["count"] for n in range(1, max(limit, summary["count"]) + 1)],
                }
                status = str(enquiry.get("status") or "").strip().lower()
                if _follow_up_stopped_reason(enquiry, summary["count"]):
                    # closed/converted/rejected or limit reached: no more follow-ups are due
                    summary.update(due=None, due_state="closed" if status in FOLLOW_UP_CLOSED_STATUSES else "limit")
                    closing = (status_email_rows.get(str(enquiry.get("enquiry_id")), {})
                               .get(CustomerStatusEmail.KIND_CLOSING))
                    if closing is not None and closing.status == CustomerStatusEmail.STATUS_SENT:
                        summary["closed_no_reply"] = True  # all follow-ups unanswered: the closing email went out
                    elif (summary["due_state"] == "limit" and getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False)
                          and not enquiry["auto_paused"] and summary.get("last_sent_at")):
                        # All follow-ups are sent: the closing email follows automatically.
                        wait = int(getattr(settings, "FOLLOW_UP_CLOSE_AFTER_DAYS", 2) or 0)
                        summary.update(due_state="closing",
                                       closing_on=timezone.localdate(summary["last_sent_at"]) + timedelta(days=wait))
                elif status == "accepted" and (enquiry.get("payment") or {}).get("in_credit"):
                    # Inside the agreed credit period: no payment reminders until the due date.
                    summary.update(due=enquiry["payment"]["due"], due_state="credit")
                elif status == "accepted" and summary.get("last_stage") != PAYMENT_STAGE:
                    # Order accepted: earlier quote reminders no longer apply; payment follow-ups start manually.
                    summary.update(due=None, due_state="payment")
            enquiry["follow_up"] = summary

    page_start_display = start_index + 1 if total_count else 0
    page_end_display = end_index if total_count else 0

    base_params: Dict[str, Any] = {"per_page": page_size}
    if query_raw:
        base_params["q"] = query_raw
    if fin_year:
        base_params["fin_year"] = fin_year
    if month:
        base_params["month"] = month
    if status:
        base_params["status"] = status
    if payment_filter:
        base_params["payment"] = payment_filter
    if document_filter:
        base_params["document"] = document_filter

    prev_url = None
    if page > 1:
        params = dict(base_params)
        params["page"] = page - 1
        prev_url = f"{reverse('enquiries:board')}?{urlencode(params)}"

    next_url = None
    if page < total_pages:
        params = dict(base_params)
        params["page"] = page + 1
        next_url = f"{reverse('enquiries:board')}?{urlencode(params)}"

    context = {
        "api_root": api_root,
        "enquiries": enquiries_page,
        "enquiries_total_count": total_count,
        "enquiries_page": page,
        "enquiries_page_size": page_size,
        "enquiries_page_size_options": page_size_options,
        "enquiries_total_pages": total_pages,
        "enquiries_page_start_index": page_start_display,
        "enquiries_page_end_index": page_end_display,
        "enquiries_prev_url": prev_url,
        "enquiries_next_url": next_url,
        "search_query": query_raw,
        "fin_years": fin_years,
        "selected_fin_year": fin_year,
        "months": [
            (4, "April"), (5, "May"), (6, "June"), (7, "July"),
            (8, "August"), (9, "September"), (10, "October"),
            (11, "November"), (12, "December"), (1, "January"),
            (2, "February"), (3, "March"),
        ],
        "selected_month": month,
        "status_choices": STATUS_CHOICES,
        "selected_status": status,
        "selected_payment": payment_filter,
        "document_filter_choices": [(key, label) for key, (label, _words) in DOCUMENT_FILTERS.items()],
        "selected_document": document_filter,
        "payment_status_choices": EnquiryContact.PAYMENT_STATUS_CHOICES,
    }
    return render(request, "enquiries/board.html", context)


@login_required
def follow_up(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry: {exc}")
        return redirect("enquiries:board")

    # Step 9: stop following up closed/converted/rejected enquiries or after the limit.
    sent_count = FollowUpSent.objects.filter(enquiry_id=str(enquiry_id), status=FollowUpSent.STATUS_SENT).count()
    stopped = _follow_up_stopped_reason({**enquiry, "enquiry_id": enquiry_id}, sent_count)
    if stopped:
        messages.warning(request, stopped)
        return redirect("enquiries:board")

    # Customer email typed when the enquiry was created on the board (PDF / Google Sheet).
    if not str(enquiry.get("customer_email") or "").strip():
        saved_email = _saved_customer_email(enquiry_id)
        if saved_email:
            enquiry = {**enquiry, "customer_email": saved_email}

    chosen_thread = (request.GET.get("thread") or "").strip()
    if chosen_thread and not _GMAIL_ID_PATTERN.match(chosen_thread):
        messages.error(request, "Invalid Gmail conversation selected.")
        return redirect("enquiries:board")

    # Local development: make sure the sample "demo-" emails exist so follow-ups work without Gmail.
    from dashboard import views as dashboard_views

    dashboard_views._ensure_local_demo_emails(request.user)

    gmail_service = GmailService(request.user)
    try:
        outcome, result = _resolve_follow_up_conversation(enquiry_id, enquiry, gmail_service, chosen_thread)
    except GmailAPIError as exc:
        messages.error(request, f"Could not check Gmail for this enquiry: {exc}")
        return redirect("enquiries:board")

    if outcome == "error":
        messages.error(request, result)
        return redirect("enquiries:board")

    if outcome == "choose":
        return render(
            request,
            "enquiries/follow_up_select.html",
            {"enquiry": enquiry, "enquiry_id": enquiry_id, "conversations": result},
        )

    conversation = result
    customer_email = conversation.get("customer_email") or ""
    reply_to_id = str(conversation.get("latest_message_id") or "").strip()
    if not _is_valid_email(customer_email) or not reply_to_id:
        messages.error(
            request,
            "The customer's email address could not be identified in the Gmail conversation, so no follow-up was prepared.",
        )
        return redirect("enquiries:board")

    # Store follow-up context so the compose page can prefill, send to the customer and record history.
    request.session["follow_up_context"] = {
        "enquiry_id": enquiry_id,
        "customer_email": customer_email,
        "customer_name": str(enquiry.get("customer_name") or "").strip(),
        "subject": str(enquiry.get("subject") or "").strip(),
        "gmail_message_id": reply_to_id,
        "gmail_thread_id": conversation.get("thread_id") or "",
        "token": secrets.token_urlsafe(24),
        "stage": _suggested_follow_up_stage(enquiry_id, enquiry),
    }
    request.session.modified = True

    return redirect(reverse("dashboard:compose_email", args=[reply_to_id]))


def _start_candidates(enquiry_id: str, enquiry: Mapping[str, Any], gmail_service) -> List[Dict[str, Any]]:
    """Gmail conversations (in the signed-in mailbox) that may be this enquiry's quotation / proforma invoice."""
    subject = str(enquiry.get("subject") or "").strip()
    customer_name = str(enquiry.get("customer_name") or "").strip()
    saved_email = str(enquiry.get("customer_email") or "").strip() or _saved_customer_email(enquiry_id)
    saved_email = saved_email if _is_valid_email(saved_email) else ""
    found: List[Dict[str, Any]] = []
    if subject:
        found = gmail_service.find_enquiry_conversations(subject=subject, customer_email=saved_email,
                                                         customer_name=customer_name)
    if not found and saved_email:  # the email may carry its own subject (e.g. "Quotation Q-77"): try the customer alone
        found = gmail_service.find_enquiry_conversations(subject="", customer_email=saved_email)
    seen, out = set(), []
    for conversation in found:
        if conversation.get("thread_id") and conversation["thread_id"] not in seen and conversation.get("customer_email"):
            seen.add(conversation["thread_id"])
            out.append(conversation)
    return out


@login_required
def follow_up_start(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    """For a quotation or proforma invoice emailed from Gmail outside MagicWand: tell MagicWand which Gmail
    conversation it is and when it was sent, so the automatic follow-ups run in that conversation. Sends nothing."""
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry: {exc}")
        return redirect("enquiries:board")
    stopped = _follow_up_stopped_reason({**enquiry, "enquiry_id": enquiry_id}, 0)
    if stopped:
        messages.warning(request, stopped)
        return redirect("enquiries:board")

    from dashboard import views as dashboard_views
    dashboard_views._ensure_local_demo_emails(request.user)  # local development only: sample emails
    gmail_service = GmailService(request.user)
    try:
        conversations = _start_candidates(enquiry_id, enquiry, gmail_service)
    except GmailAPIError as exc:
        messages.error(request, f"Could not check Gmail for this enquiry: {exc}")
        return redirect("enquiries:board")

    today = timezone.localdate()
    context: Dict[str, Any] = {
        "enquiry": enquiry, "enquiry_id": enquiry_id, "conversations": conversations,
        "customer_email": (conversations[0].get("customer_email") if conversations else "")
        or _saved_customer_email(enquiry_id),
        "gmail_account": getattr(request.user, "email", "") or request.user.get_username(),
        "document_choices": [(key, item["label"]) for key, item in share_email.DOCUMENTS.items()],
        "document": "quotation", "today": today,
        "sent_on": (conversations[0].get("last_date").date().isoformat()
                    if len(conversations) == 1 and conversations[0].get("last_date") else today.isoformat()),
    }
    if request.method != "POST":
        return render(request, "enquiries/follow_up_start.html", context)

    thread_id = (request.POST.get("thread") or "").strip()
    document = request.POST.get("document") or ""
    context.update(selected_thread=thread_id, document=document if document in share_email.DOCUMENTS else "quotation",
                   sent_on=(request.POST.get("sent_on") or "").strip())
    conversation = next((c for c in conversations if c["thread_id"] == thread_id), None)
    sent_on = parse_date(context["sent_on"]) if context["sent_on"] else None
    error = ""
    if conversation is None:
        error = "Please pick the Gmail conversation for this enquiry."
    elif document not in share_email.DOCUMENTS:
        error = "Please choose whether a quotation or a proforma invoice was sent."
    elif sent_on is None:
        error = "Please enter the date the email was sent."
    elif sent_on > today:
        error = "The date the email was sent cannot be in the future."
    if error:
        context["error"] = error
        return render(request, "enquiries/follow_up_start.html", context)

    stage = share_email.DOCUMENTS[document]["stage"]
    already = FollowUpStart.objects.filter(enquiry_id=str(enquiry_id), stage=stage,
                                           gmail_thread_id=thread_id).order_by("-started_at").first()
    if already is not None:
        messages.info(request, f"Follow-ups for enquiry #{enquiry_id} are already running in this Gmail conversation.")
        return redirect("enquiries:board")
    FollowUpStart.objects.create(
        enquiry_id=str(enquiry_id), stage=stage, gmail_thread_id=thread_id,
        gmail_message_id=str(conversation.get("latest_message_id") or ""),
        customer_email=str(conversation.get("customer_email") or "").lower(), cc=payment_terms.default_cc(enquiry_id),
        subject=str(conversation.get("subject") or "")[:500], started_by=request.user,
        started_at=timezone.make_aware(datetime.combine(sent_on, datetime.min.time()).replace(hour=12)))
    _save_customer_email(enquiry_id, str(conversation.get("customer_email") or "").lower(), request.user)
    first = sent_on + timedelta(days=int((list(getattr(settings, "FOLLOW_UP_AUTO_GAP_DAYS", []) or [2]))[0]))
    label = share_email.DOCUMENTS[document]["label"].lower()
    messages.success(
        request,
        f"Follow-ups started for enquiry #{enquiry_id}: the {label} sent on {sent_on:%d %b %Y} will be followed up "
        f"from {first:%d %b %Y}, every 2 days, 4 follow-ups, then the closing email, in that Gmail conversation."
        + ("" if getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False) else
           " Automatic follow-ups are turned off on this site, so nothing is sent yet."))
    return redirect("enquiries:board")


def _suggested_follow_up_stage(enquiry_id: str, enquiry: Mapping[str, Any]) -> str:
    """Stage to preselect on the compose page: payment pending for accepted orders."""
    if str(enquiry.get("status") or "").strip().lower() == "accepted":
        return PAYMENT_STAGE
    return ""


_GMAIL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _is_valid_email(value: str) -> bool:
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    try:
        validate_email(value or "")
    except ValidationError:
        return False
    return True


def _resolve_follow_up_conversation(enquiry_id, enquiry, gmail_service, chosen_thread=""):
    """Find the enquiry's Gmail conversation without guessing.

    Returns ("ok", conversation), ("choose", [conversations]) or ("error", message).
    Order: an employee-confirmed choice, the conversation of the last sent follow-up,
    the original Gmail message saved on email-created enquiries (customer_reference
    "EMAIL-<id>"), then a Gmail search. Search results from subject + name alone are
    never used without the employee confirming the conversation.
    """
    subject = str(enquiry.get("subject") or "").strip()
    customer_name = str(enquiry.get("customer_name") or "").strip()
    saved_email = str(enquiry.get("customer_email") or "").strip()
    if not _is_valid_email(saved_email):
        saved_email = ""

    def search():
        if not subject:
            return []
        return gmail_service.find_enquiry_conversations(
            subject=subject, customer_email=saved_email, customer_name=customer_name
        )

    if chosen_thread:
        candidates = search()
        for conversation in candidates:
            if conversation.get("thread_id") == chosen_thread:
                return "ok", conversation
        return "error", "The selected Gmail conversation does not match this enquiry. Please try Follow-up again."

    # The enquiry's own thread: the newest of the last sent follow-up and the quotation / PI / rejection
    # email that started the follow-ups. Every later email replies there, never in another thread.
    previous = (
        FollowUpSent.objects.filter(enquiry_id=enquiry_id, status=FollowUpSent.STATUS_SENT)
        .exclude(gmail_thread_id="").exclude(stage=MARKETING_STAGE)  # Marketing has its own thread
        .order_by("-sent_at")
        .first()
    )
    start = (FollowUpStart.objects.filter(enquiry_id=str(enquiry_id)).exclude(gmail_thread_id="")
             .exclude(stage=MARKETING_STAGE).order_by("-started_at").first())
    known = [(row.sent_at, row.gmail_thread_id, row.customer_email) for row in (previous,) if row]
    known += [(row.started_at, row.gmail_thread_id, row.customer_email) for row in (start,) if row]
    for _, thread_id, known_email in sorted(known, key=lambda item: item[0], reverse=True):
        try:
            conversation = gmail_service.describe_conversation(thread_id)
        except GmailAPIError:
            conversation = None
        if conversation:
            if known_email:
                conversation["customer_email"] = known_email.lower()
            return "ok", conversation

    reference = str(enquiry.get("customer_reference") or "").strip()
    if reference.upper().startswith("EMAIL-"):
        original_id = reference[len("EMAIL-"):].strip()
        if _GMAIL_ID_PATTERN.match(original_id):
            try:
                original = gmail_service.get_message(original_id)
            except GmailAPIError:
                original = None
            thread_id = (original or {}).get("thread_id")
            if thread_id:
                conversation = gmail_service.describe_conversation(thread_id)
                if conversation:
                    return "ok", conversation

    if not subject and not saved_email:
        return "error", "This enquiry has no subject or customer email, so its Gmail conversation cannot be identified."

    candidates = search()
    if not candidates:
        return "error", "No Gmail conversation matching this enquiry was found. No follow-up was prepared."
    if saved_email and len(candidates) == 1:
        return "ok", candidates[0]
    # Several matches, or a match found by subject/name only: the employee must confirm.
    return "choose", candidates


@login_required
def gmail_ai_leads(request: HttpRequest) -> JsonResponse:
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    force_refresh = str(request.GET.get("force") or "").strip().lower() in {"1", "true", "yes", "on"}

    lead_limit = int(request.GET.get("limit") or 200)
    candidate_limit = int(request.GET.get("candidate_limit") or 200)

    ttl_hours_env = (os.getenv("ENQUIRY_GMAIL_AI_LEADS_CACHE_TTL_HOURS") or "").strip()
    ttl_hours = int(ttl_hours_env) if ttl_hours_env.isdigit() else 12
    positive_cache_cutoff = timezone.now() - timedelta(hours=ttl_hours)

    api_root = _get_api_root()
    source = "gmail_search"
    errors: List[str] = []

    gmail_service = GmailService(request.user)

    try:
        candidates = _search_gmail_lead_candidates(
            gmail_service,
            user_email=getattr(request.user, "email", None),
            candidate_limit=candidate_limit,
        )
    except GmailAPIError as exc:
        return JsonResponse(
            {
                "source": source,
                "error": str(exc),
                "reauth_url": _get_google_reauth_url(),
                "leads": [],
                "candidates": 0,
                "cached": 0,
                "analyzed": 0,
                "remaining": 0,
            },
            status=400,
        )

    message_ids = [row["message_id"] for row in candidates if row.get("message_id")]
    existing = {
        record.message_id: record
        for record in GmailLeadAnalysis.objects.filter(user=request.user, message_id__in=message_ids)
    }

    leads: List[Dict[str, Any]] = []
    analyzed = 0
    cached = 0
    remaining = 0

    for candidate in candidates:
        if len(leads) >= lead_limit:
            break

        message_id = candidate.get("message_id")
        if not message_id:
            continue

        record = existing.get(message_id)
        record_fresh = bool(
            record
            and record.model_name == GMAIL_LEAD_HEURISTIC_MODEL
            and record.analyzed_at
            and record.analyzed_at >= positive_cache_cutoff
        )

        message_payload: Dict[str, Any] = {
            "id": message_id,
            "subject": candidate.get("subject") or "",
            "sender": candidate.get("sender") or "",
            "snippet": candidate.get("snippet") or "",
            "body_text": "",
            "attachments": [],
        }
        if record and record_fresh and not force_refresh:
            cached += 1
            if record.is_lead:
                leads.append(
                    {
                        "message_id": record.message_id,
                        "mailbox": record.mailbox,
                        "sender": record.sender,
                        "subject": record.subject,
                        "received_at": record.received_at.isoformat() if record.received_at else None,
                        "lead_score": record.lead_score,
                        "reason": record.reason,
                        "next_step": record.next_step,
                        "gmail_url": _gmail_web_message_url(request.user, record.message_id),
                    }
                )
            continue

        try:
            message_payload = gmail_service.get_message(message_id)
        except GmailAPIError as exc:
            errors.append(str(exc))
            remaining += 1
            continue

        if not _gmail_message_has_pdf_attachment(message_payload):
            if record is not None:
                record.is_lead = False
                record.lead_score = 0
                record.reason = "No PDF attachment detected."
                record.next_step = ""
                record.raw = {}
                record.analyzed_at = timezone.now()
                record.save()
            remaining += 1
            continue

        analysis = _score_gmail_lead_heuristically(
            message_payload,
            user_email=getattr(request.user, "email", None),
        )
        is_lead = bool(analysis.get("is_lead"))
        lead_score = int(analysis.get("lead_score") or 0)
        reason = str(analysis.get("reason") or "")
        next_step = str(analysis.get("next_step") or "")

        parsed_date = message_payload.get("parsed_date")
        received_at = parsed_date if isinstance(parsed_date, timezone.datetime) else None

        if record is None:
            record = GmailLeadAnalysis(user=request.user, message_id=message_id)
        record.mailbox = candidate.get("mailbox") or record.mailbox
        record.sender = str(message_payload.get("sender") or candidate.get("sender") or "")[:320]
        record.subject = str(message_payload.get("subject") or candidate.get("subject") or "")[:500]
        record.received_at = received_at
        record.is_lead = is_lead
        record.lead_score = lead_score
        record.reason = reason
        record.next_step = next_step
        record.model_name = GMAIL_LEAD_HEURISTIC_MODEL
        record.raw = {
            "rule": GMAIL_LEAD_HEURISTIC_MODEL,
            "attachment_count": len(message_payload.get("attachments") or []),
            "signals": analysis.get("signals") or [],
            "blockers": analysis.get("blockers") or [],
        }
        record.analyzed_at = timezone.now()
        record.save()
        existing[message_id] = record

        analyzed += 1

        if record.is_lead:
            leads.append(
                {
                    "message_id": record.message_id,
                    "mailbox": record.mailbox,
                    "sender": record.sender,
                    "subject": record.subject,
                    "received_at": record.received_at.isoformat() if record.received_at else None,
                    "lead_score": record.lead_score,
                    "reason": record.reason,
                    "next_step": record.next_step,
                    "gmail_url": _gmail_web_message_url(request.user, record.message_id),
                }
            )
        else:
            remaining += 1

    return JsonResponse(
        {
            "source": source,
            "leads": leads,
            "candidates": len(candidates),
            "cached": cached,
            "analyzed": analyzed,
            "remaining": remaining,
            "ttl_hours": ttl_hours,
            "errors": errors[:10],
        }
    )


@login_required
@require_POST
def toggle_converted_status(request: HttpRequest, enquiry_id: str) -> JsonResponse:
    api_root = _get_api_root()
    raw_payload: Dict[str, Any]
    content_type = (request.META.get("CONTENT_TYPE") or request.content_type or "").lower()
    if "application/json" in content_type:
        try:
            decoded = request.body.decode("utf-8") if request.body else "{}"
            raw_payload = json.loads(decoded or "{}")
        except (ValueError, UnicodeDecodeError):
            raw_payload = {}
    else:
        try:
            raw_payload = request.POST.dict()
        except Exception:
            raw_payload = {}

    converted_flag = raw_payload.get("converted")
    if isinstance(converted_flag, str):
        converted = converted_flag.strip().lower() in {"1", "true", "yes", "on"}
    else:
        converted = bool(converted_flag)

    previous_status_raw = str(raw_payload.get("previous_status") or "").strip().lower() or None
    if previous_status_raw and previous_status_raw not in STATUS_VALUE_SET:
        return JsonResponse({"error": "Unknown previous status."}, status=400)
    if converted and (not previous_status_raw or previous_status_raw == CONVERTED_STATUS):
        return JsonResponse({"error": "A non-converted status is required before converting."}, status=400)

    session_map: Dict[str, str] = request.session.get(CONVERSION_SESSION_KEY, {})
    if not isinstance(session_map, dict):
        session_map = {}

    target_status = CONVERTED_STATUS if converted else previous_status_raw or session_map.get(enquiry_id)
    if not converted and (not target_status or target_status == CONVERTED_STATUS):
        return JsonResponse({"error": "Unable to determine the previous status for this enquiry."}, status=400)

    if target_status not in STATUS_VALUE_SET:
        return JsonResponse({"error": "Status value is not supported."}, status=400)
    if converted:
        blocked = _conversion_block_reason(enquiry_id, previous_status_raw, CONVERTED_STATUS)
        if blocked:
            return JsonResponse({"error": blocked}, status=400)
    if not converted and target_status not in NON_CONVERTED_STATUS_SET:
        return JsonResponse({"error": "Only non-converted statuses can be restored."}, status=400)

    try:
        services.update_enquiry(api_root, enquiry_id, {"status": target_status}, request=request)
    except services.EnquiryServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if converted:
        session_map[enquiry_id] = previous_status_raw or session_map.get(enquiry_id) or "open"
        stored_previous = session_map[enquiry_id]
    else:
        stored_previous = target_status
        session_map.pop(enquiry_id, None)
        if _payment_status(enquiry_id) == EnquiryContact.PAYMENT_PAID:
            # Conversion undone: the payment is no longer treated as received.
            _save_payment(enquiry_id, EnquiryContact.PAYMENT_PENDING, request.user, keep_details=True)

    request.session[CONVERSION_SESSION_KEY] = session_map
    request.session.modified = True

    response_payload = {
        "status": "ok",
        "enquiry_id": enquiry_id,
        "new_status": target_status,
        "previous_status": stored_previous,
    }
    return JsonResponse(response_payload)


@login_required
@require_POST
def delete_enquiry(request: HttpRequest, enquiry_id: str) -> JsonResponse:
    """Delete an enquiry from the catalogue view.

    This proxies a delete call to the enquiry microservice and returns a lightweight JSON response
    suitable for AJAX callers on the board view.
    """
    api_root = _get_api_root()
    try:
        services.delete_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    # Clear any stored conversion state for this enquiry to avoid stale session data.
    session_map: Dict[str, str] = request.session.get(CONVERSION_SESSION_KEY, {})
    if isinstance(session_map, dict) and enquiry_id in session_map:
        session_map.pop(enquiry_id, None)
        request.session[CONVERSION_SESSION_KEY] = session_map
        request.session.modified = True
    return JsonResponse({"status": "ok", "enquiry_id": enquiry_id})


@login_required
def gmail_queue(request: HttpRequest) -> HttpResponse:
    api_root = _get_api_root()
    selected_status = (request.GET.get("event_status") or "all").strip().lower()
    status_filter = None if not selected_status or selected_status == "all" else selected_status

    if request.method == "POST":
        action = request.POST.get("action") or ""
        try:
            if action == "promote_event":
                message_id = request.POST.get("message_id")
                if not message_id:
                    messages.error(request, "Missing email event identifier.")
                else:
                    priority = request.POST.get("priority") or None
                    assigned_to = request.POST.get("assigned_to") or None
                    services.promote_email_event(
                        api_root,
                        message_id,
                        priority=priority,
                        assigned_to=assigned_to,
                        request=request,
                    )
                    messages.success(request, "Email event promoted to enquiry.")
            else:
                messages.error(request, "Unknown action requested.")
        except services.EnquiryServiceError as exc:
            messages.error(request, f"Action failed: {exc}")

        query = ""
        if selected_status and selected_status != "all":
            query = f"?event_status={selected_status}"
        return redirect(f"{reverse('enquiries:gmail_queue')}{query}")

    try:
        email_events = services.list_email_events(
            api_root,
            status=status_filter,
            limit=200,
            request=request,
        )
    except services.EnquiryServiceError as exc:
        email_events = []
        messages.error(request, f"Unable to load Gmail events: {exc}")

    context = {
        "api_root": api_root,
        "email_events": email_events,
        "event_status": selected_status or "all",
        "event_filters": EVENT_FILTERS,
        "priority_choices": PRIORITY_CHOICES,
        "default_priority": DEFAULT_PRIORITY,
    }
    return render(request, "enquiries/gmail_queue.html", context)


def _create_google_sheet_enquiry(request: HttpRequest, *, sheet_title: str) -> Dict[str, Any]:
    api_root = _get_api_root()
    if not sheet_title:
        sheet_title = "New Enquiry"
    customer_name = " ".join((request.POST.get("customer_name") or "").split())[:255]
    customer_email = (request.POST.get("customer_email") or "").strip().lower()
    kyc_customer = _kyc_customer_from_post(request)
    if kyc_customer is not None:
        customer_name = customer_name or kyc_customer.name[:255]
        customer_email = customer_email or kyc_links.contact_email(kyc_customer)
    if customer_email and not _is_valid_email(customer_email):
        raise ValueError("Please enter a valid customer email address.")
    other_emails, other_error = customer_emails.parse(request.POST.getlist("other_customer_email"),
                                                      main=customer_email)
    if other_error:
        raise ValueError(other_error)
    if other_emails and not customer_email:
        customer_email, other_emails = other_emails[0], other_emails[1:]

    gmail_service = GmailService(request.user)
    credentials = gmail_service.build_authorized_credentials()

    try:
        sheets_service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        sheet_body = {"properties": {"title": sheet_title}}
        spreadsheet = (
            sheets_service.spreadsheets()
            .create(body=sheet_body, fields="spreadsheetId,spreadsheetUrl")
            .execute()
        )
    except HttpError as exc:
        status = getattr(exc, "resp", None)
        status_code = getattr(status, "status", None)
        logger.exception(
            "Google Sheets API error when creating enquiry sheet for user %s",
            getattr(request.user, "username", "unknown"),
        )
        message = "Unable to create Google Sheet. Please reconnect your Google account."
        if status_code and status_code >= 500:
            message = "Google Sheets is unavailable right now. Please try again in a moment."
        raise RuntimeError(message) from exc
    except Exception as exc:
        logger.exception("Unexpected error while creating enquiry sheet for user %s", request.user)
        raise RuntimeError("Unable to create Google Sheet at the moment.") from exc

    sheet_url = spreadsheet.get("spreadsheetUrl")
    spreadsheet_id = spreadsheet.get("spreadsheetId")
    if not sheet_url and spreadsheet_id:
        sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    default_payload = {
        "subject": sheet_title,
        "project_name": sheet_title,
        # The enquiry service requires a customer name; fall back to the title as before.
        "customer_name": customer_name or sheet_title,
        "enquiry_date": timezone.now().date().isoformat(),
        "currency": "USD",
        "status": "open",
        "channel": "portal",
        "remarks": f"Created from Google Sheet: {sheet_url}",
        "description": f"Created from Google Sheet: {sheet_title}",
    }
    enquiry_form = forms.EnquiryCreateForm(default_payload)
    if not enquiry_form.is_valid():
        first_error = next(iter(enquiry_form.errors.values()))[0]
        raise ValueError(str(first_error))

    payload = enquiry_form.build_payload()
    payload["status"] = "open"
    payload["remarks"] = payload.get("remarks") or f"Created from Google Sheet: {sheet_url}"
    payload["description"] = payload.get("description") or payload["remarks"]
    payload["channel"] = payload.get("channel") or "portal"
    created_enquiry = services.create_enquiry(
        api_root,
        payload,
        request=request,
    )
    _save_customer_email((created_enquiry or {}).get("enquiry_id"), customer_email, request.user)
    customer_emails.save_others((created_enquiry or {}).get("enquiry_id"), other_emails, request.user)
    if kyc_customer is not None:
        kyc_links.set_link((created_enquiry or {}).get("enquiry_id"), kyc_customer, request.user)
    return {
        "sheet_url": sheet_url,
        "spreadsheet_id": spreadsheet_id,
        "title": sheet_title,
        "enquiry_id": (created_enquiry or {}).get("enquiry_id"),
        "enquiry_detail_url": (
            reverse("enquiries:detail", args=[created_enquiry.get("enquiry_id")])
            if created_enquiry and created_enquiry.get("enquiry_id")
            else None
        ),
    }


@login_required
@require_POST
def create_google_sheet(request: HttpRequest) -> JsonResponse:
    """Create a dedicated Google Sheet for a new enquiry and return its URL."""
    sheet_title = (request.POST.get("title") or "").strip()
    try:
        response_payload = _create_google_sheet_enquiry(request, sheet_title=sheet_title)
    except GmailAPIError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except services.EnquiryServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse(response_payload, status=201)


@login_required
@require_POST
def import_google_sheet(request: HttpRequest) -> HttpResponse:
    api_root = _get_api_root()
    form = forms.GoogleSheetImportForm(request.POST)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
        return redirect("enquiries:board")

    sheet_url = form.cleaned_data["sheet_url"]
    status = form.cleaned_data.get("status") or "open"

    try:
        payload = _build_enquiry_payload_from_sheet(request.user, sheet_url)
        payload["status"] = status
        services.create_enquiry(api_root, payload, request=request)
        messages.success(request, "Enquiry saved from Google Sheet.")
    except GoogleSheetImportError as exc:
        messages.error(request, str(exc))
    except GmailAPIError as exc:
        messages.error(request, f"Google authentication error: {exc}")
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to save enquiry: {exc}")
    return redirect("enquiries:board")


@login_required
@require_POST
def upload_pdf_enquiry(request: HttpRequest) -> HttpResponse:
    try:
        _create_pdf_enquiry(request)
        messages.success(request, "PDF uploaded as a new enquiry.")
    except ValueError as exc:
        messages.error(request, str(exc))
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to save enquiry: {exc}")
    return redirect("enquiries:board")


def _create_pdf_enquiry(
    request: HttpRequest,
    *,
    title: Optional[str] = None,
    pdf_file: Any = None,
) -> Dict[str, Any]:
    api_root = _get_api_root()
    if pdf_file is None:
        form = forms.PDFUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            first_error = next(iter(form.errors.values()))[0]
            raise ValueError(str(first_error))
        pdf_file = form.cleaned_data["pdf"]
        title = title or form.cleaned_data.get("title")
        customer_name = (form.cleaned_data.get("customer_name") or "").strip()
        customer_email = (form.cleaned_data.get("customer_email") or "").strip().lower()
        kyc_customer = _kyc_customer_from_post(request)
        if kyc_customer is not None:
            customer_name = customer_name or kyc_customer.name[:255]
            customer_email = customer_email or kyc_links.contact_email(kyc_customer)
        other_emails, other_error = customer_emails.parse(request.POST.getlist("other_customer_email"),
                                                          main=customer_email)
        if other_error:
            raise ValueError(other_error)
        if other_emails and not customer_email:
            customer_email, other_emails = other_emails[0], other_emails[1:]
    else:
        customer_name, customer_email, kyc_customer, other_emails = "", "", None, []
        pdf_name = (getattr(pdf_file, "name", "") or "").lower()
        if not pdf_name.endswith(".pdf"):
            raise ValueError("Please upload a PDF file.")

    resolved_title = title or pdf_file.name.rsplit(".", 1)[0] or "PDF Enquiry"
    today = timezone.now().date().isoformat()

    default_payload = {
        "subject": resolved_title,
        # The enquiry service requires a customer name; fall back to the title as before.
        "customer_name": customer_name or resolved_title,
        "project_name": resolved_title,
        "enquiry_date": today,
        "currency": "USD",
        "description": f"Uploaded from PDF {pdf_file.name}",
        "status": "open",
    }

    enquiry_form = forms.EnquiryCreateForm(default_payload)
    if not enquiry_form.is_valid():
        first_error = next(iter(enquiry_form.errors.values()))[0]
        raise ValueError(f"Unable to prepare enquiry: {first_error}")

    payload = enquiry_form.build_payload()
    payload["status"] = "open"
    created = services.create_enquiry(
        api_root,
        payload,
        attachments=[pdf_file],
        request=request,
    )
    enquiry_id = (created or {}).get("enquiry_id")
    _save_customer_email(enquiry_id, customer_email, request.user)
    customer_emails.save_others(enquiry_id, other_emails, request.user)
    if kyc_customer is not None:
        kyc_links.set_link(enquiry_id, kyc_customer, request.user)
    return {
        "title": resolved_title,
        "enquiry_id": enquiry_id,
        "enquiry_detail_url": reverse("enquiries:detail", args=[enquiry_id]) if enquiry_id else None,
    }


def _is_new_rejection(old_status: Any, new_status: Any) -> bool:
    return str(new_status or "").strip().lower() == "rejected" and str(old_status or "").strip().lower() != "rejected"


def _lost_reason_from(post) -> str:
    """The reason staff gave for losing an enquiry: one of the listed reasons, with a note for "Other"."""
    reason = (post.get("lost_reason") or "").strip()
    if reason not in negotiation.LOST_REASONS:
        return ""
    note = " ".join((post.get("lost_reason_note") or "").split())
    return f"{reason}: {note}" if reason == "Other" and note else reason


@login_required
def detail(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry {enquiry_id}: {exc}")
        return redirect("enquiries:board")

    subdivisions = enquiry.get("subdivisions")
    if subdivisions is None:
        try:
            subdivisions = services.fetch_subdivisions(api_root, enquiry_id, request=request)
        except services.EnquiryServiceError as exc:
            subdivisions = []
            messages.error(request, f"Unable to load sub-enquiries: {exc}")
    subdivisions = list(subdivisions or [])
    enquiry["subdivisions"] = subdivisions

    if request.method == "POST":
        form = forms.EnquiryUpdateForm(request.POST, request.FILES, initial=enquiry)
        form.fields["attachments"].label = "Attach additional files"
        form.fields["profile"].label = "Replace profile"
        blocked = form.is_valid() and _conversion_block_reason(enquiry_id, enquiry.get("status"), form.cleaned_data.get("status"))
        lost_reason = _lost_reason_from(request.POST)
        if form.is_valid() and not blocked and _is_new_rejection(enquiry.get("status"), form.cleaned_data.get("status")) and not lost_reason:
            blocked = "Choose why the enquiry was lost before rejecting it."
        if blocked:
            form.add_error("status", blocked)
            messages.error(request, blocked)
        elif form.is_valid():
            payload = form.cleaned_payload()
            files = request.FILES.getlist("attachments")
            try:
                services.update_enquiry(
                    api_root,
                    enquiry_id,
                    payload,
                    attachments=files,
                    profile_files=request.FILES.getlist("profile"),
                    request=request,
                )
                messages.success(request, "Enquiry updated successfully.")
                _after_status_change(request, enquiry_id, enquiry, payload.get("status"), lost_reason=lost_reason)
                return redirect(reverse("enquiries:detail", args=[enquiry_id]))
            except services.EnquiryServiceError as exc:
                messages.error(request, f"Unable to update enquiry: {exc}")
        else:
            messages.error(request, "Please correct the highlighted errors and try again.")
    else:
        form = forms.EnquiryUpdateForm(initial=enquiry)
        form.fields["attachments"].label = "Attach additional files"
        form.fields["profile"].label = "Replace profile"

    field_groups = _collect_field_groups(form)
    primary_fields = [
        form[name] for name in PRIMARY_UPDATE_FIELDS if name in form.fields
    ]

    context = {
        "enquiry": enquiry,
        "form": form,
        "api_root": api_root,
        "design_fields": field_groups["design"],
        "compounding_fields": field_groups["compounding"],
        "production_fields": field_groups["production"],
        "costing_fields": field_groups["costing"],
        "primary_fields": primary_fields,
        "subdivisions": subdivisions,
        "column_matrix": _build_column_matrix_display(enquiry),
        "lost_reasons": negotiation.LOST_REASONS,
    }
    return render(request, "enquiries/detail.html", context)


@login_required
def download_attachment(request: HttpRequest, enquiry_id: str, attachment_id: str) -> HttpResponse:
    api_root = _get_api_root()
    download_path = f"/enquiries/{enquiry_id}/attachments/{attachment_id}"
    try:
        content, filename, content_type = services.download_attachment(
            api_root,
            download_path,
            request=request,
        )
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to download attachment: {exc}")
        return redirect(reverse("enquiries:detail", args=[enquiry_id]))

    safe_filename = filename.replace('"', "")
    response = HttpResponse(content, content_type=content_type)
    disposition = "inline" if request.GET.get("inline") == "1" else "attachment"
    response["Content-Disposition"] = f"{disposition}; filename=\"{safe_filename}\""
    return response


@login_required
def subdivision_create(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry {enquiry_id}: {exc}")
        return redirect("enquiries:board")

    if request.method == "POST":
        post_data = request.POST.copy()
        post_data.setdefault("parent_enquiry_id", enquiry_id)
        form = forms.SubdivisionForm(post_data, request.FILES)
        if form.is_valid():
            payload = form.cleaned_payload(include_nulls=False)
            attachments = request.FILES.getlist("attachments")
            profile_files = request.FILES.getlist("profile")
            try:
                created = services.create_subdivision(
                    api_root,
                    enquiry_id,
                    payload,
                    attachments=attachments,
                    profile_files=profile_files,
                    request=request,
                )
            except services.EnquiryServiceError as exc:
                messages.error(request, f"Unable to create sub-enquiry: {exc}")
            else:
                subdivision_id = created.get("subdivision_id")
                if subdivision_id:
                    messages.success(request, f"SubEnquiry {subdivision_id} created successfully.")
                    return redirect(reverse("enquiries:subdivision_detail", args=[enquiry_id, subdivision_id]))
                messages.success(request, "SubEnquiry created successfully.")
                return redirect(reverse("enquiries:detail", args=[enquiry_id]))
        else:
            messages.error(request, "Please correct the highlighted errors and try again.")
    else:
        initial = dict(enquiry)
        initial["parent_enquiry_id"] = enquiry_id
        next_code: str = ""
        try:
            existing_subs = services.fetch_subdivisions(api_root, enquiry_id, request=request)
        except services.EnquiryServiceError as exc:
            existing_subs = []
            messages.error(request, f"Unable to load sub-enquiries: {exc}")
        used_codes = {
            (sub.get("code") or "").strip().lower()
            for sub in existing_subs or []
        }
        for letter in string.ascii_lowercase:
            if letter not in used_codes:
                next_code = letter
                break
        if next_code:
            initial.setdefault("code", next_code.upper())
            initial.setdefault("subdivision_id", f"{enquiry_id}-{next_code}")
        form = forms.SubdivisionForm(initial=initial)

    field_groups = _collect_field_groups(form)
    context = {
        "enquiry": enquiry,
        "subdivision": None,
        "form": form,
        "design_fields": field_groups["design"],
        "compounding_fields": field_groups["compounding"],
        "production_fields": field_groups["production"],
        "costing_fields": field_groups["costing"],
        "is_new": True,
    }
    return render(request, "enquiries/subdivision_detail.html", context)


@login_required
def subdivision_detail(request: HttpRequest, enquiry_id: str, subdivision_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry {enquiry_id}: {exc}")
        return redirect("enquiries:board")

    try:
        subdivision = services.fetch_subdivision(api_root, enquiry_id, subdivision_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load sub-enquiry {subdivision_id}: {exc}")
        return redirect(reverse("enquiries:detail", args=[enquiry_id]))

    if request.method == "POST":
        post_data = request.POST.copy()
        post_data.setdefault("parent_enquiry_id", enquiry_id)
        post_data.setdefault("subdivision_id", subdivision_id)
        form = forms.SubdivisionForm(post_data, request.FILES, initial=subdivision)
        if form.is_valid():
            payload = form.cleaned_payload(include_nulls=True)
            attachments = request.FILES.getlist("attachments")
            profile_files = request.FILES.getlist("profile")
            try:
                services.update_subdivision(
                    api_root,
                    enquiry_id,
                    subdivision_id,
                    payload,
                    attachments=attachments,
                    profile_files=profile_files,
                    request=request,
                )
            except services.EnquiryServiceError as exc:
                messages.error(request, f"Unable to update sub-enquiry: {exc}")
            else:
                messages.success(request, "SubEnquiry updated successfully.")
                return redirect(reverse("enquiries:subdivision_detail", args=[enquiry_id, subdivision_id]))
        else:
            messages.error(request, "Please correct the highlighted errors and try again.")
    else:
        initial = dict(subdivision)
        initial["parent_enquiry_id"] = enquiry_id
        form = forms.SubdivisionForm(initial=initial)

    field_groups = _collect_field_groups(form)
    context = {
        "enquiry": enquiry,
        "subdivision": subdivision,
        "form": form,
        "design_fields": field_groups["design"],
        "compounding_fields": field_groups["compounding"],
        "production_fields": field_groups["production"],
        "costing_fields": field_groups["costing"],
        "is_new": False,
    }
    return render(request, "enquiries/subdivision_detail.html", context)


@login_required
@require_POST
def subdivision_delete(request: HttpRequest, enquiry_id: str, subdivision_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        services.delete_subdivision(api_root, enquiry_id, subdivision_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to delete sub-enquiry: {exc}")
    else:
        messages.success(request, f"SubEnquiry {subdivision_id} deleted.")
    return redirect(reverse("enquiries:detail", args=[enquiry_id]))


@login_required
def download_subdivision_attachment(
    request: HttpRequest,
    enquiry_id: str,
    subdivision_id: str,
    attachment_id: str,
) -> HttpResponse:
    api_root = _get_api_root()
    try:
        content, filename, content_type = services.download_subdivision_attachment(
            api_root,
            enquiry_id,
            subdivision_id,
            attachment_id,
            request=request,
        )
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to download sub-enquiry attachment: {exc}")
        return redirect(reverse("enquiries:subdivision_detail", args=[enquiry_id, subdivision_id]))

    safe_filename = filename.replace('"', "")
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{safe_filename}"'
    return response


@login_required
def summary(request: HttpRequest) -> HttpResponse:
    api_root = _get_api_root()
    selected_period = (request.GET.get("period") or "all").strip().lower()
    if selected_period not in SUMMARY_PERIOD_CHOICES:
        selected_period = "all"
    start_date = (request.GET.get("start_date") or "").strip()
    end_date = (request.GET.get("end_date") or "").strip()
    filters_applied = str(request.GET.get("applied") or "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        data = services.fetch_summary(
            api_root,
            period=selected_period,
            start_date=start_date or None,
            end_date=end_date or None,
            request=request,
        )
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load summary: {exc}")
        data = None

    filtered_enquiries: List[Dict[str, Any]] = []
    if filters_applied:
        try:
            all_enquiries = services.list_enquiries(api_root, request=request)
            filtered_enquiries = _filter_summary_enquiries(
                all_enquiries,
                period=selected_period,
                start_date=start_date,
                end_date=end_date,
            )
            for enquiry in filtered_enquiries:
                _attach_primary_pdf_link(enquiry)
        except ValueError:
            filtered_enquiries = []
        except services.EnquiryServiceError as exc:
            messages.error(request, f"Unable to load filtered enquiries: {exc}")

    if data:
        _tidy_summary(data)
    for enquiry in filtered_enquiries:
        enquiry["received_at_dt"] = _normalize_summary_filter_datetime(enquiry.get("received_at_dt"))

    context = {
        "summary": data,
        "summary_time_zone": SUMMARY_DISPLAY_TIME_ZONE,
        "summary_period_choices": SUMMARY_PERIOD_CHOICES,
        "summary_selected_period": selected_period,
        "summary_start_date": start_date,
        "summary_end_date": end_date,
        "summary_scope_label": SUMMARY_PERIOD_CHOICES.get(selected_period, "All enquiries"),
        "summary_filters_applied": filters_applied,
        "summary_filtered_enquiries": filtered_enquiries,
    }
    return render(request, "enquiries/summary.html", context)


@login_required
def export_pdf(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        pdf_bytes, filename = services.export_enquiry_pdf(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to export enquiry {enquiry_id}: {exc}")
        return redirect(reverse("enquiries:detail", args=[enquiry_id]))
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    disposition = "inline" if request.GET.get("inline") == "1" else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


@login_required
def preview(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry {enquiry_id}: {exc}")
        return redirect("enquiries:board")

    form_initial: Dict[str, Any] = {}
    for name in forms.EnquiryUpdateForm.base_fields.keys():
        if name in PREVIEW_SKIP_FIELDS:
            continue
        if name in enquiry:
            form_initial[name] = enquiry.get(name)

    preview_form = forms.EnquiryUpdateForm(initial=form_initial)
    field_entries, additional_values = _build_preview_field_entries(preview_form, form_initial)

    context = {
        "enquiry": enquiry,
        "subdivision": None,
        "fields": field_entries,
        "additional_values": additional_values,
        "preview_mode": "update",
        "back_url": reverse("enquiries:detail", args=[enquiry_id]),
        "heading": f"Generate Share Link for Enquiry {enquiry_id}",
        "column_matrix": _build_column_matrix_display(enquiry),
        "requires_pdf_upload": not _record_has_pdf(enquiry),
        "existing_pdf_name": _first_pdf_name(enquiry),
        "share_error": None,
    }
    return render(request, "enquiries/preview.html", context)


@login_required
def preview_subdivision(request: HttpRequest, enquiry_id: str, subdivision_id: str) -> HttpResponse:
    api_root = _get_api_root()
    try:
        subdivision = services.fetch_subdivision(api_root, enquiry_id, subdivision_id, request=request)
        parent = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load sub-enquiry {subdivision_id}: {exc}")
        return redirect(reverse("enquiries:detail", args=[enquiry_id]))

    form_initial: Dict[str, Any] = {}
    for name in forms.SubdivisionForm.base_fields.keys():
        if name in PREVIEW_SKIP_FIELDS:
            continue
        if name in subdivision:
            form_initial[name] = subdivision.get(name)

    form_initial.setdefault("parent_enquiry_id", enquiry_id)
    form_initial.setdefault("subdivision_id", subdivision_id)
    form_initial.setdefault("code", subdivision.get("code"))

    preview_form = forms.SubdivisionForm(initial=form_initial)
    field_entries, additional_values = _build_preview_field_entries(
        preview_form,
        form_initial,
        hidden_names={"parent_enquiry_id", "subdivision_id", "code"},
    )

    context = {
        "enquiry": parent,
        "subdivision": subdivision,
        "fields": field_entries,
        "additional_values": additional_values,
        "preview_mode": "subdivision",
        "back_url": reverse("enquiries:subdivision_detail", args=[enquiry_id, subdivision_id]),
        "heading": f"Generate Share Link for SubEnquiry {subdivision.get('subdivision_id')}",
        "column_matrix": _build_column_matrix_display(parent),
        "requires_pdf_upload": not _record_has_pdf(subdivision),
        "existing_pdf_name": _first_pdf_name(subdivision),
        "share_error": None,
    }
    return render(request, "enquiries/preview.html", context)


def _render_preview_response(
    request: HttpRequest,
    form: django_forms.Form,
    mode: str,
    *,
    error_message: Optional[str] = None,
    share_error: Optional[str] = None,
) -> HttpResponse:
    if error_message:
        messages.error(request, error_message)

    api_root = _get_api_root()
    enquiry_id = request.POST.get("enquiry_id") or request.POST.get("parent_enquiry_id") or ""

    if mode == "subdivision":
        parent_id = request.POST.get("parent_enquiry_id") or enquiry_id
        subdivision_id = request.POST.get("subdivision_id") or ""
        try:
            subdivision = services.fetch_subdivision(api_root, parent_id, subdivision_id, request=request)
            parent = services.fetch_enquiry(api_root, parent_id, request=request)
        except services.EnquiryServiceError:
            subdivision = {"subdivision_id": subdivision_id}
            parent = {"enquiry_id": parent_id}

        post_data = {k: v for k, v in request.POST.items()}
        field_entries, additional_values = _build_preview_field_entries(
            form,
            post_data,
            hidden_names={"parent_enquiry_id", "subdivision_id", "code"},
        )

        hidden_error_msgs: List[str] = []
        try:
            visible_names = {e.get("name") for e in field_entries if isinstance(e, dict)}
            for fname in form.errors.keys():
                if fname not in visible_names:
                    for err in form.errors.getlist(fname):
                        hidden_error_msgs.append(f"{fname}: {err}")
        except Exception:
            hidden_error_msgs = []

        context = {
            "enquiry": parent,
            "subdivision": subdivision,
            "fields": field_entries,
            "additional_values": additional_values,
            "preview_mode": "subdivision",
            "back_url": reverse("enquiries:subdivision_detail", args=[parent.get("enquiry_id"), subdivision.get("subdivision_id")]),
            "heading": f"Generate Share Link for SubEnquiry {subdivision.get('subdivision_id')}",
            "column_matrix": _build_column_matrix_display(parent),
            "hidden_errors": hidden_error_msgs,
            "requires_pdf_upload": not _record_has_pdf(subdivision),
            "existing_pdf_name": _first_pdf_name(subdivision),
            "share_error": share_error,
        }
        return render(request, "enquiries/preview.html", context)

    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request) if enquiry_id else {}
    except services.EnquiryServiceError:
        enquiry = {"enquiry_id": enquiry_id}

    post_data = {k: v for k, v in request.POST.items()}
    field_entries, additional_values = _build_preview_field_entries(form, post_data)

    hidden_error_msgs2: List[str] = []
    try:
        visible_names2 = {e.get("name") for e in field_entries if isinstance(e, dict)}
        for fname in form.errors.keys():
            if fname not in visible_names2:
                for err in form.errors.getlist(fname):
                    hidden_error_msgs2.append(f"{fname}: {err}")
    except Exception:
        hidden_error_msgs2 = []

    context = {
        "enquiry": enquiry,
        "subdivision": None,
        "fields": field_entries,
        "additional_values": additional_values,
        "preview_mode": "update" if mode == "update" else "create",
        "back_url": reverse("enquiries:detail", args=[enquiry.get("enquiry_id")]) if enquiry.get("enquiry_id") else reverse("enquiries:board"),
        "heading": f"Generate Share Link for Enquiry {enquiry.get('enquiry_id') or ''}",
        "column_matrix": _build_column_matrix_display(enquiry) if enquiry else {"columns": [], "rows": []},
        "hidden_errors": hidden_error_msgs2,
        "requires_pdf_upload": not _record_has_pdf(enquiry),
        "existing_pdf_name": _first_pdf_name(enquiry),
        "share_error": share_error,
    }
    return render(request, "enquiries/preview.html", context)


def _normalize_share_value(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, list):
        normalized_list = []
        for item in value:
            normalized_item = _normalize_share_value(item)
            if normalized_item in (None, ""):
                continue
            normalized_list.append(normalized_item)
        return normalized_list
    if isinstance(value, dict):
        normalized_dict: Dict[str, Any] = {}
        for key, item in value.items():
            normalized_item = _normalize_share_value(item)
            if normalized_item in (None, ""):
                continue
            normalized_dict[str(key)] = normalized_item
        return normalized_dict
    return value


def _collect_share_values_from_enquiry(enquiry: Mapping[str, Any] | None) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    if not enquiry:
        return values

    for name in forms.EnquiryUpdateForm.base_fields.keys():
        if name in PREVIEW_SKIP_FIELDS or name not in enquiry:
            continue
        normalized_value = _normalize_share_value(enquiry.get(name))
        if normalized_value in (None, "", [], {}):
            continue
        values[name] = normalized_value

    conversion_rate = _normalize_share_value(enquiry.get("conversion_rate"))
    if conversion_rate not in (None, "", [], {}) and "conversion_rate" not in values:
        values["conversion_rate"] = conversion_rate

    return values


def _build_share_url(request: HttpRequest, token: str, *, enq_no: str = "enquiry") -> str:
    enq_no_slug = slugify(str(enq_no or "enquiry")).upper() or "ENQUIRY"
    share_id = secrets.token_urlsafe(9)
    try:
        ShareLink.objects.create(id=share_id, token=token, brand="magicwand", enq_no=enq_no_slug)
    except Exception:
        share_id = None

    if share_id:
        try:
            return request.build_absolute_uri(
                reverse("enquiries:share_pretty", args=["magicwand", enq_no_slug, share_id])
            )
        except Exception:
            pass
    return request.build_absolute_uri(f"{reverse('enquiries:share')}?t={token}")


def _create_share_link_from_values(
    request: HttpRequest,
    values: Mapping[str, Any] | None,
    *,
    share_password: str,
    enquiry_id: str = "",
    pdf_bytes: bytes,
    pdf_filename: str = "enquiry.pdf",
    pdf_content_type: str = "application/pdf",
) -> Dict[str, str]:
    share_password = (share_password or "").strip()
    if not share_password:
        raise ValueError("Please provide a password to protect the share link.")
    if not pdf_bytes:
        raise ValueError("Upload a PDF to generate the share link.")

    share_payload = dict(values or {})
    share_payload.update(
        {
            "pdf_filename": pdf_filename or "enquiry.pdf",
            "pdf_content_b64": base64.b64encode(pdf_bytes).decode("ascii"),
            "pdf_content_type": pdf_content_type or "application/pdf",
            "requires_password": True,
            "password_hash": make_password(share_password),
        }
    )
    if enquiry_id:
        share_payload["enquiry_id"] = enquiry_id

    token = signing.dumps(share_payload, salt="enquiry-share", compress=True)
    share_url = _build_share_url(
        request,
        token,
        enq_no=str((values or {}).get("enq_number") or enquiry_id or "enquiry"),
    )
    return {
        "share_url": share_url,
        "share_password": share_password,
        "token": token,
    }


SHARE_PASSWORD_PREFIX = "AG-"
SHARE_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"  # no 0/O, 1/l/I
SHARE_PASSWORD_LENGTH = 8  # characters after the prefix


def generate_share_password() -> str:
    """A different password for every share link: AG- plus 8 random letters and digits, e.g. AG-hmD52xna."""
    return SHARE_PASSWORD_PREFIX + "".join(
        secrets.choice(SHARE_PASSWORD_ALPHABET) for _ in range(SHARE_PASSWORD_LENGTH))


@login_required
def generate_link(request: HttpRequest) -> HttpResponse:
    """Generate a signed shareable link with selected fields.

    Accepts the same POST payload as the preview form but returns a page
    with a generated URL for sharing instead of a PDF.
    """
    mode = request.POST.get("mode") or "create"
    if mode == "subdivision":
        form_class = forms.SubdivisionForm
    elif mode == "update":
        form_class = forms.EnquiryUpdateForm
    else:
        form_class = forms.EnquiryCreateForm

    form = form_class(request.POST, request.FILES)
    # Skip strict validation rules (matrix counts, conversion rate, etc.) for sharing
    setattr(form, "skip_strict_validation", True)
    # Soften hidden JSON matrix fields by bypassing strict JSON parsing during share
    for col_field in ("extrusion_die_columns", "mold_columns"):
        if col_field in form.fields:
            try:
                form.fields[col_field] = django_forms.CharField(
                    required=False, widget=django_forms.HiddenInput()
                )
            except Exception:
                pass
    # Relax requirements since this is a share flow (no strict validation)
    for name in (
        "customer_name",
        "subject",
        "currency",
        "channel",
        "priority",
        "description",
        "assigned_to",
        "customer_reference",
        # Ensure workflow field does not block sharing
        "status",
    ):
        if name in form.fields:
            form.fields[name].required = False
    if not form.is_valid():
        logger.warning(
            "generate_link: invalid form (mode=%s, enquiry_id=%s) errors=%s",
            mode,
            request.POST.get("enquiry_id") or request.POST.get("parent_enquiry_id") or "",
            str(form.errors),
        )
        return _render_preview_response(request, form, mode, error_message="Please correct the highlighted errors and try again.")

    # The preview page fills in a new password for each link; if it still arrives empty, create one here.
    share_password = (request.POST.get("share_password") or "").strip() or generate_share_password()

    api_root = _get_api_root()
    enquiry_id = request.POST.get("enquiry_id") or request.POST.get("parent_enquiry_id") or ""
    existing_enquiry: Dict[str, Any] = {}
    if enquiry_id:
        try:
            existing_enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request)
        except services.EnquiryServiceError:
            existing_enquiry = {"enquiry_id": enquiry_id}
    subdivision_id = request.POST.get("subdivision_id") or ""
    existing_subdivision: Dict[str, Any] = {}
    if mode == "subdivision" and enquiry_id and subdivision_id:
        try:
            existing_subdivision = services.fetch_subdivision(api_root, enquiry_id, subdivision_id, request=request)
        except services.EnquiryServiceError:
            existing_subdivision = {"subdivision_id": subdivision_id, "parent_enquiry_id": enquiry_id}

    pdf_file = request.FILES.get("share_pdf")
    pdf_bytes: Optional[bytes] = None
    pdf_filename = "enquiry.pdf"
    pdf_content_type = "application/pdf"
    if pdf_file:
        try:
            pdf_bytes = pdf_file.read()
        except Exception:
            return _render_preview_response(request, form, mode, error_message="Unable to read the uploaded PDF.", share_error="pdf")
        if not pdf_bytes:
            return _render_preview_response(request, form, mode, error_message="Uploaded PDF appears to be empty.", share_error="pdf")
        pdf_filename = pdf_file.name or pdf_filename
        pdf_content_type = pdf_file.content_type or pdf_content_type
    elif mode == "subdivision" and enquiry_id and subdivision_id:
        download = _download_primary_pdf(
            api_root,
            enquiry_id,
            existing_subdivision or {},
            request,
            subdivision_id=subdivision_id,
        )
        if download:
            pdf_bytes, pdf_filename, pdf_content_type = download
    elif enquiry_id:
        download = _download_primary_pdf(api_root, enquiry_id, existing_enquiry, request)
        if download:
            pdf_bytes, pdf_filename, pdf_content_type = download

    if not pdf_bytes:
        return _render_preview_response(
            request,
            form,
            mode,
            error_message="Upload a PDF to generate the share link.",
            share_error="pdf",
        )

    # Determine selected fields to include
    include_values = request.POST.getlist("include_fields")
    included_fields = set(include_values) if include_values else {
        name for name in form.fields.keys() if name not in PREVIEW_SKIP_FIELDS
    }
    # Build compact payload of selected values, normalising numbers/dates
    if hasattr(form, "cleaned_payload"):
        normalised = form.cleaned_payload(include_nulls=False)
    else:
        normalised = form.build_payload()
    values: Dict[str, Any] = {}
    for name in included_fields:
        if name in normalised:
            values[name] = normalised[name]
    # Include only conversion-related metadata needed for internal reporting;
    # avoid embedding the currency code directly into the shared token.
    for extra in ("conversion_rate",):
        if extra in normalised:
            values.setdefault(extra, normalised[extra])
    share_result = _create_share_link_from_values(
        request,
        values,
        share_password=share_password,
        enquiry_id=enquiry_id,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
        pdf_content_type=pdf_content_type,
    )

    context = {
        "share_url": share_result["share_url"],
        "included": sorted(included_fields),
        "share_password": share_result["share_password"],
    }
    # "Send to customer": email the link + password with the quotation PDF and chosen documents attached.
    record = ShareLink.objects.filter(token=share_result.get("token") or "").first() if enquiry_id else None
    if record is not None:
        try:
            _, share_data = share_email.load_share(record.id)
            context["email"] = share_email.form_defaults(
                request, record, share_data, share_result["share_password"], existing_enquiry or {})
        except share_email.ShareEmailError:
            logger.warning("generate_link: could not prepare the customer email for %s", enquiry_id)
    return render(request, "enquiries/link_generated.html", context)


def _thread_reply_target(gmail: GmailService, thread: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """The latest message of the enquiry's known thread, to reply to; None when there is no usable thread."""
    thread_id = str((thread or {}).get("thread_id") or "")
    if not thread_id:
        return None
    try:
        messages_in_thread = gmail.get_thread_messages(thread_id, limit=50) or []
        if not messages_in_thread:
            return None
        original = gmail.get_message(messages_in_thread[-1].get("id"))
    except GmailAPIError:
        return None
    if not original:
        return None
    return {**original, "thread_id": original.get("thread_id") or thread_id}


@login_required
@require_POST
def send_share_email(request: HttpRequest) -> HttpResponse:
    """Email the quotation share link (with its password) to the customer from the employee's Gmail."""
    share_id = (request.POST.get("share_id") or "").strip()
    password = (request.POST.get("share_password") or "").strip()
    try:
        record, data = share_email.load_share(share_id)
    except share_email.ShareEmailError as exc:
        messages.error(request, str(exc))
        return redirect("enquiries:board")
    if not share_email.password_matches(data, password):
        messages.error(request, "This share link could not be verified. Please create it again.")
        return redirect("enquiries:board")
    enquiry_id = str(data.get("enquiry_id") or "")
    api_root = _get_api_root()
    try:
        enquiry = services.fetch_enquiry(api_root, enquiry_id, request=request) if enquiry_id else {}
    except services.EnquiryServiceError:
        enquiry = {"enquiry_id": enquiry_id}

    email = share_email.form_defaults(request, record, data, password, enquiry)
    document = request.POST.get("document") or "quotation"
    email.update({
        "document": document if document in share_email.DOCUMENTS else "quotation",
        "to": (request.POST.get("to") or "").strip(),
        "cc": (request.POST.get("cc") or "").strip(),
        "subject": (request.POST.get("subject") or "").strip(),
        "body": (request.POST.get("body") or "").strip(),
        "selected": request.POST.getlist("documents"),
    })
    context = {"share_url": share_email.share_url(request, record), "share_password": password, "email": email}
    try:
        if not enquiry_id:
            raise share_email.ShareEmailError("This share link is not linked to an enquiry.")
        to, cc = share_email.validate_recipients(email["to"], email["cc"])
        if not email["subject"] or not email["body"]:
            raise share_email.ShareEmailError("Please fill in the subject and the message.")
        attachments = [share_email.quotation_attachment(data, enquiry_id, email.get("voucher_no") or "")]
        attachments += share_email.download_documents(api_root, enquiry_id, email["documents"], email["selected"],
                                                      request=request)
        share_email.check_size(attachments)
        gmail = GmailService(request.user)
        original = _thread_reply_target(gmail, email.get("thread"))
        if original is not None:
            # Same conversation as the earlier quotation / follow-ups: reply in that thread.
            result = gmail.send_reply(original_message=original, body=email["body"],
                                      subject=original.get("subject") or email["subject"], to=to, cc=cc or None,
                                      attachments=attachments)
        else:
            result = gmail.send_email(to, email["subject"], email["body"], cc=cc or None, attachments=attachments)
    except share_email.ShareEmailError as exc:
        context["email_error"] = str(exc)
        return render(request, "enquiries/link_generated.html", context)
    except GmailAPIError as exc:
        context["email_error"] = f"Gmail could not send the email: {exc}. Please sign in again and retry."
        return render(request, "enquiries/link_generated.html", context)

    share_email.remember_recipients(enquiry_id, to, cc, request.user)
    names = ", ".join(a["filename"] for a in attachments)
    copy = f" (CC: {', '.join(cc)})" if cc else ""
    what = share_email.DOCUMENTS[email["document"]]
    where = " in the existing email thread" if original is not None else ""
    if result.get("local_demo"):
        context["email_sent"] = f"Test mode: the email to {to}{copy} was prepared but not sent. Attachments: {names}."
    else:
        from . import auto_follow_up
        auto_follow_up.record_start(enquiry_id, what["stage"], sent=result, customer_email=to, cc=cc,
                                    subject=original.get("subject") if original is not None else email["subject"],
                                    user=request.user, document_no=email.get("voucher_no") or "")
        if getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False):
            gap = (list(getattr(settings, "FOLLOW_UP_AUTO_GAP_DAYS", []) or [2]))[0]
            then = (f" Automatic follow-ups will reply in this same email thread every {gap} days until the "
                    "customer answers.")
        else:
            then = " Automatic follow-ups are turned off on this site, so please follow up from the enquiry board."
        context["email_sent"] = f"{what['label']} emailed to {to}{copy}{where}. Attachments: {names}.{then}"
    logger.info("%s share email for %s sent by %s to %s", what["label"], enquiry_id, request.user, to)
    return render(request, "enquiries/link_generated.html", context)


def share(request: HttpRequest, brand: str | None = None, enq_no: str | None = None, token: str | None = None) -> HttpResponse:
    """Public-ish share page that renders selected fields with branding."""
    token = token or request.GET.get("t") or ""
    # If pretty URL used, token may actually be an opaque share id. Resolve via DB if present.
    if brand and enq_no and token and ":" not in token:
        try:
            record = ShareLink.objects.filter(id=token).first()
            if record:
                token = record.token
        except Exception:
            # Leave token as-is; fallback below will handle BadSignature
            pass
    if not token:
        messages.error(request, "Missing share token.")
        return redirect("enquiries:board")
    try:
        data = signing.loads(token, salt="enquiry-share")
    except signing.BadSignature:
        messages.error(request, "Invalid or expired share link.")
        return redirect("enquiries:board")

    api_root = _get_api_root()
    enquiry_status: Optional[str] = None
    enquiry_id_value = data.get("enquiry_id")
    if enquiry_id_value:
        try:
            current_enquiry = services.fetch_enquiry(api_root, enquiry_id_value, request=request)
            enquiry_status = str(current_enquiry.get("status") or "").lower() or None
        except services.EnquiryServiceError:
            enquiry_status = None

    password_hash = data.get("password_hash")
    requires_password = bool(password_hash)
    password_valid = False
    password_error: Optional[str] = None
    if requires_password:
        if request.method == "POST":
            entered_password = request.POST.get("share_password") or ""
            if check_password(entered_password, password_hash):
                password_valid = True
            else:
                password_error = "Incorrect password."
        if not password_valid:
            context = {
                "requires_password": True,
                "error": password_error,
                "form_action": request.get_full_path(),
            }
            return render(request, "enquiries/share_password.html", context)

    data.pop("password_hash", None)

    # Prepare display list using labels
    items: List[Dict[str, Any]] = []
    for key, value in data.items():
        if key in {"currency", "conversion_rate", "pdf_content_b64", "pdf_filename", "pdf_content_type", "requires_password"}:
            continue
        label = DETAIL_FIELD_LABELS.get(key, key.replace("_", " ").title())
        items.append({"key": key, "label": label, "value": value})

    context = {
        "items": items,
        "currency": data.get("currency"),
        "conversion_rate": data.get("conversion_rate"),
        "pdf_content_b64": data.get("pdf_content_b64"),
        "pdf_filename": data.get("pdf_filename"),
        "pdf_content_type": data.get("pdf_content_type") or "application/pdf",
        "share_token": token,
        "enquiry_id": data.get("enquiry_id"),
        "enquiry_status": enquiry_status,
    }
    return render(request, "enquiries/share.html", context)


_SHARE_REASON_MARKERS = {
    CustomerStatusEmail.KIND_REJECTION: "Rejection reason (via share link):",
}


def _share_reason(enquiry: Mapping[str, Any] | None, *, kind: str) -> str:
    """What the customer typed on the share link; other remarks are internal notes and do not count."""
    marker = _SHARE_REASON_MARKERS[kind]
    remarks = str((enquiry or {}).get("remarks") or "")
    return next((line.split(marker, 1)[1].strip() for line in reversed(remarks.splitlines()) if marker in line), "")


def _after_status_change(request: HttpRequest, enquiry_id: str, enquiry: Mapping[str, Any], new_status: Any,
                         lost_reason: str = "") -> None:
    """Staff changed the status: email the customer when an order becomes rejected."""
    old_status = str((enquiry or {}).get("status") or "").strip().lower()
    new_status = str(new_status or "").strip().lower()
    if new_status:
        negotiation.log_status_change(getattr(request, "user", None), enquiry_id, old_status, new_status,
                                      lost_reason=lost_reason)
    if old_status == CONVERTED_STATUS and new_status and new_status != CONVERTED_STATUS:
        if _payment_status(enquiry_id) == EnquiryContact.PAYMENT_PAID:
            _save_payment(enquiry_id, EnquiryContact.PAYMENT_PENDING, request.user, keep_details=True)
    if new_status != "rejected" or old_status == "rejected":
        return
    reason = _share_reason(enquiry, kind=CustomerStatusEmail.KIND_REJECTION)
    outcome = status_emails.send_status_email(CustomerStatusEmail.KIND_REJECTION, enquiry_id, enquiry or {}, reason=reason)
    _report_status_email(request, outcome, "rejection")


def _report_status_email(request: HttpRequest, outcome: Mapping[str, Any], label: str) -> None:
    result = outcome.get("result")
    if result == CustomerStatusEmail.STATUS_SENT:
        messages.success(request, f"The {label} email was sent to the customer.")
    elif result == CustomerStatusEmail.STATUS_DRY_RUN:
        messages.info(request, f"Test mode: the {label} email was prepared but not sent.")
    elif result in (CustomerStatusEmail.STATUS_SKIPPED, CustomerStatusEmail.STATUS_FAILED):
        messages.warning(request, f"The {label} email was not sent: {outcome.get('note')}.")


def _append_remark(existing: Mapping[str, Any] | None, line: str) -> str:
    existing_remarks = str((existing or {}).get("remarks") or "").strip()
    return f"{existing_remarks}\n{line}" if existing_remarks else line


def share_action(request: HttpRequest) -> HttpResponse:
    """Customer's answer on the share link: accept, or reject with a reason."""
    token = request.POST.get("token") or ""
    action = (request.POST.get("action") or "").lower()
    if action not in {"accept", "reject"}:
        return JsonResponse({"error": "Unknown action."}, status=400)
    if not token:
        return JsonResponse({"error": "Missing share token."}, status=400)
    try:
        data = signing.loads(token, salt="enquiry-share")
    except signing.BadSignature:
        return JsonResponse({"error": "Invalid or expired share token."}, status=400)
    enquiry_id = data.get("enquiry_id")
    if not enquiry_id:
        return JsonResponse({"error": "Share link is missing an enquiry reference."}, status=400)

    api_root = _get_api_root()
    reason = (request.POST.get("reason") or "").strip()
    try:
        existing = services.fetch_enquiry(api_root, enquiry_id, request=request)
    except services.EnquiryServiceError:
        existing = None

    status_value = "accepted" if action == "accept" else "rejected"
    update_payload: Dict[str, Any] = {"status": status_value}
    if action == "reject" and reason:
        update_payload["remarks"] = _append_remark(existing, f"Rejection reason (via share link): {reason}")
    try:
        services.update_enquiry(api_root, enquiry_id, update_payload, request=request)
    except services.EnquiryServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if action == "reject":
        status_emails.send_status_email(CustomerStatusEmail.KIND_REJECTION, enquiry_id, existing or {}, reason=reason)

    return JsonResponse({"status": "ok", "enquiry_id": enquiry_id, "new_status": status_value})


@login_required
def preview_pdf(request: HttpRequest) -> HttpResponse:
    api_root = _get_api_root()
    mode = request.POST.get("mode") or "create"
    enquiry_id = request.POST.get("enquiry_id") or request.POST.get("parent_enquiry_id")
    subdivision_id = request.POST.get("subdivision_id") or None

    if mode == "subdivision":
        form_class = forms.SubdivisionForm
    elif mode == "update":
        form_class = forms.EnquiryUpdateForm
    else:
        form_class = forms.EnquiryCreateForm

    form = form_class(request.POST, request.FILES)
    if "customer_name" in form.fields:
        form.fields["customer_name"].required = False
    if "subject" in form.fields:
        form.fields["subject"].required = False
    if "currency" in form.fields:
        form.fields["currency"].required = False
    if "attachments" in form.fields:
        form.fields["attachments"].label = "Attach additional files"
    if "profile" in form.fields:
        form.fields["profile"].label = "Profile"
    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    if hasattr(form, "cleaned_payload"):
        payload = form.cleaned_payload(include_nulls=False)
    else:
        payload = form.build_payload()
    profile_file = form.cleaned_data.get("profile") if "profile" in form.cleaned_data else None
    attachments = form.cleaned_data.get("attachments") if "attachments" in form.cleaned_data else []

    save_flag = request.POST.get("save") in {"true", "1", "on"}
    displayed_values = request.POST.getlist("all_fields")
    displayed_fields = set(displayed_values)
    fallback_display = False
    if not displayed_fields:
        displayed_fields = {name for name in form.fields.keys() if name not in PREVIEW_SKIP_FIELDS}
        fallback_display = True
    include_values = request.POST.getlist("include_fields")
    if include_values:
        included_fields = set(include_values)
    elif fallback_display:
        included_fields = set(displayed_fields)
    else:
        included_fields = set()

    if mode == "subdivision":
        if not enquiry_id or not subdivision_id:
            return JsonResponse({"error": "Missing sub-enquiry identifier"}, status=400)
        try:
            existing = services.fetch_subdivision(api_root, enquiry_id, subdivision_id, request=request)
        except services.EnquiryServiceError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        if save_flag:
            try:
                existing = services.update_subdivision(
                    api_root,
                    enquiry_id,
                    subdivision_id,
                    payload,
                    request=request,
                )
            except services.EnquiryServiceError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            try:
                pdf_bytes = generate_preview_pdf(
                    payload,
                    profile_file=profile_file,
                    general_files=attachments or [],
                    existing=existing,
                    download_attachment=lambda path: services.download_subdivision_attachment(
                        api_root, enquiry_id, subdivision_id, path.split("/")[-1], request=request
                    ),
                    included_fields=included_fields,
                    displayed_fields=displayed_values or None,
                )
            except ValueError as exc:
                return JsonResponse({"error": str(exc)}, status=400)
            except Exception:
                logging.getLogger(__name__).exception("Unable to render preview PDF")
                return JsonResponse({"error": "Unable to render preview PDF."}, status=500)
            filename = f"{existing.get('subdivision_id', subdivision_id)}-quotation.pdf"
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

        try:
            pdf_bytes = generate_preview_pdf(
                payload,
                profile_file=profile_file,
                general_files=attachments or [],
                existing=existing,
                download_attachment=lambda path: services.download_subdivision_attachment(
                    api_root, enquiry_id, subdivision_id, path.split("/")[-1], request=request
                ),
                included_fields=included_fields,
                displayed_fields=displayed_values or None,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logging.getLogger(__name__).exception("Unable to render preview PDF")
            return JsonResponse({"error": "Unable to render preview PDF."}, status=500)
        return HttpResponse(pdf_bytes, content_type="application/pdf")

    # Enquiry preview
    existing = None
    if enquiry_id:
        try:
            existing = services.fetch_enquiry(api_root, enquiry_id, request=request)
        except services.EnquiryServiceError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    if save_flag and enquiry_id:
        before_save = existing
        blocked = _conversion_block_reason(enquiry_id, (existing or {}).get("status"), payload.get("status"))
        if blocked:
            return JsonResponse({"error": blocked}, status=400)
        try:
            existing = services.update_enquiry(
                api_root,
                enquiry_id,
                payload,
                request=request,
            )
        except services.EnquiryServiceError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        _after_status_change(request, enquiry_id, before_save or {}, payload.get("status"))
        try:
            pdf_bytes = generate_preview_pdf(
                payload,
                profile_file=profile_file,
                general_files=attachments or [],
                existing=existing,
                download_attachment=lambda path: services.download_attachment(api_root, path, request=request),
                included_fields=included_fields,
                displayed_fields=displayed_values or None,
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logging.getLogger(__name__).exception("Unable to render preview PDF")
            return JsonResponse({"error": "Unable to render preview PDF."}, status=500)
        filename = f"{existing.get('enquiry_id', enquiry_id)}-quotation.pdf"
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    try:
        pdf_bytes = generate_preview_pdf(
            payload,
            profile_file=profile_file,
            general_files=attachments or [],
            existing=existing,
            download_attachment=lambda path: services.download_attachment(api_root, path, request=request),
            included_fields=included_fields,
            displayed_fields=displayed_fields,
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        logging.getLogger(__name__).exception("Unable to render preview PDF")
        return JsonResponse({"error": "Unable to render preview PDF."}, status=500)

    return HttpResponse(pdf_bytes, content_type="application/pdf")

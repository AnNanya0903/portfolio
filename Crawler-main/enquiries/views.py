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
from django.views.decorators.http import require_POST
from django.utils import timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from allauth.socialaccount.models import SocialAccount

from . import forms, services
from .models import GmailLeadAnalysis, ShareLink
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

    range_start: Optional[datetime] = None
    range_end: Optional[datetime] = None
    if start_date:
        parsed_start = date.fromisoformat(start_date)
        range_start = timezone.make_aware(
            datetime.combine(parsed_start, datetime.min.time()),
            timezone.get_current_timezone(),
        )
    if end_date:
        parsed_end = date.fromisoformat(end_date)
        range_end = timezone.make_aware(
            datetime.combine(parsed_end, datetime.max.time()),
            timezone.get_current_timezone(),
        )
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
    if isinstance(value, timezone.datetime):
        return value
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
            return services.download_attachment(api_root, download_url, request=request)
        except services.EnquiryServiceError:
            continue
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
        else:
            messages.error(request, "Unknown action requested.")
        return redirect("enquiries:board")

    query_raw = (request.GET.get("q") or "").strip()
    query = query_raw.lower()
    fin_year = (request.GET.get("fin_year") or "").strip()
    month = (request.GET.get("month") or "").strip()
    status = (request.GET.get("status") or "").strip()

    if status and status not in STATUS_VALUE_SET:
        status = ""

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

    total_count = len(enquiries)
    total_pages = max(1, int(math.ceil(total_count / page_size))) if page_size else 1
    if page > total_pages:
        page = total_pages

    start_index = (page - 1) * page_size
    end_index = min(total_count, start_index + page_size)
    enquiries_page = enquiries[start_index:end_index]

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

    query = _build_enquiry_follow_up_query(enquiry)
    if not query:
        messages.error(request, "This enquiry does not have enough subject information to prepare a Gmail follow-up.")
        return redirect("enquiries:board")

    gmail_service = GmailService(request.user)
    try:
        matches = gmail_service.search_messages(query, limit=1)
    except GmailAPIError as exc:
        messages.error(request, str(exc))
        return redirect("enquiries:board")

    if not matches:
        messages.error(request, "No Gmail thread matched this enquiry subject for follow-up.")
        return redirect("enquiries:board")

    message_id = str((matches[0] or {}).get("id") or "").strip()
    if not message_id:
        messages.error(request, "Matched Gmail thread is missing its message identifier.")
        return redirect("enquiries:board")

    # Store follow-up context so the compose page can record history after send.
    request.session["follow_up_context"] = {
        "enquiry_id": enquiry_id,
        "customer_email": str(enquiry.get("customer_email") or "").strip(),
        "customer_name": str(enquiry.get("customer_name") or "").strip(),
        "subject": str(enquiry.get("subject") or "").strip(),
        "gmail_message_id": message_id,
    }
    request.session.modified = True

    return redirect(reverse("dashboard:compose_email", args=[message_id]))


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
        "customer_name": sheet_title,
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
    else:
        pdf_name = (getattr(pdf_file, "name", "") or "").lower()
        if not pdf_name.endswith(".pdf"):
            raise ValueError("Please upload a PDF file.")

    resolved_title = title or pdf_file.name.rsplit(".", 1)[0] or "PDF Enquiry"
    today = timezone.now().date().isoformat()

    default_payload = {
        "subject": resolved_title,
        "customer_name": resolved_title,
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
    return {
        "title": resolved_title,
        "enquiry_id": enquiry_id,
        "enquiry_detail_url": reverse("enquiries:detail", args=[enquiry_id]) if enquiry_id else None,
    }


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
        if form.is_valid():
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

    context = {
        "summary": data,
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
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
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
    }


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

    share_password = (request.POST.get("share_password") or "").strip()
    if not share_password:
        return _render_preview_response(request, form, mode, error_message="Please provide a password to protect the share link.", share_error="password")

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


def share_action(request: HttpRequest) -> HttpResponse:
    token = request.POST.get("token") or ""
    action = (request.POST.get("action") or "").lower()
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or reverse("enquiries:board")
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

    status_value = "accepted" if action == "accept" else "rejected"
    api_root = _get_api_root()
    update_payload: Dict[str, Any] = {"status": status_value}
    if action == "reject":
        reason = (request.POST.get("reason") or "").strip()
        if reason:
            try:
                existing = services.fetch_enquiry(api_root, enquiry_id, request=request)
                existing_remarks = str(existing.get("remarks") or "").strip()
            except services.EnquiryServiceError:
                existing_remarks = ""
            if existing_remarks:
                new_remarks = f"{existing_remarks}\nRejection reason (via share link): {reason}"
            else:
                new_remarks = f"Rejection reason (via share link): {reason}"
            update_payload["remarks"] = new_remarks
    try:
        services.update_enquiry(api_root, enquiry_id, update_payload, request=request)
    except services.EnquiryServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

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
        try:
            existing = services.update_enquiry(
                api_root,
                enquiry_id,
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

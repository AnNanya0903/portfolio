"""Database persistence layer for the enquiry service."""

from __future__ import annotations

import json
import logging
import os
import re
import string
import time
from datetime import datetime, date, time as dt_time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from google.cloud.sql.connector import Connector  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Connector = None

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    inspect,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.sql import func

from models import (
    AttachmentMeta,
    BoardResponse,
    ColumnData,
    EmailIngestionEvent,
    Enquiry,
    Subdivision,
    SummaryHighlight,
    SummaryMetrics,
    SummaryResponse,
)

logger = logging.getLogger(__name__)

_metadata = MetaData()

_SUMMARY_CACHE: Dict[str, Tuple[float, SummaryResponse]] = {}


_SUMMARY_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _enquiry_count(count: int) -> str:
    """"1 enquiry", "3 enquiries"."""
    return f"{count} enquiry" if count == 1 else f"{count} enquiries"


def _normalize_summary_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _coerce_summary_date(value: Optional[str]) -> Optional[date]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Dates must use YYYY-MM-DD format.")


_INDIA_TIME = timezone(timedelta(hours=5, minutes=30))  # IST; India has no daylight saving


def _ist_day_edge_as_utc(day: date, edge: dt_time) -> datetime:
    """The start or end of an Indian day as a naive UTC time, which is how enquiry times are stored."""
    return datetime.combine(day, edge, tzinfo=_INDIA_TIME).astimezone(timezone.utc).replace(tzinfo=None)


def _summary_filter_key(period: str, start_date: Optional[str], end_date: Optional[str]) -> str:
    return f"{period}|{start_date or ''}|{end_date or ''}"


def _resolve_summary_date_range(
    period: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Tuple[str, Optional[datetime], Optional[datetime]]:
    selected_period = (period or "all").strip().lower()
    if selected_period not in {"all", "weekly", "monthly", "custom"}:
        raise ValueError("Unsupported summary period.")

    now = datetime.utcnow()
    if selected_period == "weekly":
        return selected_period, now - timedelta(days=7), now
    if selected_period == "monthly":
        return selected_period, now - timedelta(days=30), now
    if selected_period == "custom":
        parsed_start = _coerce_summary_date(start_date)
        parsed_end = _coerce_summary_date(end_date)
        if parsed_start is None and parsed_end is None:
            raise ValueError("Choose at least one custom date.")
        # Custom dates are Indian days (12:00 AM to 11:59 PM IST), like the times the report shows. The service
        # works in naive UTC, so the day's edges are converted; cutting at UTC midnight (5:30 AM IST) put
        # enquiries received between 12:00 AM and 5:30 AM IST on the wrong day.
        start_dt = _ist_day_edge_as_utc(parsed_start, dt_time.min) if parsed_start else None
        end_dt = _ist_day_edge_as_utc(parsed_end, dt_time.max) if parsed_end else None
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            raise ValueError("Custom end date must be on or after the start date.")
        return selected_period, start_dt, end_dt
    return "all", None, None


def _is_local_environment() -> bool:
    deploy_env = (os.getenv("DEPLOY_ENV") or "").strip().lower()
    if deploy_env in {"local", "dev", "development"}:
        return True
    debug_value = (os.getenv("DEBUG") or "").strip().lower()
    return debug_value in {"1", "true", "yes", "on"}


def _default_fallback_sqlite_path() -> Path:
    configured = (os.getenv("ENQUIRY_FALLBACK_SQLITE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    try:
        repo_root = Path(__file__).resolve().parents[2]
    except Exception:
        repo_root = Path.cwd()
    return (repo_root / "logs" / "local" / "local-stack.sqlite3").resolve()


def _use_sqlite_fallback_engine() -> Engine:
    global _engine
    sqlite_path = _default_fallback_sqlite_path()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    logger.warning(
        "Enquiry service falling back to SQLite storage (%s). Start Postgres/Docker or update ENQUIRY_DATABASE_URL to restore Postgres.",
        sqlite_path,
    )
    _engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    with _engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return _engine

# Require a delimiter (space/hyphen) after "AG" so words like "Agreement" don't become false-positive compounds.
# Also keep the "code family" prefix short to avoid accidental matches on long words.
_MATERIAL_AG_CODE_RE = re.compile(
    r"\bAG(?:[-\s]+)([A-Z]{1,4}(?:\s*[-]?\s*)\d{1,6}(?:\.\d+)?[A-Z]{0,2})\b",
    re.IGNORECASE,
)
_MATERIAL_AG_NUM_RE = re.compile(r"\bAG(?:[-\s]+)(\d{3,4})\b", re.IGNORECASE)
_MATERIAL_FRC_RE = re.compile(r"\b(?:AG[\s-]*)?FRC\s*[-]?\s*(\d{2,6})\b", re.IGNORECASE)
_MATERIAL_KNOWN_RE = re.compile(
    r"\b(EPDM|NBR|SBR|FKM|VITON|SILICONE|NEOPRENE|PTFE|TEFLON|GRAPHITE|GRAFOIL)\b",
    re.IGNORECASE,
)
_MATERIAL_NOISE_RE = re.compile(
    r"\b(AGREEMENT|AG-REEMENT|ENQUIRY|QUOTE|QUOTATION|INVOICE|CHARGE|CHARGES|TOTAL|AMOUNT|RATE|PRICE)\b",
    re.IGNORECASE,
)
_MATERIAL_AG_NUM_CANON_RE = re.compile(r"\bAG[\s-]*(\d{3,4})\b", re.IGNORECASE)


def _normalize_material_key(value: str) -> str:
    canonical = _material_display_name(value)
    trimmed = canonical.strip().upper()
    if not trimmed:
        return ""
    normalized = "".join(ch for ch in trimmed if ch.isalnum())
    if normalized.startswith("AG") and len(normalized) > 2:
        return normalized[2:]
    return normalized


def _format_frc_material(code: str) -> str:
    digits = re.sub(r"\D+", "", str(code or ""))
    if not digits:
        return ""
    return f"FRC {digits}"


def _canonicalize_material(value: str) -> str:
    trimmed = " ".join(str(value or "").strip().split())
    if not trimmed or trimmed.upper() in {"—", "N/A"}:
        return ""
    upper = trimmed.upper()

    known_match = _MATERIAL_KNOWN_RE.search(upper)
    known_material = known_match.group(1).upper() if known_match else ""

    frc_match = _MATERIAL_FRC_RE.search(upper)
    if frc_match:
        return _format_frc_material(frc_match.group(1))

    ag_num_match = _MATERIAL_AG_NUM_CANON_RE.search(upper)
    if ag_num_match:
        return f"AG-{ag_num_match.group(1)}"

    if known_material:
        return known_material

    if _MATERIAL_NOISE_RE.search(upper):
        return ""

    if upper.startswith("AG-"):
        compact = re.sub(r"\s+", "", upper)
        if 3 <= len(compact) <= 20:
            return compact
    return trimmed


def _material_display_name(value: str) -> str:
    return _canonicalize_material(value)


def _extract_material_from_text(text: str, *, allow_extended_ag_code: bool = False) -> Optional[str]:
    if not isinstance(text, str):
        return None
    haystack = " ".join(text.strip().split())
    if not haystack:
        return None

    match = _MATERIAL_FRC_RE.search(haystack)
    if match:
        material = _format_frc_material(match.group(1))
        if material:
            return material

    if allow_extended_ag_code:
        match = _MATERIAL_AG_CODE_RE.search(haystack)
        if match:
            code = match.group(1).strip()
            if code:
                return _material_display_name(f"AG-{code}")

    match = _MATERIAL_AG_NUM_RE.search(haystack)
    if match:
        code = match.group(1).strip()
        if code:
            return f"AG-{code}"

    match = _MATERIAL_KNOWN_RE.search(haystack)
    if match:
        return match.group(1).strip().upper()

    return None


def _attachment_looks_like_pdf(attachment: Any) -> bool:
    try:
        name = str(getattr(attachment, "file_name", "") or "").lower()
    except Exception:
        name = ""
    try:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
    except Exception:
        content_type = ""
    return name.endswith(".pdf") or "pdf" in content_type


def _pdf_max_pages() -> int:
    try:
        value = int(os.getenv("ENQUIRY_SUMMARY_PDF_MAX_PAGES", "3"))
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 12))


def _pdf_text_char_limit() -> int:
    try:
        value = int(os.getenv("ENQUIRY_SUMMARY_PDF_TEXT_LIMIT", "16000"))
    except (TypeError, ValueError):
        value = 16000
    return max(2000, min(value, 80000))


@lru_cache(maxsize=256)
def _read_pdf_text_cached(path_str: str, mtime_ns: int, size_bytes: int) -> str:
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return ""

    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""

    max_pages = _pdf_max_pages()
    char_limit = _pdf_text_char_limit()
    extracted_parts: List[str] = []

    for idx, page in enumerate(reader.pages):
        if idx >= max_pages:
            break
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            extracted_parts.append(page_text)
        if sum(len(part) for part in extracted_parts) >= char_limit:
            break

    combined = "\n".join(extracted_parts)
    combined = " ".join(combined.split())
    if len(combined) > char_limit:
        combined = combined[:char_limit]
    return combined


def _read_pdf_text(path_value: Optional[str]) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    try:
        stat = path.stat()
    except Exception:
        return ""
    return _read_pdf_text_cached(str(path), int(getattr(stat, "st_mtime_ns", 0)), int(stat.st_size))


def _summary_use_gemini() -> bool:
    use_gemini = os.getenv("ENQUIRY_SUMMARY_USE_GEMINI", "").strip().lower()
    return use_gemini in {"", "1", "true", "yes", "on"}


def _summary_model_candidates() -> List[str]:
    configured_model = (os.getenv("ENQUIRY_SUMMARY_MODEL") or "").strip()
    fallback_model = (os.getenv("GEMINI_MODEL") or "").strip()
    candidates = [name for name in (configured_model, fallback_model) if name]
    if "models/gemini-2.5-flash" not in candidates:
        candidates.append("models/gemini-2.5-flash")
    return candidates


def _summary_extract_gemini_text(resp: Any) -> str:
    try:
        return (resp.text or "").strip()
    except Exception:
        pass
    try:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            return ""
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        if not parts:
            return ""
        return str(getattr(parts[0], "text", "") or "").strip()
    except Exception:
        return ""


def _summary_parse_json_payload(text: str) -> Dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()

    try:
        payload_val = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            payload_val = json.loads(cleaned[start : end + 1], strict=False)
        else:
            raise

    if isinstance(payload_val, list):
        return {"items": [item for item in payload_val if isinstance(item, dict)]}
    if isinstance(payload_val, dict):
        # Some models return a single extracted object instead of {"items": [...]}
        if "items" not in payload_val and "enquiry_id" in payload_val:
            return {"items": [payload_val]}
        return payload_val
    raise RuntimeError("Gemini response was neither JSON object nor array")


def _summary_to_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(",", "")
        cleaned = re.sub(r"(?i)\b(INR|RS\.?|USD|EUR)\b", "", cleaned)
        cleaned = cleaned.replace("₹", "").replace("$", "").strip()
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def _iter_pdf_attachments_for_enquiry(enquiry: Enquiry) -> List[Any]:
    attachments: List[Any] = []
    attachments.extend(getattr(enquiry, "attachments", None) or [])
    for subdivision in getattr(enquiry, "subdivisions", None) or []:
        attachments.extend(getattr(subdivision, "attachments", None) or [])
    return [item for item in attachments if _attachment_looks_like_pdf(item)]


def _collect_pdf_text_for_enquiry(enquiry: Enquiry) -> str:
    try:
        total_limit = int(os.getenv("ENQUIRY_SUMMARY_GEMINI_PDF_TEXT_LIMIT", "6000"))
    except (TypeError, ValueError):
        total_limit = 6000
    try:
        per_file_limit = int(os.getenv("ENQUIRY_SUMMARY_GEMINI_PDF_FILE_LIMIT", "1500"))
    except (TypeError, ValueError):
        per_file_limit = 1500

    total_limit = max(2000, min(total_limit, 120000))
    per_file_limit = max(500, min(per_file_limit, total_limit))

    chunks: List[str] = []
    current_size = 0
    for attachment in _iter_pdf_attachments_for_enquiry(enquiry):
        file_name = str(getattr(attachment, "file_name", "") or "").strip()
        text = _read_pdf_text(getattr(attachment, "storage_path", None))
        if not text:
            continue
        snippet = text[:per_file_limit]
        prefix = f"[{file_name}] " if file_name else ""
        line = f"{prefix}{snippet}".strip()
        if not line:
            continue
        remaining = total_limit - current_size
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining]
        chunks.append(line)
        current_size += len(line)
        if current_size >= total_limit:
            break
    return "\n".join(chunks).strip()


def _extract_pdf_insights_with_gemini(enquiries: List[Enquiry]) -> Dict[str, Dict[str, Any]]:
    if not enquiries or not _summary_use_gemini():
        return {}

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {}

    try:
        max_enquiries = int(os.getenv("ENQUIRY_SUMMARY_GEMINI_MAX_ENQUIRIES", "120"))
    except (TypeError, ValueError):
        max_enquiries = 120
    try:
        batch_size = int(os.getenv("ENQUIRY_SUMMARY_GEMINI_BATCH_SIZE", "8"))
    except (TypeError, ValueError):
        batch_size = 8
    try:
        text_limit = int(os.getenv("ENQUIRY_SUMMARY_GEMINI_PROMPT_TEXT_LIMIT", "900"))
    except (TypeError, ValueError):
        text_limit = 900

    max_enquiries = max(10, min(max_enquiries, 1000))
    batch_size = max(2, min(batch_size, 20))
    text_limit = max(400, min(text_limit, 8000))

    entries: List[Dict[str, Any]] = []
    for enquiry in enquiries[:max_enquiries]:
        pdf_text = _collect_pdf_text_for_enquiry(enquiry)
        if not pdf_text:
            continue
        entries.append(
            {
                "enquiry_id": enquiry.enquiry_id,
                "customer": enquiry.customer_name,
                "subject": enquiry.subject,
                "pdf_text": pdf_text[:text_limit],
            }
        )

    if not entries:
        return {}

    try:
        import google.generativeai as genai  # type: ignore
    except Exception:
        return {}

    try:
        genai.configure(api_key=api_key)
    except Exception:
        return {}

    generation_config_json = genai.types.GenerationConfig(
        temperature=0.1,
        max_output_tokens=3200,
        response_mime_type="application/json",
    )
    generation_config_text = genai.types.GenerationConfig(
        temperature=0.1,
        max_output_tokens=3200,
    )

    model_candidates = _summary_model_candidates()
    results: Dict[str, Dict[str, Any]] = {}

    for idx in range(0, len(entries), batch_size):
        batch = entries[idx : idx + batch_size]
        payload = {"items": batch}
        prompt = (
            "You are reading Avigiri enquiry PDFs.\n"
            "For each item, extract:\n"
            "- material: primary material or compound code (e.g. EPDM, AG-7020, FRC 7060).\n"
            "- price_per_piece: numeric final per-piece/pcs price when present.\n"
            "- price_per_kg: numeric per-kg price when present.\n"
            "- price_per_meter: numeric rate per meter/MTR when present.\n"
            "Rules:\n"
            "- Use only values clearly present in the provided PDF text.\n"
            "- Prefer explicit material labels; ignore unrelated document codes and part numbers.\n"
            "- Do NOT output words like agreement/charges/quotation headings as material.\n"
            "- If value is missing, use null.\n"
            "- Keep enquiry_id exactly unchanged.\n"
            "Return strict JSON only in this format:\n"
            "{\"items\":[{\"enquiry_id\":\"...\",\"material\":null|string,"
            "\"price_per_piece\":null|number,\"price_per_kg\":null|number,"
            "\"price_per_meter\":null|number}]}\n\n"
            f"DATA:\n{json.dumps(payload, ensure_ascii=False)}"
        )

        parsed_items: List[Dict[str, Any]] = []
        last_error: Optional[Exception] = None
        for model_name in model_candidates:
            try:
                model: Any = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt, generation_config=generation_config_json)
                raw_text = _summary_extract_gemini_text(response)
                if not raw_text:
                    raise RuntimeError("Empty Gemini response")
                parsed = _summary_parse_json_payload(raw_text)
                raw_items = parsed.get("items")
                if not isinstance(raw_items, list):
                    raise RuntimeError("Gemini response missing items")
                parsed_items = [item for item in raw_items if isinstance(item, dict)]
                break
            except Exception as exc:
                last_error = exc
                try:
                    response = model.generate_content(prompt, generation_config=generation_config_text)
                    raw_text = _summary_extract_gemini_text(response)
                    if not raw_text:
                        raise RuntimeError("Empty Gemini response")
                    parsed = _summary_parse_json_payload(raw_text)
                    raw_items = parsed.get("items")
                    if not isinstance(raw_items, list):
                        raise RuntimeError("Gemini response missing items")
                    parsed_items = [item for item in raw_items if isinstance(item, dict)]
                    break
                except Exception as exc2:
                    last_error = exc2
                    continue

        if not parsed_items:
            if last_error is not None:
                logger.warning("enquiry_summary_pdf_gemini_failed batch=%s err=%s", idx // batch_size, last_error)
            continue

        for item in parsed_items:
            enquiry_id = str(item.get("enquiry_id") or "").strip()
            if not enquiry_id:
                continue
            material = (
                _extract_material_from_text(item.get("material"), allow_extended_ag_code=False)
                if isinstance(item.get("material"), str)
                else ""
            )
            results[enquiry_id] = {
                "material": material or None,
                "price_per_piece": _summary_to_float(item.get("price_per_piece")),
                "price_per_kg": _summary_to_float(item.get("price_per_kg")),
                "price_per_meter": _summary_to_float(item.get("price_per_meter")),
            }

    return results


def _infer_material_for_enquiry(enquiry: Enquiry) -> Optional[str]:
    candidate = getattr(enquiry, "material_customer", None)
    if isinstance(candidate, str) and candidate.strip():
        extracted = _extract_material_from_text(candidate, allow_extended_ag_code=True)
        return extracted or _material_display_name(candidate.strip()) or None

    # Prefer structured fields (less noisy than subject/description).
    value = getattr(enquiry, "hardness_or_material", None)
    extracted = _extract_material_from_text(str(value or ""), allow_extended_ag_code=True)
    if extracted:
        return extracted

    subdivisions = getattr(enquiry, "subdivisions", None) or []
    for subdivision in subdivisions:
        sub_candidate = getattr(subdivision, "material_customer", None)
        if isinstance(sub_candidate, str) and sub_candidate.strip():
            extracted = _extract_material_from_text(sub_candidate, allow_extended_ag_code=True)
            return extracted or _material_display_name(sub_candidate.strip()) or None
        extracted = _extract_material_from_text(
            str(getattr(subdivision, "hardness_or_material", "") or ""),
            allow_extended_ag_code=True,
        )
        if extracted:
            return extracted

    attachment_sources: List[Any] = []
    attachment_sources.extend(getattr(enquiry, "attachments", None) or [])
    for subdivision in subdivisions:
        attachment_sources.extend(getattr(subdivision, "attachments", None) or [])

    for attachment in attachment_sources:
        if not _attachment_looks_like_pdf(attachment):
            continue
        text = _read_pdf_text(getattr(attachment, "storage_path", None))
        extracted = _extract_material_from_text(text, allow_extended_ag_code=False)
        if extracted:
            return extracted

    # Last resort: infer from free-form text.
    for field in ("subject", "description"):
        value = getattr(enquiry, field, None)
        extracted = _extract_material_from_text(str(value or ""), allow_extended_ag_code=False)
        if extracted:
            return extracted

    return None


enquiries_table = Table(
    "enquiries",
    _metadata,
    Column("enquiry_id", String(32), primary_key=True),
    Column("customer_name", String(255), nullable=False),
    Column("subject", String(255), nullable=False),
    Column("channel", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("description", Text, nullable=False),
    Column("material_customer", Text),
    Column("feasibility", Text),
    Column("required_trials_meters", Float),
    Column("die_development_charge", Float),
    Column("die_tool_development_charge_usd", Float),
    Column("supplier_mold_cost", Float),
    Column("supplier_cost_per_piece", Float),
    Column("mold_development_cost", Float),
    Column("mold_development_charge_usd", Float),
    Column("manufacturing_process", Text),
    Column("manufacturing_process_internal", Text),
    Column("surface_treatment", Text),
    Column("area_or_volume", Text),
    Column("gasket_thickness_length_mm", Text),
    Column("die_number", Text),
    Column("hardness_or_material", Text),
    Column("weight_per_meter_g", Float),
    Column("weight_per_meter_pricing", Float),
    Column("weight_per_piece_g", Float),
    Column("price_per_kg", Float),
    Column("minimum_order_quantity", Integer),
    Column("price_per_piece", Float),
    Column("customer_reference", Text),
    Column("test_standard", Text),
    Column("specific_gravity", Float),
    Column("project_name", Text),
    Column("digital_cutting_price", Float),
    Column("width_of_tape_required", Float),
    Column("dsa_tape_application_charge", Float),
    Column("secondary_operation_charge", Float),
    Column("total_price_per_piece", Float),
    Column("x20_percent", Float),
    Column("scrap_percent", Float),
    Column("final_price_per_piece", Float),
    Column("remarks", Text),
    Column("extrusion_die_count", Integer),
    Column("mold_count", Integer),
    Column("extrusion_die_columns", Text),
    Column("mold_columns", Text),
    Column("received_at", DateTime, nullable=False),
    Column("assigned_to", String(255)),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now()),
)


attachments_table = Table(
    "enquiry_attachments",
    _metadata,
    Column("attachment_id", String(64), primary_key=True),
    Column("enquiry_id", String(32), ForeignKey("enquiries.enquiry_id", ondelete="CASCADE"), nullable=False),
    Column("file_name", String(255), nullable=False),
    Column("content_type", String(255)),
    Column("size_bytes", Integer),
    Column("uploaded_at", DateTime, nullable=False),
    Column("storage_path", Text, nullable=False),
    Column("category", String(32), nullable=False, server_default="general"),
)


subdivisions_table = Table(
    "enquiry_subdivisions",
    _metadata,
    Column("subdivision_id", String(48), primary_key=True),
    Column(
        "parent_enquiry_id",
        String(32),
        ForeignKey("enquiries.enquiry_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("code", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default="open"),
    Column("customer_name", String(255)),
    Column("subject", String(255)),
    Column("channel", String(64)),
    Column("priority", String(16)),
    Column("description", Text),
    Column("material_customer", Text),
    Column("feasibility", Text),
    Column("required_trials_meters", Float),
    Column("die_development_charge", Float),
    Column("die_tool_development_charge_usd", Float),
    Column("supplier_mold_cost", Float),
    Column("supplier_cost_per_piece", Float),
    Column("mold_development_cost", Float),
    Column("mold_development_charge_usd", Float),
    Column("manufacturing_process", Text),
    Column("manufacturing_process_internal", Text),
    Column("surface_treatment", Text),
    Column("area_or_volume", Text),
    Column("gasket_thickness_length_mm", Text),
    Column("die_number", Text),
    Column("hardness_or_material", Text),
    Column("weight_per_meter_g", Float),
    Column("weight_per_meter_pricing", Float),
    Column("weight_per_piece_g", Float),
    Column("price_per_kg", Float),
    Column("minimum_order_quantity", Integer),
    Column("price_per_piece", Float),
    Column("customer_reference", Text),
    Column("test_standard", Text),
    Column("specific_gravity", Float),
    Column("project_name", Text),
    Column("digital_cutting_price", Float),
    Column("width_of_tape_required", Float),
    Column("dsa_tape_application_charge", Float),
    Column("secondary_operation_charge", Float),
    Column("total_price_per_piece", Float),
    Column("x20_percent", Float),
    Column("scrap_percent", Float),
    Column("final_price_per_piece", Float),
    Column("remarks", Text),
    Column("received_at", DateTime),
    Column("assigned_to", String(255)),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now()),
)


subdivision_attachments_table = Table(
    "enquiry_subdivision_attachments",
    _metadata,
    Column("attachment_id", String(64), primary_key=True),
    Column(
        "subdivision_id",
        String(48),
        ForeignKey("enquiry_subdivisions.subdivision_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("file_name", String(255), nullable=False),
    Column("content_type", String(255)),
    Column("size_bytes", Integer),
    Column("uploaded_at", DateTime, nullable=False),
    Column("storage_path", Text, nullable=False),
    Column("category", String(32), nullable=False, server_default="general"),
)


email_events_table = Table(
    "email_ingestion_events",
    _metadata,
    Column("message_id", String(128), primary_key=True),
    Column("thread_id", String(128)),
    Column("mailbox", String(255), nullable=False),
    Column("sender", String(255)),
    Column("subject", Text),
    Column("snippet", Text),
    Column("body_preview", Text),
    Column("received_at", DateTime),
    Column("detection_score", Float),
    Column("matched_keywords", Text),
    Column("status", String(32), nullable=False, server_default="pending"),
    Column("reason", Text),
    Column("created_enquiry_id", String(32)),
    Column("has_attachments", Integer, server_default="0"),
    Column("auto_created", Integer, server_default="0"),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
)


email_watch_state_table = Table(
    "email_watch_state",
    _metadata,
    Column("mailbox", String(255), primary_key=True),
    Column("last_polled_at", DateTime),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("updated_at", DateTime, nullable=False, server_default=func.now(), onupdate=func.now()),
)


_engine: Optional[Engine] = None


def _get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    database_url = os.environ.get("ENQUIRY_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        _engine = create_engine(database_url, future=True)
        if database_url.startswith("sqlite"):
            logger.warning(
                "Enquiry service is running against SQLite (%s); do not use this configuration in production.",
                database_url,
            )
        return _engine

    sqlite_path = os.getenv("SQLITE_PATH")
    if sqlite_path:
        logger.warning(
            "SQLITE_PATH is configured for the enquiry service (%s); this is intended for local development only.",
            sqlite_path,
        )
        _engine = create_engine(
            f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
        )
        with _engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        return _engine

    db_user = os.environ.get("DB_USER", "")
    db_pass = os.environ.get("DB_PASSWORD", "")
    db_name = os.environ.get("DB_NAME", "")
    instance_conn = os.environ.get("INSTANCE_CONNECTION_NAME")

    if instance_conn:
        if Connector is None:
            raise RuntimeError(
                "google-cloud-sql-connector is required when INSTANCE_CONNECTION_NAME is provided"
            )
        if not db_user:
            raise RuntimeError("DB_USER must be set when using INSTANCE_CONNECTION_NAME")

        connector = Connector()

        def getconn():
            return connector.connect(
                instance_conn,
                "pg8000",
                user=db_user,
                password=db_pass,
                db=db_name,
            )

        _engine = create_engine("postgresql+pg8000://", future=True, creator=getconn)
        return _engine

    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")

    if not db_user or not db_name:
        raise RuntimeError(
            "Enquiry service database configuration missing. Set ENQUIRY_DATABASE_URL (recommended) or DB_USER/DB_NAME credentials."
        )

    _engine = create_engine(
        f"postgresql+pg8000://{db_user}:{db_pass}@{host}:{port}/{db_name}", future=True
    )
    return _engine


def _ensure_schema() -> None:
    engine = _get_engine()
    try:
        _metadata.create_all(engine)
        _ensure_optional_columns(engine)
    except (InterfaceError, OperationalError) as exc:
        if not _is_local_environment() or (engine.url and str(engine.url).startswith("sqlite")):
            raise
        logger.warning("Enquiry service database unavailable; switching to SQLite fallback.", exc_info=True)
        engine = _use_sqlite_fallback_engine()
        _metadata.create_all(engine)
        _ensure_optional_columns(engine)


def _ensure_optional_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    try:
        enquiry_columns = {column["name"] for column in inspector.get_columns("enquiries")}
    except Exception:
        enquiry_columns = set()

    statements: List[str] = []

    def _add(column_name: str, ddl: str) -> None:
        if column_name not in enquiry_columns:
            statements.append(ddl)

    _add("customer_reference", "ALTER TABLE enquiries ADD COLUMN customer_reference TEXT")
    _add("test_standard", "ALTER TABLE enquiries ADD COLUMN test_standard TEXT")
    _add("specific_gravity", "ALTER TABLE enquiries ADD COLUMN specific_gravity REAL")
    _add("project_name", "ALTER TABLE enquiries ADD COLUMN project_name TEXT")
    _add("manufacturing_process_internal", "ALTER TABLE enquiries ADD COLUMN manufacturing_process_internal TEXT")
    _add("weight_per_meter_pricing", "ALTER TABLE enquiries ADD COLUMN weight_per_meter_pricing REAL")
    _add("digital_cutting_price", "ALTER TABLE enquiries ADD COLUMN digital_cutting_price REAL")
    _add("width_of_tape_required", "ALTER TABLE enquiries ADD COLUMN width_of_tape_required REAL")
    _add("x20_percent", "ALTER TABLE enquiries ADD COLUMN x20_percent REAL")
    _add("extrusion_die_count", "ALTER TABLE enquiries ADD COLUMN extrusion_die_count INTEGER")
    _add("mold_count", "ALTER TABLE enquiries ADD COLUMN mold_count INTEGER")
    _add("extrusion_die_columns", "ALTER TABLE enquiries ADD COLUMN extrusion_die_columns TEXT")
    _add("mold_columns", "ALTER TABLE enquiries ADD COLUMN mold_columns TEXT")

    if statements:
        with engine.begin() as conn:
            for statement in statements:
                try:
                    conn.exec_driver_sql(statement)
                except Exception:
                    continue

    try:
        attachment_columns = {column["name"] for column in inspector.get_columns("enquiry_attachments")}
    except Exception:
        attachment_columns = set()

    if "category" not in attachment_columns:
        with engine.begin() as conn:
            try:
                conn.exec_driver_sql(
                    "ALTER TABLE enquiry_attachments ADD COLUMN category TEXT DEFAULT 'general'"
                )
            except Exception:
                pass


def _load_column_data(value) -> List[ColumnData]:
    if value in (None, ""):
        return []
    raw: List[dict]
    if isinstance(value, str):
        try:
            raw = json.loads(value)
        except (TypeError, ValueError):
            return []
    elif isinstance(value, list):
        raw = value
    else:
        return []
    columns: List[ColumnData] = []
    for item in raw:
        if isinstance(item, ColumnData):
            columns.append(item)
            continue
        if isinstance(item, dict):
            try:
                columns.append(ColumnData(**item))
            except Exception:
                continue
    return columns


def _dump_column_data(items) -> Optional[str]:
    if not items:
        return None
    serialisable: List[dict] = []
    for entry in items:
        if isinstance(entry, ColumnData):
            serialisable.append(entry.model_dump())
        elif isinstance(entry, dict):
            serialisable.append(entry)
        else:
            try:
                serialisable.append(ColumnData(**entry).model_dump())  # type: ignore[arg-type]
            except Exception:
                continue
    if not serialisable:
        return None
    return json.dumps(serialisable)


def _row_to_enquiry(
    row: Dict,
    attachments: List[Dict],
    *,
    subdivisions: Optional[List[Subdivision]] = None,
) -> Enquiry:
    attachment_models = [
        AttachmentMeta(
            attachment_id=attachment["attachment_id"],
            file_name=attachment["file_name"],
            content_type=attachment.get("content_type"),
            size_bytes=attachment.get("size_bytes") or 0,
            uploaded_at=attachment["uploaded_at"],
            download_url=f"/enquiries/{row['enquiry_id']}/attachments/{attachment['attachment_id']}",
            storage_path=attachment["storage_path"],
            category=attachment.get("category", "general"),
        )
        for attachment in attachments
    ]
    data = dict(row)
    data["extrusion_die_columns"] = _load_column_data(data.get("extrusion_die_columns"))
    data["mold_columns"] = _load_column_data(data.get("mold_columns"))
    data["attachments"] = attachment_models
    data["subdivisions"] = list(subdivisions or [])
    return Enquiry(**data)


def _fetch_attachments(conn, enquiry_ids: Iterable[str]) -> Dict[str, List[Dict]]:
    ids = list(dict.fromkeys(enquiry_ids))
    if not ids:
        return {}
    stmt = select(attachments_table).where(attachments_table.c.enquiry_id.in_(ids))
    rows = conn.execute(stmt).mappings().all()
    grouped: Dict[str, List[Dict]] = {key: [] for key in ids}
    for attachment in rows:
        grouped.setdefault(attachment["enquiry_id"], []).append(dict(attachment))
    return grouped


def _row_to_subdivision(row: Dict, attachments: List[Dict]) -> Subdivision:
    attachment_models = [
        AttachmentMeta(
            attachment_id=attachment["attachment_id"],
            file_name=attachment["file_name"],
            content_type=attachment.get("content_type"),
            size_bytes=attachment.get("size_bytes") or 0,
            uploaded_at=attachment["uploaded_at"],
            download_url=(
                f"/enquiries/{row['parent_enquiry_id']}/subdivisions/{row['subdivision_id']}/attachments/{attachment['attachment_id']}"
            ),
            storage_path=attachment["storage_path"],
            category=attachment.get("category", "general"),
        )
        for attachment in attachments
    ]
    data = dict(row)
    data["attachments"] = attachment_models
    return Subdivision(**data)


def _fetch_subdivision_attachments(conn, subdivision_ids: Iterable[str]) -> Dict[str, List[Dict]]:
    ids = list(dict.fromkeys(subdivision_ids))
    if not ids:
        return {}
    stmt = select(subdivision_attachments_table).where(
        subdivision_attachments_table.c.subdivision_id.in_(ids)
    )
    rows = conn.execute(stmt).mappings().all()
    grouped: Dict[str, List[Dict]] = {key: [] for key in ids}
    for attachment in rows:
        grouped.setdefault(attachment["subdivision_id"], []).append(dict(attachment))
    return grouped


def _fetch_subdivisions_map(conn, parent_enquiry_ids: Iterable[str]) -> Dict[str, List[Subdivision]]:
    ids = list(dict.fromkeys(parent_enquiry_ids))
    if not ids:
        return {}
    stmt = (
        select(subdivisions_table)
        .where(subdivisions_table.c.parent_enquiry_id.in_(ids))
        .order_by(subdivisions_table.c.created_at.asc())
    )
    rows = conn.execute(stmt).mappings().all()
    attachments = _fetch_subdivision_attachments(conn, (row["subdivision_id"] for row in rows))
    grouped: Dict[str, List[Subdivision]] = {key: [] for key in ids}
    for row in rows:
        subdivision = _row_to_subdivision(row, attachments.get(row["subdivision_id"], []))
        grouped.setdefault(row["parent_enquiry_id"], []).append(subdivision)
    return grouped


def _deserialize_keywords(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:  # pragma: no cover - defensive fallback
        if isinstance(raw, str):
            return [item.strip() for item in raw.split(",") if item.strip()]
        return []
    if isinstance(data, list):
        return [str(item) for item in data]
    return []


def _row_to_email_event(row: Dict) -> EmailIngestionEvent:
    matched = _deserialize_keywords(row.get("matched_keywords"))
    return EmailIngestionEvent(
        message_id=row.get("message_id"),
        thread_id=row.get("thread_id"),
        mailbox=row.get("mailbox"),
        sender=row.get("sender"),
        subject=row.get("subject"),
        snippet=row.get("snippet"),
        body_preview=row.get("body_preview"),
        received_at=row.get("received_at"),
        detection_score=row.get("detection_score"),
        matched_keywords=matched,
        status=row.get("status"),
        reason=row.get("reason"),
        created_enquiry_id=row.get("created_enquiry_id"),
        has_attachments=bool(row.get("has_attachments")),
        auto_created=bool(row.get("auto_created")),
        created_at=row.get("created_at"),
    )


def has_any_enquiries() -> bool:
    """Check whether the enquiries table already contains records."""
    _ensure_schema()
    with _get_engine().connect() as conn:
        row = conn.execute(select(enquiries_table.c.enquiry_id).limit(1)).first()
    return row is not None


def list_enquiries(status: Optional[str] = None, priority: Optional[str] = None) -> List[Enquiry]:
    _ensure_schema()
    stmt = select(enquiries_table).order_by(enquiries_table.c.received_at.desc(), enquiries_table.c.enquiry_id.desc())
    if status:
        stmt = stmt.where(enquiries_table.c.status == status)
    if priority:
        stmt = stmt.where(enquiries_table.c.priority == priority)

    with _get_engine().begin() as conn:
        rows = conn.execute(stmt).mappings().all()
        attachments = _fetch_attachments(conn, (row["enquiry_id"] for row in rows))
        subdivisions = _fetch_subdivisions_map(conn, (row["enquiry_id"] for row in rows))

    return [
        _row_to_enquiry(
            row,
            attachments.get(row["enquiry_id"], []),
            subdivisions=subdivisions.get(row["enquiry_id"], []),
        )
        for row in rows
    ]


def list_subdivisions(parent_enquiry_id: str) -> List[Subdivision]:
    _ensure_schema()
    stmt = (
        select(subdivisions_table)
        .where(subdivisions_table.c.parent_enquiry_id == parent_enquiry_id)
        .order_by(subdivisions_table.c.created_at.asc())
    )
    with _get_engine().begin() as conn:
        rows = conn.execute(stmt).mappings().all()
        attachments = _fetch_subdivision_attachments(conn, (row["subdivision_id"] for row in rows))
    return [_row_to_subdivision(row, attachments.get(row["subdivision_id"], [])) for row in rows]


def get_enquiry(enquiry_id: str) -> Optional[Enquiry]:
    _ensure_schema()
    stmt = select(enquiries_table).where(enquiries_table.c.enquiry_id == enquiry_id)
    with _get_engine().begin() as conn:
        row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        attachments = _fetch_attachments(conn, [enquiry_id])
        subdivisions = _fetch_subdivisions_map(conn, [enquiry_id])
    return _row_to_enquiry(
        row,
        attachments.get(enquiry_id, []),
        subdivisions=subdivisions.get(enquiry_id, []),
    )


def get_board() -> BoardResponse:
    items = list_enquiries()
    lanes = {"open": [], "closed": []}
    for item in items:
        lane_key = "open" if item.status == "open" else "closed"
        lanes[lane_key].append(item)
    return BoardResponse(open=lanes["open"], closed=lanes["closed"])


def generate_enquiry_id() -> str:
    _ensure_schema()
    with _get_engine().begin() as conn:
        row = conn.execute(
            select(enquiries_table.c.enquiry_id)
            .order_by(enquiries_table.c.enquiry_id.desc())
            .limit(1)
        ).scalar_one_or_none()

    if not row:
        return "ENQ-1001"

    try:
        numeric = int(str(row).split("-")[-1])
    except ValueError:
        numeric = 1000
    return f"ENQ-{numeric + 1:04d}"


def _generate_subdivision_identifier(conn, parent_enquiry_id: str) -> tuple[str, str]:
    existing_codes = {
        (row[0] or "").lower()
        for row in conn.execute(
            select(subdivisions_table.c.code).where(subdivisions_table.c.parent_enquiry_id == parent_enquiry_id)
        )
    }
    for letter in string.ascii_lowercase:
        if letter not in existing_codes:
            code = letter
            subdivision_id = f"{parent_enquiry_id}-{letter}"
            return subdivision_id, letter
    raise ValueError("No available subdivision codes")


def get_subdivision(parent_enquiry_id: str, subdivision_id: str) -> Optional[Subdivision]:
    _ensure_schema()
    stmt = select(subdivisions_table).where(
        subdivisions_table.c.parent_enquiry_id == parent_enquiry_id,
        subdivisions_table.c.subdivision_id == subdivision_id,
    )
    with _get_engine().begin() as conn:
        row = conn.execute(stmt).mappings().first()
        if not row:
            return None
        attachments = _fetch_subdivision_attachments(conn, [subdivision_id])
    return _row_to_subdivision(row, attachments.get(subdivision_id, []))


def create_enquiry(enquiry_id: str, payload: Dict, attachments: List[Dict]) -> Enquiry:
    _ensure_schema()
    base_data = dict(payload)
    base_data["extrusion_die_columns"] = _dump_column_data(base_data.get("extrusion_die_columns"))
    base_data["mold_columns"] = _dump_column_data(base_data.get("mold_columns"))
    base_data.setdefault("received_at", datetime.utcnow())
    base_data.setdefault("status", "open")
    base_data.update({"enquiry_id": enquiry_id, "updated_at": datetime.utcnow()})

    with _get_engine().begin() as conn:
        try:
            conn.execute(insert(enquiries_table).values(base_data))
        except IntegrityError as exc:
            raise ValueError("Enquiry ID already exists") from exc

        if attachments:
            conn.execute(insert(attachments_table), attachments)

        row = conn.execute(
            select(enquiries_table).where(enquiries_table.c.enquiry_id == enquiry_id)
        ).mappings().first()
        attachment_rows = _fetch_attachments(conn, [enquiry_id])

    return _row_to_enquiry(row, attachment_rows.get(enquiry_id, []), subdivisions=[])


def next_subdivision_identifier(parent_enquiry_id: str) -> tuple[str, str]:
    _ensure_schema()
    with _get_engine().begin() as conn:
        return _generate_subdivision_identifier(conn, parent_enquiry_id)


def create_subdivision(
    parent_enquiry_id: str,
    payload: Dict,
    attachments: List[Dict],
    *,
    subdivision_id: Optional[str] = None,
    code: Optional[str] = None,
) -> Subdivision:
    _ensure_schema()
    with _get_engine().begin() as conn:
        if subdivision_id is None or code is None:
            subdivision_id, code = _generate_subdivision_identifier(conn, parent_enquiry_id)
        base_data = dict(payload)
        base_data.setdefault("status", "open")
        base_data.setdefault("received_at", datetime.utcnow())
        base_data.update(
            {
                "subdivision_id": subdivision_id,
                "parent_enquiry_id": parent_enquiry_id,
                "code": code,
                "updated_at": datetime.utcnow(),
            }
        )

        conn.execute(insert(subdivisions_table).values(base_data))

        if attachments:
            for item in attachments:
                item.setdefault("subdivision_id", subdivision_id)
            conn.execute(insert(subdivision_attachments_table), attachments)

        row = conn.execute(
            select(subdivisions_table).where(subdivisions_table.c.subdivision_id == subdivision_id)
        ).mappings().first()
        attachment_rows = _fetch_subdivision_attachments(conn, [subdivision_id])

    return _row_to_subdivision(row, attachment_rows.get(subdivision_id, []))


def update_enquiry(enquiry_id: str, updates: Dict, new_attachments: List[Dict]) -> Optional[Enquiry]:
    if not updates and not new_attachments:
        return get_enquiry(enquiry_id)

    _ensure_schema()
    updates = dict(updates)
    if updates:
        if "extrusion_die_columns" in updates:
            updates["extrusion_die_columns"] = _dump_column_data(updates.get("extrusion_die_columns"))
        if "mold_columns" in updates:
            updates["mold_columns"] = _dump_column_data(updates.get("mold_columns"))
        updates["updated_at"] = datetime.utcnow()

    with _get_engine().begin() as conn:
        if updates:
            result = conn.execute(
                update(enquiries_table)
                .where(enquiries_table.c.enquiry_id == enquiry_id)
                .values(**updates)
            )
            if result.rowcount == 0:
                return None

        if new_attachments:
            conn.execute(insert(attachments_table), new_attachments)

        row = conn.execute(
            select(enquiries_table).where(enquiries_table.c.enquiry_id == enquiry_id)
        ).mappings().first()
        if not row:
            return None
        attachment_rows = _fetch_attachments(conn, [enquiry_id])
        subdivision_rows = _fetch_subdivisions_map(conn, [enquiry_id])

    return _row_to_enquiry(
        row,
        attachment_rows.get(enquiry_id, []),
        subdivisions=subdivision_rows.get(enquiry_id, []),
    )


def delete_enquiry(enquiry_id: str) -> bool:
    _ensure_schema()
    with _get_engine().begin() as conn:
        conn.execute(delete(attachments_table).where(attachments_table.c.enquiry_id == enquiry_id))
        result = conn.execute(delete(enquiries_table).where(enquiries_table.c.enquiry_id == enquiry_id))
    return result.rowcount > 0


def update_subdivision(
    parent_enquiry_id: str,
    subdivision_id: str,
    updates: Dict,
    new_attachments: List[Dict],
) -> Optional[Subdivision]:
    if not updates and not new_attachments:
        return get_subdivision(parent_enquiry_id, subdivision_id)

    _ensure_schema()
    updates = dict(updates)
    if updates:
        updates["updated_at"] = datetime.utcnow()

    with _get_engine().begin() as conn:
        if updates:
            result = conn.execute(
                update(subdivisions_table)
                .where(
                    subdivisions_table.c.subdivision_id == subdivision_id,
                    subdivisions_table.c.parent_enquiry_id == parent_enquiry_id,
                )
                .values(**updates)
            )
            if result.rowcount == 0:
                return None

        if new_attachments:
            for item in new_attachments:
                item.setdefault("subdivision_id", subdivision_id)
            conn.execute(insert(subdivision_attachments_table), new_attachments)

        row = conn.execute(
            select(subdivisions_table).where(
                subdivisions_table.c.subdivision_id == subdivision_id,
                subdivisions_table.c.parent_enquiry_id == parent_enquiry_id,
            )
        ).mappings().first()
        if not row:
            return None
        attachment_rows = _fetch_subdivision_attachments(conn, [subdivision_id])

    return _row_to_subdivision(row, attachment_rows.get(subdivision_id, []))


def delete_subdivision(parent_enquiry_id: str, subdivision_id: str) -> bool:
    _ensure_schema()
    with _get_engine().begin() as conn:
        conn.execute(
            delete(subdivision_attachments_table).where(
                subdivision_attachments_table.c.subdivision_id == subdivision_id
            )
        )
        result = conn.execute(
            delete(subdivisions_table).where(
                subdivisions_table.c.subdivision_id == subdivision_id,
                subdivisions_table.c.parent_enquiry_id == parent_enquiry_id,
            )
        )
    return result.rowcount > 0


def get_attachment(attachment_id: str) -> Optional[AttachmentMeta]:
    _ensure_schema()
    stmt = select(attachments_table).where(attachments_table.c.attachment_id == attachment_id)
    with _get_engine().begin() as conn:
        attachment = conn.execute(stmt).mappings().first()
        if not attachment:
            return None
        enquiry = conn.execute(
            select(enquiries_table.c.enquiry_id).where(
                enquiries_table.c.enquiry_id == attachment["enquiry_id"]
            )
        ).scalar_one_or_none()

    if not enquiry:
        return None

    return AttachmentMeta(
        attachment_id=attachment["attachment_id"],
        file_name=attachment["file_name"],
        content_type=attachment.get("content_type"),
        size_bytes=attachment.get("size_bytes") or 0,
        uploaded_at=attachment["uploaded_at"],
        download_url=f"/enquiries/{attachment['enquiry_id']}/attachments/{attachment['attachment_id']}",
        storage_path=attachment["storage_path"],
        category=attachment.get("category", "general"),
    )


def get_subdivision_attachment(attachment_id: str) -> Optional[AttachmentMeta]:
    _ensure_schema()
    stmt = select(subdivision_attachments_table).where(
        subdivision_attachments_table.c.attachment_id == attachment_id
    )
    with _get_engine().begin() as conn:
        attachment = conn.execute(stmt).mappings().first()
        if not attachment:
            return None
        subdivision = conn.execute(
            select(
                subdivisions_table.c.subdivision_id,
                subdivisions_table.c.parent_enquiry_id,
            ).where(subdivisions_table.c.subdivision_id == attachment["subdivision_id"])
        ).mappings().first()

    if not subdivision:
        return None

    return AttachmentMeta(
        attachment_id=attachment["attachment_id"],
        file_name=attachment["file_name"],
        content_type=attachment.get("content_type"),
        size_bytes=attachment.get("size_bytes") or 0,
        uploaded_at=attachment["uploaded_at"],
        download_url=(
            f"/enquiries/{subdivision['parent_enquiry_id']}/subdivisions/{subdivision['subdivision_id']}/attachments/{attachment['attachment_id']}"
        ),
        storage_path=attachment["storage_path"],
        category=attachment.get("category", "general"),
    )


def summarize_enquiries(
    *,
    period: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> SummaryResponse:
    global _SUMMARY_CACHE

    cache_seconds = 0
    try:
        cache_seconds = int(os.getenv("ENQUIRY_SUMMARY_CACHE_SECONDS", "120"))
    except (TypeError, ValueError):
        cache_seconds = 120

    selected_period, filter_start_dt, filter_end_dt = _resolve_summary_date_range(
        period=period,
        start_date=start_date,
        end_date=end_date,
    )
    cache_key = _summary_filter_key(selected_period, start_date, end_date)
    now_ts = time.time()
    if cache_seconds > 0:
        cached = _SUMMARY_CACHE.get(cache_key)
        if cached is not None:
            cached_at, cached_response = cached
            if (now_ts - cached_at) < cache_seconds:
                return cached_response

    enquiries = list_enquiries()
    if filter_start_dt is not None or filter_end_dt is not None:
        filtered_enquiries: List[Enquiry] = []
        for enquiry in enquiries:
            received_at = _normalize_summary_datetime(getattr(enquiry, "received_at", None))
            if received_at is None:
                continue
            if filter_start_dt is not None and received_at < filter_start_dt:
                continue
            if filter_end_dt is not None and received_at > filter_end_dt:
                continue
            filtered_enquiries.append(enquiry)
        enquiries = filtered_enquiries
    total = len(enquiries)

    status_counts: Dict[str, int] = {}
    for enquiry in enquiries:
        status = (getattr(enquiry, "status", "") or "").strip().lower() or "open"
        status_counts[status] = status_counts.get(status, 0) + 1

    open_count = status_counts.get("open", 0)
    accepted_count = status_counts.get("accepted", 0)
    rejected_count = status_counts.get("rejected", 0)
    converted_count = status_counts.get("converted", 0)
    closed_count = total - open_count

    priority_counts: Dict[str, int] = {}
    channel_counts: Dict[str, int] = {}
    material_counts_by_key: Dict[str, int] = {}
    material_display_by_key: Dict[str, str] = {}
    material_by_enquiry_id: Dict[str, str] = {}
    price_piece_by_enquiry_id: Dict[str, float] = {}
    price_kg_by_enquiry_id: Dict[str, float] = {}
    price_meter_by_enquiry_id: Dict[str, float] = {}
    price_per_piece_values: List[float] = []
    price_per_kg_values: List[float] = []
    price_per_meter_values: List[float] = []
    pdf_insights = _extract_pdf_insights_with_gemini(enquiries)

    for enquiry in enquiries:
        priority_counts[enquiry.priority] = priority_counts.get(enquiry.priority, 0) + 1
        channel_counts[enquiry.channel] = channel_counts.get(enquiry.channel, 0) + 1
        insight = pdf_insights.get(enquiry.enquiry_id, {})
        material_raw = _infer_material_for_enquiry(enquiry)
        if not material_raw:
            material_raw = insight.get("material") if isinstance(insight, dict) else None
        if material_raw:
            key = _normalize_material_key(material_raw)
            if key:
                display = _material_display_name(material_raw) or material_raw.strip()
                material_by_enquiry_id[enquiry.enquiry_id] = display
                material_display_by_key.setdefault(key, display)
                material_counts_by_key[key] = material_counts_by_key.get(key, 0) + 1

        piece_value = _summary_to_float(enquiry.price_per_piece)
        if piece_value is None:
            piece_value = _summary_to_float(getattr(enquiry, "final_price_per_piece", None))
        if piece_value is None and isinstance(insight, dict):
            piece_value = _summary_to_float(insight.get("price_per_piece"))
        if piece_value is not None:
            price_per_piece_values.append(piece_value)
            price_piece_by_enquiry_id[enquiry.enquiry_id] = piece_value

        kg_value = _summary_to_float(enquiry.price_per_kg)
        if kg_value is None and isinstance(insight, dict):
            kg_value = _summary_to_float(insight.get("price_per_kg"))
        if kg_value is not None:
            price_per_kg_values.append(kg_value)
            price_kg_by_enquiry_id[enquiry.enquiry_id] = kg_value

        meter_value = _summary_to_float(getattr(enquiry, "weight_per_meter_pricing", None))
        if meter_value is None and isinstance(insight, dict):
            meter_value = _summary_to_float(insight.get("price_per_meter"))
        if meter_value is not None:
            price_per_meter_values.append(meter_value)
            price_meter_by_enquiry_id[enquiry.enquiry_id] = meter_value

    avg_piece = (
        round(sum(price_per_piece_values) / len(price_per_piece_values), 2)
        if price_per_piece_values
        else None
    )
    avg_kg = (
        round(sum(price_per_kg_values) / len(price_per_kg_values), 2)
        if price_per_kg_values
        else None
    )
    avg_meter = (
        round(sum(price_per_meter_values) / len(price_per_meter_values), 2)
        if price_per_meter_values
        else None
    )

    sorted_material_items = sorted(material_counts_by_key.items(), key=lambda item: item[1], reverse=True)
    material_counts: Dict[str, int] = {}
    for key, count in sorted_material_items:
        label = material_display_by_key.get(key) or key
        material_counts[label] = count

    metrics = SummaryMetrics(
        total_enquiries=total,
        open_enquiries=open_count,
        closed_enquiries=closed_count,
        accepted_enquiries=accepted_count,
        rejected_enquiries=rejected_count,
        converted_enquiries=converted_count,
        by_status={
            key: status_counts.get(key, 0)
            for key in ("open", "accepted", "converted", "rejected", "closed")
            if status_counts.get(key, 0)
        },
        by_priority=dict(sorted(priority_counts.items(), key=lambda item: item[0])),
        by_channel=dict(sorted(channel_counts.items(), key=lambda item: item[0])),
        by_material=dict(list(material_counts.items())[:5]),
        average_price_per_piece=avg_piece,
        average_price_per_kg=avg_kg,
        average_price_per_meter=avg_meter,
    )

    highlights: List[SummaryHighlight] = []
    if total:
        backlog_ratio = (open_count / total) * 100
        highlights.append(
            SummaryHighlight(
                title="Overall Volume",
                detail=f"{_enquiry_count(total)} tracked with {open_count} open ({backlog_ratio:.1f}% backlog).",
            )
        )

    if priority_counts:
        # On a tie the more urgent priority wins (high before medium before low).
        top_priority = max(
            priority_counts.items(),
            key=lambda item: (item[1], -_SUMMARY_PRIORITY_RANK.get(str(item[0]).strip().lower(), 9)),
        )
        highlights.append(
            SummaryHighlight(
                title="Focus Priority",
                detail=f"'{top_priority[0]}' has the heaviest load with {_enquiry_count(top_priority[1])}.",
            )
        )

    if material_counts:
        top_material = max(material_counts.items(), key=lambda item: item[1])
        highlights.append(
            SummaryHighlight(
                title="Material Hotspot",
                detail=f"'{top_material[0]}' features in {_enquiry_count(top_material[1])} this cycle.",
            )
        )

    def _snip(text: Optional[str], limit: int = 140) -> str:
        if not isinstance(text, str):
            return ""
        cleaned = " ".join(text.strip().split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rstrip() + "…"

    open_enquiries = [enquiry for enquiry in enquiries if enquiry.status == "open"]
    open_sorted = sorted(
        open_enquiries,
        key=lambda e: (
            {"high": 3, "medium": 2, "low": 1}.get((e.priority or "").strip().lower(), 0),
            e.received_at,
        ),
        reverse=True,
    )

    generated_at = datetime.utcnow()
    if any(enquiry.received_at.tzinfo for enquiry in enquiries):
        # Keep response consistent with existing naive datetime expectations.
        generated_at = generated_at.replace(tzinfo=None)

    baseline_examples = open_sorted[:3]
    narrative_lines = [
        "Enquiry desk summary:",
        f"• Total: {_enquiry_count(total)} (open: {open_count}).",
    ]

    outcome_parts: List[str] = []
    for key, label in (
        ("accepted", "accepted"),
        ("rejected", "rejected"),
        ("converted", "converted"),
        ("closed", "closed"),
    ):
        count = status_counts.get(key, 0)
        if count:
            outcome_parts.append(f"{label}: {count}")
    if outcome_parts:
        narrative_lines.append(f"• Outcomes – {', '.join(outcome_parts)}.")

    if priority_counts:
        prio_line = ", ".join(f"{name}: {count}" for name, count in sorted(
            priority_counts.items(),
            key=lambda item: (_SUMMARY_PRIORITY_RANK.get(str(item[0]).strip().lower(), 9), str(item[0]))))
        narrative_lines.append(f"• Priority mix – {prio_line}.")
    if material_counts:
        top_materials = list(material_counts.items())[:3]
        material_line = ", ".join(f"{name}: {count}" for name, count in top_materials)
        narrative_lines.append(f"• Material spotlight – {material_line}.")
    if baseline_examples:
        sample = "; ".join(f"#{item.enquiry_id} {_snip(item.subject, 60)}" for item in baseline_examples)
        narrative_lines.append(f"• Next actions – {sample}.")
    if avg_piece is not None:
        narrative_lines.append(f"• Average price per piece: {avg_piece:.2f}.")
    if avg_kg is not None:
        narrative_lines.append(f"• Average price per kg: {avg_kg:.2f}.")
    if avg_meter is not None:
        narrative_lines.append(f"• Average price per meter: {avg_meter:.2f}.")

    narrative = "\n".join(narrative_lines)
    baseline_narrative = narrative

    def _is_rich_narrative(text: str) -> bool:
        cleaned = (text or "").strip()
        if len(cleaned) < 180:
            return False
        bullet_lines = [
            line for line in cleaned.splitlines()
            if line.strip().startswith(("•", "-", "*"))
        ]
        return len(bullet_lines) >= 5

    if _summary_use_gemini():
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if api_key:
            try:
                import google.generativeai as genai  # type: ignore

                model_candidates = _summary_model_candidates()

                genai.configure(api_key=api_key)

                max_items = 0
                try:
                    max_items = int(os.getenv("ENQUIRY_SUMMARY_MAX_ITEMS", "25"))
                except (TypeError, ValueError):
                    max_items = 25
                max_items = max(5, min(max_items, 60))

                now = generated_at
                rows: List[Dict[str, Any]] = []
                for enquiry in open_sorted[:max_items]:
                    age_days = None
                    try:
                        age_days = max(0, (now - enquiry.received_at).days)
                    except Exception:
                        age_days = None
                    rows.append(
                        {
                            "enquiry_id": enquiry.enquiry_id,
                            "customer": enquiry.customer_name,
                            "subject": _snip(enquiry.subject, 100),
                            "priority": enquiry.priority,
                            "channel": enquiry.channel,
                            "material": material_by_enquiry_id.get(enquiry.enquiry_id),
                            "assigned_to": enquiry.assigned_to,
                            "age_days": age_days,
                            "price_per_piece": price_piece_by_enquiry_id.get(enquiry.enquiry_id),
                            "price_per_kg": price_kg_by_enquiry_id.get(enquiry.enquiry_id),
                            "price_per_meter": price_meter_by_enquiry_id.get(enquiry.enquiry_id),
                            "status": enquiry.status,
                        }
                    )

                summary_input: Dict[str, Any] = {
                    "generated_at": generated_at.isoformat(timespec="seconds"),
                    "metrics": metrics.model_dump(),
                    "open_enquiries_sample": rows,
                }

                response_schema = {
                    "type": "object",
                    "properties": {
                        "highlights": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "detail": {"type": "string"},
                                },
                                "required": ["title", "detail"],
                            },
                        },
                        "narrative": {"type": "string"},
                    },
                    "required": ["highlights", "narrative"],
                }

                prompt_json = (
                    "You are an operations analyst for Avigiri.\n"
                    "Using ONLY the JSON data below, write:\n"
                    "1) narrative: plain text with bullet points using the '•' character.\n"
                    "   Include sections for: Key numbers, Signals, Risks, Next actions.\n"
                    "2) highlights: 3–6 concise items (title <= 6 words, detail <= 140 chars)\n"
                    "Rules: Do not invent data. If something is unknown, say 'unknown'. "
                    "Reference enquiry IDs exactly when suggesting actions. Keep narrative under ~1800 chars.\n\n"
                    f"DATA:\n{json.dumps(summary_input, ensure_ascii=False)}"
                )

                prompt_text = (
                    "You are an operations analyst for Avigiri.\n"
                    "Using ONLY the JSON data below, return plain text in exactly this format:\n"
                    "NARRATIVE:\n"
                    "Use bullet points with the '•' character and include sections for: "
                    "Key numbers, Signals, Risks, Next actions.\n"
                    "HIGHLIGHTS:\n"
                    "- <title> | <detail>\n"
                    "(3–6 items; title <= 6 words; detail <= 140 chars)\n"
                    "Rules: Do not invent data. If something is unknown, say 'unknown'. "
                    "Reference enquiry IDs exactly when suggesting actions. Keep narrative under ~1800 chars.\n\n"
                    f"DATA:\n{json.dumps(summary_input, ensure_ascii=False)}"
                )

                prompt_narrative_only = (
                    "You are an operations analyst for Avigiri.\n"
                    "Using ONLY the JSON data below, write a plain text narrative with bullet points using the '•' character.\n"
                    "Include sections for: Key numbers, Signals, Risks, Next actions.\n"
                    "Rules: Do not invent data. If something is unknown, say 'unknown'. "
                    "Reference enquiry IDs exactly when suggesting actions. Keep narrative under ~1800 chars.\n\n"
                    f"DATA:\n{json.dumps(summary_input, ensure_ascii=False)}"
                )

                generation_config_json = genai.types.GenerationConfig(
                    temperature=0.2,
                    max_output_tokens=1000,
                    response_mime_type="application/json",
                    response_schema=response_schema,
                )

                generation_config_text = genai.types.GenerationConfig(
                    temperature=0.2,
                    max_output_tokens=900,
                )

                def _extract_text(resp: Any) -> str:
                    try:
                        return (resp.text or "").strip()
                    except Exception:
                        pass
                    try:
                        candidates = getattr(resp, "candidates", None) or []
                        if not candidates:
                            return ""
                        content = getattr(candidates[0], "content", None)
                        parts = getattr(content, "parts", None) or []
                        if not parts:
                            return ""
                        return str(getattr(parts[0], "text", "") or "").strip()
                    except Exception:
                        return ""

                def _parse_payload(text: str) -> Dict[str, Any]:
                    cleaned = (text or "").strip()
                    if cleaned.startswith("```"):
                        first_newline = cleaned.find("\n")
                        if first_newline != -1:
                            cleaned = cleaned[first_newline + 1 :].strip()
                        if cleaned.endswith("```"):
                            cleaned = cleaned[:-3].strip()

                    try:
                        payload_val = json.loads(cleaned, strict=False)
                    except json.JSONDecodeError:
                        start = cleaned.find("{")
                        end = cleaned.rfind("}")
                        if start != -1 and end != -1 and end > start:
                            payload_val = json.loads(cleaned[start : end + 1], strict=False)
                        else:
                            raise

                    if not isinstance(payload_val, dict):
                        raise RuntimeError("Gemini response was not a JSON object")
                    return payload_val

                def _parse_text_payload(text: str) -> Dict[str, Any]:
                    cleaned = (text or "").strip()
                    if cleaned.startswith("```"):
                        first_newline = cleaned.find("\n")
                        if first_newline != -1:
                            cleaned = cleaned[first_newline + 1 :].strip()
                        if cleaned.endswith("```"):
                            cleaned = cleaned[:-3].strip()

                    mode = ""
                    highlights_out: List[Dict[str, str]] = []
                    narrative_lines: List[str] = []

                    for line in cleaned.splitlines():
                        stripped = line.strip()
                        if not stripped:
                            continue
                        header = stripped.lower().rstrip(":")
                        if header in {"highlights", "highlight"}:
                            mode = "highlights"
                            continue
                        if header in {"narrative", "summary", "report"}:
                            mode = "narrative"
                            continue

                        if mode == "highlights":
                            item = stripped.lstrip("-•* ").strip()
                            title = ""
                            detail = ""
                            if "|" in item:
                                title, detail = (part.strip() for part in item.split("|", 1))
                            elif ":" in item:
                                title, detail = (part.strip() for part in item.split(":", 1))
                            else:
                                for sep in (" – ", " — ", " - "):
                                    if sep in item:
                                        title, detail = (part.strip() for part in item.split(sep, 1))
                                        break
                            if title and detail:
                                highlights_out.append({"title": title, "detail": detail})
                            continue

                        narrative_lines.append(stripped)

                    narrative_val = "\n".join(narrative_lines).strip()
                    if not narrative_val and not highlights_out:
                        raise RuntimeError("Gemini text response was empty")

                    return {"highlights": highlights_out, "narrative": narrative_val}

                payload: Optional[Dict[str, Any]] = None
                last_model: Optional[str] = None
                last_error: Optional[Exception] = None
                for model_name in model_candidates:
                    model: Any = None
                    try:
                        model = genai.GenerativeModel(model_name)
                    except Exception as exc:
                        last_error = exc
                        last_model = model_name
                        continue

                    try:
                        response = model.generate_content(prompt_json, generation_config=generation_config_json)
                        raw_text = _extract_text(response)
                        if not raw_text:
                            raise RuntimeError("Empty response from Gemini")
                        candidate_payload = _parse_payload(raw_text)
                        candidate_narrative = candidate_payload.get("narrative")
                        if not (isinstance(candidate_narrative, str) and candidate_narrative.strip()):
                            raise RuntimeError("Gemini response missing narrative")
                        payload = candidate_payload
                        break
                    except Exception as exc:
                        last_error = exc
                        last_model = model_name
                        try:
                            response = model.generate_content(prompt_text, generation_config=generation_config_text)
                            raw_text = _extract_text(response)
                            if not raw_text:
                                raise RuntimeError("Empty response from Gemini")
                            candidate_payload = _parse_text_payload(raw_text)
                            candidate_narrative = candidate_payload.get("narrative")
                            if not (isinstance(candidate_narrative, str) and candidate_narrative.strip()):
                                raise RuntimeError("Gemini response missing narrative")
                            payload = candidate_payload
                            break
                        except Exception as exc2:
                            last_error = exc2
                            last_model = model_name
                            try:
                                response = model.generate_content(
                                    prompt_narrative_only,
                                    generation_config=generation_config_text,
                                )
                                raw_text = _extract_text(response)
                                if not raw_text:
                                    raise RuntimeError("Empty response from Gemini")
                                candidate_payload = {"highlights": [], "narrative": raw_text.strip()}
                                candidate_narrative = candidate_payload.get("narrative")
                                if not (isinstance(candidate_narrative, str) and candidate_narrative.strip()):
                                    raise RuntimeError("Gemini response missing narrative")
                                payload = candidate_payload
                                break
                            except Exception as exc3:
                                last_error = exc3
                                last_model = model_name
                                continue

                if payload is None and last_error is not None:
                    raise RuntimeError(
                        f"Gemini summary failed (model={last_model}): {last_error}"
                    ) from last_error

                if isinstance(payload, dict):
                    narrative_val = payload.get("narrative")
                    highlights_val = payload.get("highlights")
                    if isinstance(narrative_val, str) and narrative_val.strip():
                        candidate_narrative = narrative_val.strip()
                        if _is_rich_narrative(candidate_narrative):
                            narrative = candidate_narrative
                        else:
                            logger.warning(
                                "enquiry_summary_gemini_short_narrative len=%s; using baseline narrative",
                                len(candidate_narrative),
                            )
                            narrative = baseline_narrative
                    if isinstance(highlights_val, list):
                        parsed: List[SummaryHighlight] = []
                        for item in highlights_val:
                            if not isinstance(item, dict):
                                continue
                            title = _snip(item.get("title"), 60)
                            detail = _snip(item.get("detail"), 180)
                            if not title or not detail:
                                continue
                            parsed.append(SummaryHighlight(title=title, detail=detail))
                        if parsed:
                            highlights = parsed[:6]
            except Exception as exc:
                logger.warning("enquiry_summary_gemini_failed err=%s", exc)

    response = SummaryResponse(
        generated_at=generated_at,
        metrics=metrics,
        highlights=highlights,
        narrative=narrative,
    )
    if cache_seconds > 0:
        _SUMMARY_CACHE[cache_key] = (now_ts, response)
    return response


def record_email_event(event: Dict[str, Any]) -> bool:
    _ensure_schema()
    values = dict(event)
    if "matched_keywords" in values and isinstance(values["matched_keywords"], list):
        values["matched_keywords"] = json.dumps(values["matched_keywords"])
    with _get_engine().begin() as conn:
        try:
            conn.execute(insert(email_events_table).values(values))
            return True
        except IntegrityError:
            return False


def email_event_exists(message_id: str) -> bool:
    _ensure_schema()
    with _get_engine().begin() as conn:
        row = (
            conn.execute(
                select(email_events_table.c.message_id).where(email_events_table.c.message_id == message_id)
            )
            .mappings()
            .first()
        )
    return row is not None


def list_email_events(status: Optional[str] = None, limit: int = 50) -> List[EmailIngestionEvent]:
    _ensure_schema()
    stmt = select(email_events_table).order_by(email_events_table.c.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(email_events_table.c.status == status)
    with _get_engine().begin() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_row_to_email_event(row) for row in rows]


def get_email_event(message_id: str) -> Optional[EmailIngestionEvent]:
    _ensure_schema()
    with _get_engine().begin() as conn:
        row = (
            conn.execute(
                select(email_events_table).where(email_events_table.c.message_id == message_id)
            )
            .mappings()
            .first()
        )
    return _row_to_email_event(row) if row else None


def update_email_event_status(
    message_id: str,
    status: str,
    *,
    enquiry_id: Optional[str] = None,
    reason: Optional[str] = None,
    auto_created: Optional[bool] = None,
    detection_score: Optional[float] = None,
    matched_keywords: Optional[List[str]] = None,
) -> None:
    _ensure_schema()
    values: Dict[str, Any] = {"status": status}
    if enquiry_id is not None:
        values["created_enquiry_id"] = enquiry_id
    if reason is not None:
        values["reason"] = reason
    if auto_created is not None:
        values["auto_created"] = int(bool(auto_created))
    if detection_score is not None:
        values["detection_score"] = detection_score
    if matched_keywords is not None:
        values["matched_keywords"] = json.dumps(matched_keywords)
    with _get_engine().begin() as conn:
        conn.execute(
            update(email_events_table)
            .where(email_events_table.c.message_id == message_id)
            .values(values)
        )


def get_mailbox_last_polled(mailbox: str) -> Optional[datetime]:
    _ensure_schema()
    with _get_engine().begin() as conn:
        row = (
            conn.execute(
                select(email_watch_state_table.c.last_polled_at).where(
                    email_watch_state_table.c.mailbox == mailbox
                )
            )
            .mappings()
            .first()
        )
    if row:
        return row.get("last_polled_at")
    return None


def update_mailbox_last_polled(mailbox: str, timestamp: datetime) -> None:
    _ensure_schema()
    with _get_engine().begin() as conn:
        existing = (
            conn.execute(
                select(email_watch_state_table.c.mailbox).where(
                    email_watch_state_table.c.mailbox == mailbox
                )
            )
            .mappings()
            .first()
        )
        if existing:
            conn.execute(
                update(email_watch_state_table)
                .where(email_watch_state_table.c.mailbox == mailbox)
                .values(last_polled_at=timestamp)
            )
        else:
            conn.execute(
                insert(email_watch_state_table).values(mailbox=mailbox, last_polled_at=timestamp)
            )

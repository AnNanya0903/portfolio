"""
Main MagicWand application views.
This provides the unified entry point for all MagicWand functionality.
"""

import csv
import json
import hashlib
import logging
import secrets
import struct
from collections import Counter
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
import re
import time
import math
from zoneinfo import ZoneInfo
from threading import Thread
from urllib.parse import urlsplit, urljoin
from typing import Any, Dict, Sequence, List, Optional
import requests
from requests import RequestException
from urllib.parse import quote, urlencode

from django.conf import settings
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model, login
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect, FileResponse, Http404, StreamingHttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.contrib.auth import logout
from django.contrib import messages
from django.utils import timezone
from django.urls import reverse
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, OperationalError, ProgrammingError, connection

from .deskera import DeskeraMRPClient, DeskeraMRPConfigError, DeskeraMRPError
from .mtc import (
    MTC_DOCX_MIME,
    MTCError,
    resolve_mtc_template,
    list_mtc_template_folders,
    list_mtc_templates,
    load_mtc_template_bytes,
    render_mtc_docx,
    build_mtc_filename,
)
from dashboard.ai_service import AIReplyService, AIReplyRequest, AIServiceError
from dashboard.gmail_service import GmailService, GmailAPIError
from dashboard.models import next_certificate_number, next_certificate_serial
from allauth.socialaccount.models import SocialAccount
from compounding.models import CompoundingIoTRun, CompoundingPlan, compounding_business_date
from .access import (
    SERVICE_DEFINITIONS,
    is_super_admin,
    require_any_service_access,
    require_service_access,
    user_has_service_access,
)
from .iot_store import coerce_epoch_ms, get_latest_payload, get_payload_history, set_latest_payload

try:
    from pypdf import PdfReader  # type: ignore
except Exception:
    PdfReader = None  # type: ignore

logger = logging.getLogger(__name__)

IOT_AI_SUMMARY_CACHE_PREFIX = "magicwand:iot:ai_summary"
IOT_AI_SUMMARY_CACHE_TTL_SECONDS = 600

IOT_SIGNAL_SPECS: List[Dict[str, Any]] = [
    {"register": 40001, "key": "frequency", "label": "Frequency", "unit": "Hz", "decode": "float32"},
    {"register": 40009, "key": "avg_pf", "label": "Average PF", "unit": "", "decode": "float32"},
    {"register": 40017, "key": "a_vrms_ll", "label": "Average Voltage L-L", "unit": "V", "decode": "float32"},
    {"register": 40025, "key": "r_irms", "label": "R Phase Current", "unit": "A", "decode": "float32"},
    {"register": 40027, "key": "y_irms", "label": "Y Phase Current", "unit": "A", "decode": "float32"},
    {"register": 40029, "key": "b_irms", "label": "B Phase Current", "unit": "A", "decode": "float32"},
    {
        "register": 40031,
        "key": "avg_current",
        "aliases": ["a_irms"],
        "label": "Average Current",
        "unit": "A",
        "decode": "float32",
    },
    {"register": 40041, "key": "t_watt", "label": "Average Watt", "unit": "W", "decode": "float32"},
    {"register": 40065, "key": "t_kwh_import", "label": "Total KWh", "unit": "kWh", "decode": "float32"},
    {
        "register": 40102,
        "key": "ch1_temperature",
        "aliases": ["pv_channel_1"],
        "label": "CH-1 Temperature",
        "unit": "°C",
        "decode": "uint16",
        "scale": 0.1,
        "precision": 1,
        "scaled_input_max": 150,
    },
    {
        "register": 40103,
        "key": "ch2_temperature",
        "aliases": ["pv_channel_2"],
        "label": "CH-2 Temperature",
        "unit": "°C",
        "decode": "uint16",
        "scale": 0.1,
        "precision": 1,
        "scaled_input_max": 150,
    },
]

DESKERA_PORTAL_LINKS = [
    {
        "slug": "workorders",
        "label": "Work Orders",
        "description": "Plan production, release jobs, and monitor fulfillment status across the shop floor.",
        "path": "/mrp/workorder",
        "icon": "bi-clipboard-check",
    },
    {
        "slug": "jobcards",
        "label": "Job Cards",
        "description": "Issue job cards with routing, material requirements, and progress tracking.",
        "path": "/mrp/job-card",
        "icon": "bi-card-checklist",
    },
    {
        "slug": "products",
        "label": "Products",
        "description": "Maintain the product master, BOMs, and finished goods specifications.",
        "path": "/mrp/products",
        "icon": "bi-box-seam",
    },
    {
        "slug": "operations",
        "label": "Operations",
        "description": "Configure routing steps, cycle times, and dependencies for each product line.",
        "path": "/mrp/operations",
        "icon": "bi-diagram-3",
    },
    {
        "slug": "workstations",
        "label": "Work Stations",
        "description": "Manage machine cells, capacity, and assignment rules for scheduling.",
        "path": "/mrp/work-stations",
        "icon": "bi-hammer",
    },
    {
        "slug": "operators",
        "label": "Operator List",
        "description": "Track operator availability, skills, and current allocations.",
        "path": "/mrp/operator-list",
        "icon": "bi-people",
    },
    {
        "slug": "scheduling",
        "label": "Scheduling",
        "description": "Visualize production loads and rebalance jobs across stations.",
        "path": "/mrp/scheduling",
        "icon": "bi-calendar3",
    },
    {
        "slug": "chart-of-accounts",
        "label": "Chart of Accounts",
        "description": "Jump directly into Deskera Accounting to manage the GL structure.",
        "path": "/accounting/coa",
        "icon": "bi-journal-text",
    },
    {
        "slug": "journal-entry",
        "label": "Journal Entries",
        "description": "Post journals and reconcile production costs with finance.",
        "path": "/accounting/journal-entry",
        "icon": "bi-book",
    },
]

WORKORDER_STATUS_CHOICES = [
    ("", "All statuses"),
    ("PLANNED", "Planned"),
    ("RELEASED", "Released"),
    ("IN_PROGRESS", "In Progress"),
    ("COMPLETED", "Completed"),
    ("CLOSED", "Closed"),
    ("CANCELLED", "Cancelled"),
]

PORTAL_CAD_GASKET_TYPES = {"AGR", "AGX", "AGS", "AGM"}


def _iot_local_testing_enabled() -> bool:
    return bool(getattr(settings, "DEBUG", False))


def _iot_bridge_enabled() -> bool:
    return bool((getattr(settings, "IOT_PORTAL_URL", "") or "").strip())


def _iot_dashboard_available() -> bool:
    return _iot_local_testing_enabled() or _iot_bridge_enabled()


def _iot_local_only_response() -> JsonResponse:
    return JsonResponse({"ok": False, "error": "Local-only endpoint"}, status=404)


def _require_iot_ingest_token(request) -> Optional[JsonResponse]:
    if _iot_local_testing_enabled():
        return None

    expected = (getattr(settings, "IOT_SERVICE_TOKEN", "") or "").strip()
    if not expected:
        return JsonResponse({"ok": False, "error": "IOT_SERVICE_TOKEN is not configured"}, status=503)

    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.startswith("Bearer "):
        return JsonResponse({"ok": False, "error": "Missing bearer token"}, status=401)

    token = auth_header[7:].strip()
    if not token or not secrets.compare_digest(token, expected):
        return JsonResponse({"ok": False, "error": "Invalid bearer token"}, status=401)

    return None


class IoTDashboardFetchError(RuntimeError):
    """Raised when Django cannot assemble dashboard data from the active IoT source."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _coerce_iot_timestamp(raw_ts: Any) -> Optional[int]:
    parsed_ms = coerce_epoch_ms(raw_ts)
    if parsed_ms is not None:
        return parsed_ms
    if isinstance(raw_ts, str):
        value = raw_ts.strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return coerce_epoch_ms(int(parsed.timestamp() * 1000))
    return None


def _coerce_iot_registers(raw_registers: Any) -> Optional[List[int]]:
    if not isinstance(raw_registers, list):
        return None
    if any(not isinstance(item, int) for item in raw_registers):
        return None
    return raw_registers


def _coerce_iot_raw_map(raw_map: Any) -> Dict[str, Any]:
    if not isinstance(raw_map, dict):
        return {}
    return {str(key): value for key, value in raw_map.items()}


def _coerce_iot_history_limit(
    raw_limit: Any,
    *,
    default: int = 180,
    maximum: int = 5000,
    allow_unbounded: bool = False,
) -> int:
    if allow_unbounded:
        if raw_limit is None:
            return 0
        if isinstance(raw_limit, str) and raw_limit.strip().lower() in {"", "0", "all", "none", "unbounded"}:
            return 0
    try:
        parsed = int(raw_limit)
    except (TypeError, ValueError):
        return default
    if allow_unbounded and parsed <= 0:
        return 0
    return max(1, min(parsed, maximum))


def _coerce_iot_max_points(
    raw_max_points: Any,
    *,
    default: int = 5000,
    maximum: int = 50000,
) -> int:
    try:
        parsed = int(raw_max_points)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def _downsample_iot_history(items: List[Dict[str, Any]], max_points: Optional[int]) -> List[Dict[str, Any]]:
    if not max_points or max_points <= 0 or len(items) <= max_points:
        return items
    if max_points == 1:
        return items[-1:]
    step = (len(items) - 1) / (max_points - 1)
    indexes = sorted({min(len(items) - 1, max(0, round(index * step))) for index in range(max_points)})
    return [items[index] for index in indexes]


def _coerce_iot_time_range(start_raw: Any, end_raw: Any) -> tuple[Optional[int], Optional[int]]:
    start_ts = _coerce_iot_timestamp(start_raw)
    end_ts = _coerce_iot_timestamp(end_raw)
    if start_ts is not None and end_ts is not None and end_ts < start_ts:
        raise ValueError("endTs must be greater than or equal to startTs")
    return start_ts, end_ts


def _iot_signal_value(spec: Dict[str, Any], registers: Sequence[int], raw_map: Dict[str, Any]) -> Any:
    """One signal's value: from the named raw map when the sender provides it, else decoded from registers."""
    raw_value = raw_map.get(spec["key"])
    if raw_value is None:
        for alias in spec.get("aliases", []):
            raw_value = raw_map.get(alias)
            if raw_value is not None:
                break
    if raw_value is not None:
        return _normalize_iot_signal_value(spec, raw_value, from_raw_map=True)
    return _decode_iot_signal_value(spec, registers)


# History points carry only what the chart and batch maths need; the full signal table is sent for `latest` only.
IOT_COMPACT_HISTORY_KEYS = ("avg_current", "t_watt", "t_kwh_import", "ch1_temperature", "ch2_temperature")


def _compact_iot_point(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    ts = _coerce_iot_timestamp(payload.get("ts"))
    if ts is None:
        return None
    registers = _coerce_iot_registers(payload.get("registers")) or []
    raw_map = _coerce_iot_raw_map(payload.get("raw"))
    point: Dict[str, Any] = {"ts": ts}
    for key in IOT_COMPACT_HISTORY_KEYS:
        spec = _iot_signal_spec(key)
        value = _iot_signal_value(spec, registers, raw_map) if spec else None
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            value = None
        elif isinstance(value, float) and not math.isfinite(value):
            value = None
        point[key] = round(float(value), 3) if value is not None else None
    return point


def _iot_day_range_ms(raw_date: Any) -> Optional[tuple[int, int]]:
    """Start and end (inclusive, ms) of a YYYY-MM-DD day in the dashboard time zone."""
    value = str(raw_date or "").strip()
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD")
    zone = ZoneInfo(getattr(settings, "IOT_DASHBOARD_TIME_ZONE", "Asia/Kolkata") or "Asia/Kolkata")
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


def _resolve_iot_request_range(params) -> tuple[Optional[int], Optional[int]]:
    """startTs/endTs from the query, inside the `date` day when one is given."""
    start_ts, end_ts = _coerce_iot_time_range(params.get("startTs"), params.get("endTs"))
    day_range = _iot_day_range_ms(params.get("date"))
    if day_range:
        day_start, day_end = day_range
        start_ts = day_start if start_ts is None else max(start_ts, day_start)
        end_ts = day_end if end_ts is None else min(end_ts, day_end)
        if end_ts < start_ts:
            raise ValueError("startTs/endTs fall outside the selected date")
    return start_ts, end_ts


def _build_iot_signal_rows(registers: Sequence[int], raw_map: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for spec in IOT_SIGNAL_SPECS:
        value = _iot_signal_value(spec, registers, raw_map)
        rows.append(
            {
                "register": spec["register"],
                "key": spec["key"],
                "label": spec["label"],
                "unit": spec.get("unit", ""),
                "value": value,
                "displayValue": _format_iot_signal_value(value, spec.get("unit", ""), spec.get("precision")),
            }
        )
    return rows


def _build_iot_register_rows(registers: Sequence[int]) -> List[Dict[str, Any]]:
    return [
        {
            "index": index,
            "register": 40001 + index,
            "value": value,
        }
        for index, value in enumerate(registers)
    ]


def _decode_modbus_float32(registers: Sequence[int], start_register: int) -> Optional[float]:
    index = start_register - 40001
    if index < 0 or index + 1 >= len(registers):
        return None
    try:
        raw = struct.pack(">HH", int(registers[index]), int(registers[index + 1]))
        return round(struct.unpack(">f", raw)[0], 3)
    except Exception:
        return None


def _decode_modbus_uint16(registers: Sequence[int], start_register: int) -> Optional[int]:
    index = start_register - 40001
    if index < 0 or index >= len(registers):
        return None
    try:
        return int(registers[index])
    except Exception:
        return None


def _normalize_iot_signal_value(
    spec: Dict[str, Any],
    value: Any,
    *,
    from_raw_map: bool = False,
) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if not isinstance(value, (int, float, Decimal)):
        return value

    numeric = float(value)
    scale = spec.get("scale")
    precision = spec.get("precision")
    if scale is not None:
        scaled_input_max = float(spec.get("scaled_input_max", 150))
        # Older payloads stored raw temperature integers like 410/409.
        if from_raw_map and abs(numeric) <= scaled_input_max:
            scaled = numeric
        else:
            scaled = numeric * float(scale)
    else:
        scaled = numeric

    if precision is not None:
        return round(scaled, int(precision))
    if isinstance(value, float):
        return round(scaled, 3)
    if isinstance(value, int) and scale is None:
        return int(scaled)
    return scaled


def _decode_iot_signal_value(spec: Dict[str, Any], registers: Sequence[int]) -> Any:
    decoder = spec.get("decode")
    register = int(spec["register"])
    if decoder == "float32":
        return _normalize_iot_signal_value(spec, _decode_modbus_float32(registers, register))
    if decoder == "uint16":
        return _normalize_iot_signal_value(spec, _decode_modbus_uint16(registers, register))
    return None


def _format_iot_signal_value(value: Any, unit: str, precision: Any = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if precision is not None:
            rendered = f"{value:.{int(precision)}f}"
        else:
            rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        rendered = str(value)
    if unit:
        return f"{rendered} {unit}"
    return rendered


def _serialize_iot_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    registers = _coerce_iot_registers(payload.get("registers")) or []
    raw_map = _coerce_iot_raw_map(payload.get("raw"))
    signal_rows = _build_iot_signal_rows(registers, raw_map)
    serialized = {
        "machineId": _iot_public_machine_id(payload.get("machineId")),
        "ts": payload.get("ts"),
        "registers": registers,
        "raw": raw_map,
        "signalRows": signal_rows,
        "signalsByKey": {row["key"]: row["value"] for row in signal_rows},
        "registerRows": _build_iot_register_rows(registers),
    }
    return serialized


def _iot_public_machine_id(machine_id: Any) -> str:
    rendered = str(machine_id or "").strip()
    public_id = (getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
    source_id = (getattr(settings, "IOT_MACHINE_ID_SOURCE", "") or "").strip()
    if rendered and source_id and public_id and rendered == source_id:
        return public_id
    return rendered


def _iot_source_machine_id(machine_id: Any) -> str:
    requested = str(machine_id or "").strip()
    public_id = (getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
    source_id = (getattr(settings, "IOT_MACHINE_ID_SOURCE", "") or "").strip()
    if requested and source_id and public_id and requested == public_id:
        return source_id
    return requested


def _build_iot_bridge_url(path: str) -> str:
    base_url = (getattr(settings, "IOT_PORTAL_URL", "") or "").strip()
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise IoTDashboardFetchError("IOT_PORTAL_URL is not configured as an absolute URL", status_code=503)

    suffix = path if path.startswith("/") else f"/{path}"
    base_path = (parsed.path or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{base_path}{suffix}"


def _iot_payload_from_sample(machine_id: str, sample) -> Dict[str, Any]:
    """A stored sample shaped like a sender payload, for when no full payload is at hand."""
    return {
        "machineId": machine_id,
        "ts": sample.ts_ms,
        "registers": [],
        "raw": {field: getattr(sample, field) for field in sample.VALUE_FIELDS if getattr(sample, field) is not None},
    }


def _iot_dashboard_data_from_store(
    machine_id: str,
    limit: int,
    start_ts: Optional[int],
    end_ts: Optional[int],
    max_points: Optional[int],
    since_ts: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Dashboard data from Django's own sample store, or None when it holds nothing for this view yet
    (e.g. a range from before samples were first stored), so the caller can fall back to the bridge."""
    from compounding import iot_samples
    from compounding.models import IoTSample

    has_time_range = start_ts is not None or end_ts is not None
    if has_time_range:
        if not iot_samples.has_samples(machine_id, start_ts, end_ts):
            return None
    elif not iot_samples.has_samples(machine_id, int(time.time() * 1000) - 10 * 60 * 1000):
        return None

    history = iot_samples.compact_history(
        machine_id, start_ts=start_ts, end_ts=end_ts, limit=0 if has_time_range else limit, since_ts=since_ts
    )
    raw_count = len(history)
    if since_ts is None:
        history = iot_samples.downsample_points(history, max_points)

    range_query = IoTSample.objects.filter(machine_id=machine_id)
    if start_ts is not None:
        range_query = range_query.filter(ts_ms__gte=start_ts)
    if end_ts is not None:
        range_query = range_query.filter(ts_ms__lte=end_ts)
    newest = range_query.order_by("-ts_ms").first()
    latest_payload = None
    if not has_time_range:
        cached = (
            get_latest_payload() if _iot_local_testing_enabled() else iot_samples.cached_latest_payload(machine_id)
        )
        if cached and newest and (_coerce_iot_timestamp(cached.get("ts")) or 0) >= newest.ts_ms:
            latest_payload = {**cached, "machineId": machine_id}
    if latest_payload is None and newest is not None:
        latest_payload = _iot_payload_from_sample(machine_id, newest)

    return {
        "ok": True,
        "source": "database",
        "machineId": machine_id,
        "limit": limit,
        "historyCount": raw_count,
        "returnedCount": len(history),
        "historyLimited": not has_time_range and limit > 0,
        "historyDownsampled": raw_count > len(history),
        "maxPoints": max_points or 0,
        "startTs": start_ts,
        "endTs": end_ts,
        "sinceTs": since_ts,
        "latest": _serialize_iot_payload(latest_payload),
        "history": history,
    }


def _fetch_iot_dashboard_data(
    machine_id: str,
    limit: int,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    max_points: Optional[int] = None,
    since_ts: Optional[int] = None,
) -> Dict[str, Any]:
    public_machine_id = _iot_public_machine_id(machine_id) or str(machine_id or "").strip()
    source_machine_id = _iot_source_machine_id(machine_id) or str(machine_id or "").strip()

    # Samples are kept in Django's database (compounding.IoTSample); pull anything new from the bridge
    # first (throttled), then answer from the store whenever it covers the requested view.
    from compounding import iot_samples

    iot_samples.sync_recent(public_machine_id)
    try:
        stored = _iot_dashboard_data_from_store(public_machine_id, limit, start_ts, end_ts, max_points, since_ts)
    except DatabaseError:
        logger.exception("IoT sample store read failed; falling back to the live source")
        stored = None
    if stored is not None:
        return stored

    if _iot_local_testing_enabled():
        history = [
            _compact_iot_point(item)
            for item in get_payload_history(limit=limit, start_ts=start_ts, end_ts=end_ts)
        ]
        history_items = [item for item in history if item]
        raw_count = len(history_items)
        history_items = _downsample_iot_history(history_items, max_points)
        return {
            "ok": True,
            "source": "local",
            "machineId": public_machine_id,
            "limit": limit,
            "historyCount": raw_count,
            "returnedCount": len(history_items),
            "historyLimited": limit > 0,
            "historyDownsampled": raw_count > len(history_items),
            "maxPoints": max_points or 0,
            "startTs": start_ts,
            "endTs": end_ts,
            "latest": _serialize_iot_payload(get_latest_payload(start_ts=start_ts, end_ts=end_ts)),
            "history": history_items,
        }

    if not _iot_bridge_enabled():
        raise IoTDashboardFetchError("IoT bridge is not configured", status_code=404)

    token = (getattr(settings, "IOT_SERVICE_TOKEN", "") or "").strip()
    if not token:
        raise IoTDashboardFetchError("IOT_SERVICE_TOKEN is not configured", status_code=503)

    upstream_url = _build_iot_bridge_url("/dashboard-data")
    try:
        params: Dict[str, Any] = {"machineId": source_machine_id, "limit": limit}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if max_points:
            params["maxPoints"] = max_points
        response = requests.get(
            upstream_url,
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=getattr(settings, "IOT_PROXY_TIMEOUT", 20),
        )
    except RequestException as exc:
        logger.warning("IoT bridge fetch failed for machine=%s (source=%s): %s", public_machine_id, source_machine_id, exc)
        raise IoTDashboardFetchError(f"IoT bridge request failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning("IoT bridge returned non-JSON response for machine=%s (source=%s)", public_machine_id, source_machine_id)
        raise IoTDashboardFetchError("IoT bridge returned invalid JSON") from exc

    if response.status_code >= 400:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("error") or "").strip()
        message = detail or f"IoT bridge returned HTTP {response.status_code}"
        raise IoTDashboardFetchError(message)

    if not isinstance(body, dict):
        raise IoTDashboardFetchError("IoT bridge returned an unexpected payload")

    latest_payload = body.get("latest") if isinstance(body.get("latest"), dict) else None
    raw_history = body.get("history")
    history_items: List[Dict[str, Any]] = []
    if isinstance(raw_history, list):
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            point = _compact_iot_point(item)
            if point:
                history_items.append(point)
    raw_history_count = len(history_items)
    # The bridge may ignore maxPoints on older builds; never pass more than asked on to the browser.
    history_items = _downsample_iot_history(history_items, max_points)

    response_limit = body.get("limit") if body.get("limit") is not None else limit
    response_max_points = body.get("maxPoints") if body.get("maxPoints") is not None else (max_points or 0)
    return {
        "ok": True,
        "source": "bridge",
        "machineId": _iot_public_machine_id(body.get("machineId")) or public_machine_id,
        "limit": int(response_limit),
        "historyCount": int(body.get("historyCount") or raw_history_count),
        "returnedCount": len(history_items),
        "historyLimited": bool(body.get("historyLimited")),
        "historyDownsampled": bool(body.get("historyDownsampled")) or raw_history_count > len(history_items),
        "maxPoints": int(response_max_points),
        "startTs": _coerce_iot_timestamp(body.get("startTs")) or start_ts,
        "endTs": _coerce_iot_timestamp(body.get("endTs")) or end_ts,
        "latest": _serialize_iot_payload(latest_payload),
        "history": history_items,
    }


def _iot_signal_spec(key: str) -> Optional[Dict[str, Any]]:
    needle = str(key or "").strip()
    if not needle:
        return None
    for spec in IOT_SIGNAL_SPECS:
        if spec["key"] == needle or needle in spec.get("aliases", []):
            return spec
    return None


def _coerce_iot_numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _format_iot_summary_value(value: Optional[float], unit: str = "") -> str:
    if value is None:
        return "—"
    if unit == "°C":
        rendered = f"{value:.1f}"
    elif unit in {"A", "Hz", "V"}:
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    elif unit == "W":
        rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    elif unit == "kWh":
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}".strip()


def _format_iot_summary_ts(raw_ts: Any) -> str:
    parsed = _coerce_iot_timestamp(raw_ts)
    if parsed is not None:
        dt = datetime.fromtimestamp(parsed / 1000, tz=timezone.utc).astimezone(timezone.get_current_timezone())
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    if isinstance(raw_ts, str):
        value = raw_ts.strip()
        if value:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.get_current_timezone())
                else:
                    dt = dt.astimezone(timezone.get_current_timezone())
                return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            except ValueError:
                pass
    return "Unknown"


def _build_iot_history_series(history: Sequence[Dict[str, Any]], key: str) -> List[float]:
    values: List[float] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        # Compact history points carry the value at the top level; older full payloads nest it.
        signals = item.get("signalsByKey") if isinstance(item.get("signalsByKey"), dict) else item
        numeric = _coerce_iot_numeric(signals.get(key))
        if numeric is not None:
            values.append(numeric)
    return values


def _build_iot_series_summary(history: Sequence[Dict[str, Any]], key: str) -> Optional[str]:
    spec = _iot_signal_spec(key)
    if not spec:
        return None
    values = _build_iot_history_series(history, key)
    if not values:
        return None
    latest = values[-1]
    minimum = min(values)
    maximum = max(values)
    average = sum(values) / len(values)
    delta = latest - values[0]
    dynamic_range = maximum - minimum
    threshold = max(abs(latest) * 0.03, dynamic_range * 0.25, 0.25 if spec.get("unit") == "°C" else 0.05)
    trend = "stable" if abs(delta) <= threshold else ("rising" if delta > 0 else "falling")
    return (
        f"- {spec['label']}: {trend}; latest {_format_iot_summary_value(latest, spec.get('unit', ''))}; "
        f"min {_format_iot_summary_value(minimum, spec.get('unit', ''))}; "
        f"max {_format_iot_summary_value(maximum, spec.get('unit', ''))}; "
        f"avg {_format_iot_summary_value(average, spec.get('unit', ''))}."
    )


def _build_iot_signal_line(latest: Dict[str, Any], key: str) -> Optional[str]:
    signal_rows = latest.get("signalRows")
    if not isinstance(signal_rows, list):
        return None
    for row in signal_rows:
        if not isinstance(row, dict) or row.get("key") != key:
            continue
        label = row.get("label") or key
        value = row.get("displayValue") or "—"
        return f"- {label}: {value}"
    return None


def _build_iot_derived_observations(latest: Dict[str, Any]) -> List[str]:
    signals = latest.get("signalsByKey")
    if not isinstance(signals, dict):
        return []

    observations: List[str] = []

    ch1 = _coerce_iot_numeric(signals.get("ch1_temperature"))
    ch2 = _coerce_iot_numeric(signals.get("ch2_temperature"))
    if ch1 is not None and ch2 is not None:
        observations.append(f"- Channel temperature spread: {abs(ch1 - ch2):.1f} °C.")

    phase_currents = [
        _coerce_iot_numeric(signals.get("r_irms")),
        _coerce_iot_numeric(signals.get("y_irms")),
        _coerce_iot_numeric(signals.get("b_irms")),
    ]
    if all(value is not None for value in phase_currents):
        valid_currents = [float(value) for value in phase_currents if value is not None]
        average_current = sum(valid_currents) / len(valid_currents)
        spread = max(valid_currents) - min(valid_currents)
        if average_current > 0:
            observations.append(
                f"- Phase current spread: {spread:.3f} A ({(spread / average_current) * 100:.1f}% of average phase current)."
            )

    power_factor = _coerce_iot_numeric(signals.get("avg_pf"))
    if power_factor is not None:
        observations.append(f"- Power factor: {power_factor:.3f}.")

    return observations


def _build_iot_production_run_context(
    machine_id: str,
    *,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    target_machine_id = str(machine_id or "").strip()
    if not target_machine_id:
        return None

    start_dt = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc) if start_ts is not None else None
    end_dt = datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc) if end_ts is not None else None

    try:
        recent_runs = list(
            CompoundingIoTRun.objects.filter(machine_id=target_machine_id)
            .select_related("plan")
            .order_by("-started_at", "-id")[:30]
        )
    except (ProgrammingError, OperationalError) as exc:
        logger.warning(
            "IoT production context unavailable for machine=%s because compounding run tables are missing: %s",
            target_machine_id,
            exc,
        )
        return None

    if not recent_runs:
        return None

    selected_run: Optional[CompoundingIoTRun] = None
    if start_dt is not None or end_dt is not None:
        for candidate in recent_runs:
            # A batch still running (no end yet) overlaps any window after its start.
            if start_dt is not None and candidate.ended_at is not None and candidate.ended_at < start_dt:
                continue
            if end_dt is not None and candidate.started_at > end_dt:
                continue
            selected_run = candidate
            break
    if selected_run is None and start_dt is None and end_dt is None:
        selected_run = recent_runs[0]
    if selected_run is None:
        return None

    return {
        "id": selected_run.id,
        "machine_id": selected_run.machine_id,
        "plan_date": selected_run.plan.plan_date.isoformat() if selected_run.plan_id else "",
        "compound_code": selected_run.compound_code,
        "started_at": selected_run.started_at.isoformat() if selected_run.started_at else "",
        "ended_at": selected_run.ended_at.isoformat() if selected_run.ended_at else "",
        "stock_quality": selected_run.stock_quality,
        "stock_quality_label": selected_run.get_stock_quality_display(),
        "notes": selected_run.notes or "",
        "updated_at": selected_run.updated_at.isoformat() if selected_run.updated_at else "",
    }


def _build_iot_summary_snapshot(data: Dict[str, Any], production_run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else None
    history = list(data.get("history") or [])
    first_ts = _coerce_iot_timestamp(history[0].get("ts")) if history else None
    latest_ts = _coerce_iot_timestamp(latest.get("ts")) if latest else None
    window_seconds: Optional[int] = None
    if first_ts is not None and latest_ts is not None and latest_ts >= first_ts:
        window_seconds = int((latest_ts - first_ts) / 1000)

    return {
        "source": data.get("source"),
        "machineId": data.get("machineId"),
        "history_records": len(history),
        "latest_ts": latest_ts,
        "window_seconds": window_seconds,
        "production_run": production_run or None,
    }


def _build_iot_summary_cache_key(
    machine_id: str,
    data: Dict[str, Any],
    limit: int,
    production_run: Optional[Dict[str, Any]] = None,
) -> str:
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else None
    history = list(data.get("history") or [])
    snapshot = {
        "machineId": machine_id,
        "source": data.get("source"),
        "limit": limit,
        "latest": latest,
        "history": history[-30:],
        "production_run": production_run or None,
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"{IOT_AI_SUMMARY_CACHE_PREFIX}:{machine_id}:{digest}"


def _build_iot_summary_prompt(data: Dict[str, Any], production_run: Optional[Dict[str, Any]] = None) -> str:
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    history = list(data.get("history") or [])
    snapshot = _build_iot_summary_snapshot(data, production_run)

    latest_signal_lines = [
        line
        for line in [
            _build_iot_signal_line(latest, "avg_current"),
            _build_iot_signal_line(latest, "ch1_temperature"),
            _build_iot_signal_line(latest, "ch2_temperature"),
            _build_iot_signal_line(latest, "frequency"),
            _build_iot_signal_line(latest, "avg_pf"),
            _build_iot_signal_line(latest, "t_watt"),
            _build_iot_signal_line(latest, "a_vrms_ll"),
            _build_iot_signal_line(latest, "r_irms"),
            _build_iot_signal_line(latest, "y_irms"),
            _build_iot_signal_line(latest, "b_irms"),
            _build_iot_signal_line(latest, "t_kwh_import"),
        ]
        if line
    ]

    trend_lines = [
        line
        for line in [
            _build_iot_series_summary(history, "avg_current"),
            _build_iot_series_summary(history, "ch1_temperature"),
            _build_iot_series_summary(history, "ch2_temperature"),
            _build_iot_series_summary(history, "frequency"),
            _build_iot_series_summary(history, "avg_pf"),
            _build_iot_series_summary(history, "t_watt"),
        ]
        if line
    ]

    derived_lines = _build_iot_derived_observations(latest)
    history_window = snapshot.get("window_seconds")
    history_window_label = f"{history_window} seconds" if isinstance(history_window, int) else "unknown window"
    production_context_lines: List[str] = []
    if production_run:
        production_context_lines.append(f"- Compound prepared: {production_run.get('compound_code') or 'Unknown'}")
        production_context_lines.append(f"- Logged run window: {_format_iot_summary_ts(production_run.get('started_at'))} to {_format_iot_summary_ts(production_run.get('ended_at'))}")
        production_context_lines.append(f"- Quality of stock produced: {production_run.get('stock_quality_label') or production_run.get('stock_quality') or 'Unknown'}")
        if production_run.get("plan_date"):
            production_context_lines.append(f"- Compounding plan date: {production_run.get('plan_date')}")
        if production_run.get("notes"):
            production_context_lines.append(f"- Operator notes: {production_run.get('notes')}")

    return (
        "You are an industrial operations copilot helping a compounding team interpret live telemetry "
        "from an extrusion mill.\n"
        "Use only the data below. Do not invent sensor mappings or mechanical faults that are not supported by the telemetry.\n"
        "When production context is provided, use it to explain the compound produced, stock quality, and any note-based caveats together with the telemetry.\n"
        "If the evidence is weak, say 'likely' or 'possibly'. Keep the answer concise and practical.\n\n"
        "Return exactly these sections:\n"
        "Current state:\n"
        "Likely machine behavior:\n"
        "Risks or anomalies:\n"
        "Recommended checks:\n\n"
        f"Machine: {data.get('machineId') or 'Unknown'}\n"
        f"Data source: {data.get('source') or 'unknown'}\n"
        f"Latest sample timestamp: {_format_iot_summary_ts(latest.get('ts'))}\n"
        f"History samples: {len(history)} over {history_window_label}\n\n"
        "Production context:\n"
        f"{chr(10).join(production_context_lines) if production_context_lines else '- No production run was logged for this summary window.'}\n\n"
        "Latest signals:\n"
        f"{chr(10).join(latest_signal_lines) if latest_signal_lines else '- No latest signal rows available.'}\n\n"
        "Recent trends:\n"
        f"{chr(10).join(trend_lines) if trend_lines else '- No recent numeric trend data available.'}\n\n"
        "Derived observations:\n"
        f"{chr(10).join(derived_lines) if derived_lines else '- No derived observations available.'}"
    )


def _build_iot_fallback_summary(data: Dict[str, Any], production_run: Optional[Dict[str, Any]] = None) -> str:
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    signals = latest.get("signalsByKey") if isinstance(latest.get("signalsByKey"), dict) else {}
    history = list(data.get("history") or [])

    avg_current = _coerce_iot_numeric(signals.get("avg_current"))
    ch1 = _coerce_iot_numeric(signals.get("ch1_temperature"))
    ch2 = _coerce_iot_numeric(signals.get("ch2_temperature"))
    frequency = _coerce_iot_numeric(signals.get("frequency"))
    power_factor = _coerce_iot_numeric(signals.get("avg_pf"))

    state_line = "Telemetry is present, but there is not enough context to classify the mill state."
    behavior_line = "Likely machine behavior is uncertain from the current signal set alone."
    if avg_current is not None:
        mean_temp = None
        if ch1 is not None and ch2 is not None:
            mean_temp = (ch1 + ch2) / 2
        elif ch1 is not None:
            mean_temp = ch1
        elif ch2 is not None:
            mean_temp = ch2

        if avg_current < 1 and (mean_temp is None or mean_temp < 50):
            state_line = "Electrical load is very low and temperatures are relatively cool."
            behavior_line = "The extrusion mill is likely idle, stopped, or waiting between runs."
        elif avg_current < 2 and mean_temp is not None and mean_temp >= 50:
            state_line = "Electrical load is low while temperatures remain elevated."
            behavior_line = "The line is likely in warm-up, heat-soak, or holding temperature without significant production load."
        elif avg_current >= 2 and mean_temp is not None and mean_temp >= 50:
            state_line = "Electrical load and temperature are both elevated."
            behavior_line = "The extrusion mill is likely running or actively processing material."
        elif avg_current >= 2:
            state_line = "Electrical load is present but temperature channels are not especially high."
            behavior_line = "The mill is likely rotating or lightly loaded, but not yet in a fully hot steady-state condition."

    risks: List[str] = []
    if ch1 is not None and ch2 is not None and abs(ch1 - ch2) > 10:
        risks.append(f"Temperature channels differ by {abs(ch1 - ch2):.1f} °C, which may indicate uneven heating.")
    if power_factor is not None and power_factor < 0.7:
        risks.append(f"Power factor is low at {power_factor:.3f}.")
    if frequency is not None and abs(frequency - 50.0) > 1.0:
        risks.append(f"Frequency is away from the expected 50 Hz region at {frequency:.3f} Hz.")
    avg_current_series = _build_iot_history_series(history, "avg_current")
    if len(avg_current_series) >= 3:
        spread = max(avg_current_series) - min(avg_current_series)
        if avg_current is not None and avg_current > 0 and spread / avg_current > 0.35:
            risks.append("Average current is swinging materially across the recent window, suggesting unstable load.")

    checks: List[str] = []
    if avg_current is not None and avg_current < 2:
        checks.append("Confirm whether the line is expected to be idle, in warm-up, or waiting for feed.")
    else:
        checks.append("Verify product feed, screw load, and die condition if load or temperature moves unexpectedly.")
    if ch1 is not None and ch2 is not None and abs(ch1 - ch2) > 10:
        checks.append("Check heater zones and sensor placement for CH-1 and CH-2.")
    if power_factor is not None and power_factor < 0.7:
        checks.append("Inspect drive/load conditions if low power factor persists.")

    if production_run:
        compound_code = production_run.get("compound_code") or "the logged compound"
        quality_label = production_run.get("stock_quality_label") or production_run.get("stock_quality") or "Unknown"
        state_line = f"{state_line} The logged compound for this run is {compound_code}."
        behavior_line = f"{behavior_line} The recorded stock quality is {quality_label}."
        notes = str(production_run.get("notes") or "").strip()
        if quality_label.lower() in {"hold", "rework", "average", "rejected"}:
            risks.append(f"Stock quality was logged as {quality_label}, so telemetry should be reviewed against the run outcome.")
        if notes:
            checks.append(f"Review operator note: {notes}")

    return (
        "Current state:\n"
        f"- {state_line}\n\n"
        "Likely machine behavior:\n"
        f"- {behavior_line}\n\n"
        "Risks or anomalies:\n"
        f"{chr(10).join(f'- {item}' for item in risks) if risks else '- No obvious anomaly is visible from the current snapshot alone.'}\n\n"
        "Recommended checks:\n"
        f"{chr(10).join(f'- {item}' for item in checks) if checks else '- Continue monitoring the live trends.'}"
    )


def _generate_iot_summary_text(data: Dict[str, Any], production_run: Optional[Dict[str, Any]] = None) -> str:
    prompt = _build_iot_summary_prompt(data, production_run)
    try:
        ai_service = AIReplyService()
    except AIServiceError:
        logger.warning("Gemini unavailable for IoT summary; using heuristic fallback.")
        return _build_iot_fallback_summary(data, production_run)

    if getattr(ai_service, "_using_stub", False) is True:
        return _build_iot_fallback_summary(data, production_run)

    try:
        summary = ai_service.generate_reply(
            AIReplyRequest(
                thread_summary=prompt,
                purpose="assistant",
            )
        ).strip()
    except AIServiceError as exc:
        logger.warning("IoT AI summary generation failed: %s", exc)
        return _build_iot_fallback_summary(data, production_run)

    return summary or _build_iot_fallback_summary(data, production_run)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def iot_ingest(request):
    if request.method == "GET" and not _iot_local_testing_enabled():
        return _iot_local_only_response()

    auth_error = _require_iot_ingest_token(request)
    if auth_error is not None:
        return auth_error

    if request.method == "GET":
        history = [_serialize_iot_payload(item) for item in get_payload_history(limit=180)]
        return JsonResponse(
            {
                "ok": True,
                "latest": _serialize_iot_payload(get_latest_payload()),
                "history": [item for item in history if item],
            }
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body"}, status=400)

    machine_id = payload.get("machineId")
    ts = _coerce_iot_timestamp(payload.get("ts"))
    if ts is None:
        ts = _coerce_iot_timestamp(payload.get("timestamp"))
    registers = _coerce_iot_registers(payload.get("registers"))
    raw_map = _coerce_iot_raw_map(payload.get("raw"))

    if not isinstance(machine_id, str) or not machine_id.strip():
        return JsonResponse({"ok": False, "error": "machineId must be a non-empty string"}, status=400)
    if ts is None:
        return JsonResponse({"ok": False, "error": "ts must be an integer timestamp in milliseconds"}, status=400)
    if registers is None and not raw_map:
        return JsonResponse(
            {"ok": False, "error": "registers must be a list of integers, or provide a raw mapping"},
            status=400,
        )
    registers = registers or []

    latest = set_latest_payload({
        "machineId": machine_id.strip(),
        "ts": ts,
        "registers": registers,
        "raw": raw_map,
    })
    # Keep every sample, not just the in-memory tail, so days and batches can be replayed.
    try:
        from compounding import iot_samples

        public_id = _iot_public_machine_id(latest["machineId"]) or latest["machineId"]
        iot_samples.store_payloads(public_id, [latest])
        iot_samples.remember_latest_payload(public_id, latest)
    except DatabaseError:
        logger.exception("IoT ingest could not store the sample")
    logger.info(
        "IOT ingest received machine=%s ts=%s register_count=%s signal_count=%s",
        latest["machineId"],
        latest["ts"],
        len(latest["registers"]),
        len(raw_map),
    )
    return JsonResponse({"ok": True, "latest": _serialize_iot_payload(latest)})


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"], deny="json")
@require_http_methods(["GET"])
def iot_dashboard_data(request):
    if not _iot_dashboard_available():
        return JsonResponse({"ok": False, "error": "IoT dashboard is not configured"}, status=404)

    machine_id = (request.GET.get("machineId") or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
    if not machine_id:
        return JsonResponse({"ok": False, "error": "machineId is required"}, status=400)

    try:
        start_ts, end_ts = _resolve_iot_request_range(request.GET)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc), "machineId": machine_id}, status=400)
    has_time_range = start_ts is not None or end_ts is not None
    limit = _coerce_iot_history_limit(request.GET.get("limit"), allow_unbounded=has_time_range)
    # Always bound what goes to the browser: compact points are ~100 bytes, so the default keeps a response
    # well under 300 KB even for a whole day.
    max_points = _coerce_iot_max_points(
        request.GET.get("maxPoints"),
        default=getattr(settings, "IOT_HISTORY_API_MAX_POINTS", 2000),
        maximum=getattr(settings, "IOT_HISTORY_API_MAX_POINTS_LIMIT", 2500),
    )
    since_ts = _coerce_iot_timestamp(request.GET.get("sinceTs"))
    try:
        data = _fetch_iot_dashboard_data(
            machine_id,
            limit,
            start_ts=start_ts,
            end_ts=end_ts,
            max_points=max_points,
            since_ts=since_ts,
        )
        data["batchState"] = _iot_batch_state(_iot_public_machine_id(machine_id) or machine_id)
        return JsonResponse(data)
    except IoTDashboardFetchError as exc:
        return JsonResponse({"ok": False, "error": str(exc), "machineId": machine_id}, status=exc.status_code)


def _iot_batch_state(machine_id: str) -> Optional[Dict[str, Any]]:
    """Live machine/batch state for the "Machine running - no batch logged" banner."""
    try:
        from compounding import iot_batches

        return iot_batches.machine_state(machine_id)
    except DatabaseError:
        logger.exception("IoT batch state lookup failed")
        return None


def _stream_local_iot_export(
    machine_id: str,
    start_ts: Optional[int],
    end_ts: Optional[int],
    export_format: str,
):
    history = get_payload_history(limit=0, start_ts=start_ts, end_ts=end_ts)
    rows = [
        _serialize_iot_payload(item)
        for item in history
        if item and _iot_public_machine_id(item.get("machineId")) == _iot_public_machine_id(machine_id)
    ]
    if export_format == "csv":
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=["machineId", "ts", "receivedAt", "registers", "raw"])
        writer.writeheader()
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        for row in rows:
            writer.writerow(
                {
                    "machineId": row.get("machineId", ""),
                    "ts": row.get("ts", ""),
                    "receivedAt": row.get("receivedAt", ""),
                    "registers": json.dumps(row.get("registers", []), separators=(",", ":"), ensure_ascii=False),
                    "raw": json.dumps(row.get("raw", {}), separators=(",", ":"), ensure_ascii=False),
                }
            )
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
    else:
        for row in rows:
            yield json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"], deny="json")
@require_http_methods(["GET"])
def iot_history_export(request):
    if not _iot_dashboard_available():
        return JsonResponse({"ok": False, "error": "IoT dashboard is not configured"}, status=404)

    machine_id = (request.GET.get("machineId") or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
    if not machine_id:
        return JsonResponse({"ok": False, "error": "machineId is required"}, status=400)

    try:
        start_ts, end_ts = _resolve_iot_request_range(request.GET)
    except ValueError as exc:
        return JsonResponse({"ok": False, "error": str(exc), "machineId": machine_id}, status=400)

    export_format = (request.GET.get("format") or "csv").strip().lower()
    if export_format not in {"csv", "ndjson", "jsonl"}:
        return JsonResponse({"ok": False, "error": "format must be csv, ndjson, or jsonl"}, status=400)
    normalized_format = "ndjson" if export_format == "jsonl" else export_format
    extension = "csv" if normalized_format == "csv" else "ndjson"
    content_type = "text/csv" if normalized_format == "csv" else "application/x-ndjson"
    filename_machine = re.sub(r"[^A-Za-z0-9_.-]+", "-", machine_id).strip("-") or "machine"
    filename = f"iot-{filename_machine}-{start_ts or 'start'}-{end_ts or 'end'}.{extension}"

    if _iot_local_testing_enabled():
        response = StreamingHttpResponse(
            _stream_local_iot_export(machine_id, start_ts, end_ts, normalized_format),
            content_type=content_type,
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    if not _iot_bridge_enabled():
        return JsonResponse({"ok": False, "error": "IoT bridge is not configured"}, status=404)

    token = (getattr(settings, "IOT_SERVICE_TOKEN", "") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "error": "IOT_SERVICE_TOKEN is not configured"}, status=503)

    source_machine_id = _iot_source_machine_id(machine_id) or machine_id
    params: Dict[str, Any] = {"machineId": source_machine_id, "format": normalized_format}
    if start_ts is not None:
        params["startTs"] = start_ts
    if end_ts is not None:
        params["endTs"] = end_ts

    try:
        upstream_response = requests.get(
            _build_iot_bridge_url("/history/export"),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=getattr(settings, "IOT_PROXY_TIMEOUT", 20),
        )
    except RequestException as exc:
        logger.warning("IoT bridge export failed for machine=%s: %s", machine_id, exc)
        return JsonResponse({"ok": False, "error": f"IoT bridge export failed: {exc}", "machineId": machine_id}, status=502)

    if upstream_response.status_code >= 400:
        detail = ""
        try:
            body = upstream_response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("error") or "").strip()
        except ValueError:
            detail = upstream_response.text[:200]
        upstream_response.close()
        return JsonResponse(
            {"ok": False, "error": detail or f"IoT bridge returned HTTP {upstream_response.status_code}", "machineId": machine_id},
            status=upstream_response.status_code,
        )

    def stream_bridge_chunks():
        try:
            for chunk in upstream_response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream_response.close()

    response = StreamingHttpResponse(stream_bridge_chunks(), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"], deny="json")
@require_http_methods(["GET"])
def iot_dashboard_summary(request):
    try:
        if not _iot_dashboard_available():
            return JsonResponse({"ok": False, "error": "IoT dashboard is not configured"}, status=404)

        machine_id = (request.GET.get("machineId") or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        if not machine_id:
            return JsonResponse({"ok": False, "error": "machineId is required"}, status=400)

        limit = _coerce_iot_history_limit(request.GET.get("limit"), default=120, maximum=600)
        try:
            start_ts, end_ts = _resolve_iot_request_range(request.GET)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc), "machineId": machine_id}, status=400)
        force_param = (request.GET.get("force") or request.GET.get("refresh") or "").strip().lower()
        force = force_param in {"1", "true", "yes", "on"}

        try:
            data = _fetch_iot_dashboard_data(machine_id, limit, start_ts=start_ts, end_ts=end_ts)
        except IoTDashboardFetchError as exc:
            return JsonResponse({"ok": False, "error": str(exc), "machineId": machine_id}, status=exc.status_code)

        production_run = _build_iot_production_run_context(machine_id, start_ts=start_ts, end_ts=end_ts)

        latest = data.get("latest") if isinstance(data.get("latest"), dict) else None
        history = list(data.get("history") or [])
        if not latest and not history:
            return JsonResponse(
                {"ok": False, "error": "No telemetry is available for AI summary yet.", "machineId": machine_id},
                status=404,
            )

        cache_key = _build_iot_summary_cache_key(machine_id, data, limit, production_run)
        if force:
            cache.delete(cache_key)

        cached = cache.get(cache_key)
        if cached and isinstance(cached, dict):
            return JsonResponse(
                {
                    "ok": True,
                    "machineId": machine_id,
                    "cached": True,
                    "generated_at_ts": cached.get("generated_at_ts"),
                    "summary": cached.get("summary"),
                    "snapshot": cached.get("snapshot"),
                    "production_run": cached.get("production_run"),
                }
            )

        summary = _generate_iot_summary_text(data, production_run)
        payload = {
            "summary": summary,
            "generated_at_ts": time.time(),
            "snapshot": _build_iot_summary_snapshot(data, production_run),
            "production_run": production_run,
        }
        cache.set(cache_key, payload, timeout=IOT_AI_SUMMARY_CACHE_TTL_SECONDS)
        return JsonResponse({"ok": True, "machineId": machine_id, "cached": False, **payload})
    except Exception as exc:
        logger.exception("IoT dashboard summary failed unexpectedly")
        machine_id = (request.GET.get("machineId") or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        return JsonResponse(
            {
                "ok": False,
                "error": f"AI summary failed unexpectedly: {exc}",
                "machineId": machine_id,
            },
            status=500,
        )


def iot_dashboard(request):
    if not _iot_local_testing_enabled():
        raise Http404("Not found")
    return render(
        request,
        "magicwand/iot_dashboard.html",
        {
            "page_kicker": "Local Testing",
            "page_title": "Modbus Ingest Dashboard",
            "page_description": "Polls the latest in-memory payload every second from the local ingest endpoint.",
            "iot_data_url": reverse("iot_ingest"),
            "iot_summary_url": reverse("iot_dashboard_summary"),
            "iot_export_url": reverse("iot_history_export"),
            "iot_chart_max_points": getattr(settings, "IOT_CHART_MAX_POINTS", 5000),
            "iot_time_zone": getattr(settings, "IOT_DASHBOARD_TIME_ZONE", "Asia/Kolkata"),
            "iot_time_zone_label": getattr(settings, "IOT_DASHBOARD_TIME_ZONE", "Asia/Kolkata"),
        },
    )


def _deskera_portal_base() -> str:
    configured_base = (
        getattr(settings, 'DESKERA_PORTAL_BASE_URL', '')
        or getattr(settings, 'DESKERA_MRP_BASE_URL', '')
    ).strip()
    return configured_base.rstrip('/') if configured_base else ''


def _build_deskera_client() -> DeskeraMRPClient:
    return DeskeraMRPClient(
        base_url=getattr(settings, 'DESKERA_MRP_BASE_URL', ''),
        api_key=getattr(settings, 'DESKERA_MRP_API_KEY', ''),
        tenant=getattr(settings, 'DESKERA_MRP_TENANT', ''),
        kanban_path=getattr(settings, 'DESKERA_MRP_KANBAN_PATH', '/mrp/api/v1/kanban-board'),
        default_board_id=getattr(settings, 'DESKERA_MRP_BOARD_ID', ''),
        timeout=getattr(settings, 'DESKERA_MRP_TIMEOUT', 15),
        workorder_path=getattr(settings, 'DESKERA_WORKORDER_PATH', '/v1/mrp/workorder/search'),
        workstation_path=getattr(settings, 'DESKERA_WORKSTATION_PATH', '/v1/mrp/work-stations/search'),
        jobcard_path=getattr(settings, 'DESKERA_JOBCARD_PATH', '/v1/mrp/jobcard/search'),
    )


def _humanize_deskera_column(key: str) -> str:
    overrides = {
        "id": "ID",
        "purchaseOrder": "PO",
        "qcStatus": "QC Status",
        "qcDetails": "QC Details",
        "description": "Description",
        "workOrderCode": "Work Order Code",
        "documentSequenceCode": "Work Order",
        "parentWorkOrderSeqCode": "Parent Work Order",
        "parentWorkOrderCode": "Parent Work Order Code",
        "compoundCode": "Compound",
        "cutLength": "Cut Length",
        "jobStatus": "Job Status",
        "weight": "Weight",
        "productName": "Product",
        "contactName": "Customer",
        "manufactureQuantity": "Quantity",
        "actualQuantity": "Actual Quantity",
        "deliveryDate": "Due Date",
        "updatedOn": "Updated",
    }
    if key in overrides:
        return overrides[key]

    cleaned = (key or "").strip()
    if not cleaned:
        return ""

    # Convert camelCase / PascalCase into words and normalise common acronyms.
    cleaned = re.sub(r"[_\\-]+", " ", cleaned)
    cleaned = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", cleaned)
    words = [w for w in cleaned.split() if w]
    acronyms = {"id": "ID", "qc": "QC", "mrp": "MRP", "bom": "BOM", "api": "API"}
    formatted: List[str] = []
    for word in words:
        lower = word.lower()
        if lower in acronyms:
            formatted.append(acronyms[lower])
        else:
            formatted.append(word[:1].upper() + word[1:])
    return " ".join(formatted)


def _deskera_format_column_value(client: DeskeraMRPClient, key: str, value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if isinstance(value, list):
        if key == "attachmentDetails":
            names: List[str] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = item.get("attachmentFileName") or item.get("fileName") or item.get("name")
                if name:
                    names.append(str(name))
            if names:
                suffix = ""
                if len(names) > 3:
                    suffix = f", +{len(names) - 3} more"
                preview = ", ".join(names[:3])
                label = "file" if len(names) == 1 else "files"
                return f"{len(names)} {label}: {preview}{suffix}"
        return f"{len(value)} items"
    if isinstance(value, dict):
        if key == "operatorDetails":
            candidate = value.get("operatorName") or value.get("name")
            if candidate:
                return str(candidate)
        if key == "workstationDetails":
            candidate = value.get("workstationName") or value.get("workStationName") or value.get("name")
            if candidate:
                return str(candidate)
        return f"{len(value)} keys"

    lowered = (key or "").lower()
    if lowered.endswith(("date", "on")) or "date" in lowered:
        formatted = client._format_date(value)
        return formatted if formatted else str(value)

    formatted = client._format_value(value)
    return formatted if formatted else "—"


def _extract_named_custom_field_text(record: Dict[str, Any], *field_names: str) -> str:
    def has_value(candidate: Any) -> bool:
        if candidate is None:
            return False
        if isinstance(candidate, str):
            return candidate.strip() not in {"", "-", "—"}
        return candidate != "" and candidate != []

    def as_text(candidate: Any) -> str:
        if not has_value(candidate):
            return ""
        if isinstance(candidate, (int, float, bool)):
            return str(candidate).strip()
        if isinstance(candidate, str):
            raw = candidate.strip()
            if not raw:
                return ""
            if raw[:1] in {'"', "[", "{"} and raw[-1:] in {'"', "]", "}"}:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = None
                else:
                    if isinstance(decoded, (int, float, bool)):
                        return str(decoded).strip()
                    if isinstance(decoded, str):
                        return decoded.strip()
            return raw
        return str(candidate).strip()

    def normalize_name(candidate: Any) -> str:
        raw = str(candidate or "").strip().lower()
        return "".join(character for character in raw if character.isalnum())

    target_names = {normalize_name(name) for name in field_names if str(name or "").strip()}
    if not target_names:
        return ""

    custom_containers = (
        "customField",
        "customFields",
        "custom_fields",
        "productCustomField",
        "productCustomFields",
        "customFieldValues",
        "custom_field_values",
        "additionalFields",
        "additional_fields",
        "userDefinedFields",
        "user_defined_fields",
        "udf",
    )
    container_set = {name.lower() for name in custom_containers}

    def matches(name: Any) -> bool:
        normalized = normalize_name(name)
        if not normalized:
            return False
        return any(normalized == target or target in normalized or normalized in target for target in target_names)

    def extract_from_container(container: Any) -> str:
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                value = as_text(
                    item.get("value")
                    or item.get("fieldValue")
                    or item.get("field_value")
                    or item.get("displayValue")
                    or item.get("display_value")
                )
                if not value:
                    continue
                candidate_names = (
                    item.get("label"),
                    item.get("name"),
                    item.get("fieldName"),
                    item.get("field_name"),
                    item.get("key"),
                    item.get("shortName"),
                    item.get("short_name"),
                )
                if any(matches(candidate_name) for candidate_name in candidate_names):
                    return value
            return ""
        if isinstance(container, dict):
            for name_key, value_candidate in container.items():
                if not matches(name_key):
                    continue
                value = as_text(value_candidate)
                if value:
                    return value
        return ""

    for container_key in custom_containers:
        extracted = extract_from_container(record.get(container_key))
        if extracted:
            return extracted

    def search(obj: Any) -> str:
        if isinstance(obj, dict):
            for key, value in obj.items():
                lowered = str(key).lower()
                if lowered in container_set:
                    extracted = extract_from_container(value)
                    if extracted:
                        return extracted
                if matches(key):
                    extracted = as_text(value)
                    if extracted:
                        return extracted
                nested = search(value)
                if nested:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = search(item)
                if nested:
                    return nested
        return ""

    return search(record)


def _extract_compound_code_from_deskera_workorder(record: Dict[str, Any]) -> str:
    def has_value(candidate: Any) -> bool:
        return candidate is not None and candidate != "" and candidate != []

    def as_text(candidate: Any) -> str:
        if not has_value(candidate):
            return ""
        if isinstance(candidate, (int, float, bool)):
            return str(candidate).strip()
        if isinstance(candidate, str):
            raw = candidate.strip()
            if not raw:
                return ""
            if raw[:1] in {'"', "[", "{"} and raw[-1:] in {'"', "]", "}"}:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = None
                else:
                    if isinstance(decoded, (int, float, bool)):
                        return str(decoded).strip()
                    if isinstance(decoded, str):
                        return decoded.strip()
            return raw
        return ""

    configured_fields = getattr(settings, "DESKERA_WORKORDER_COMPOUND_CODE_FIELDS", "")
    for path in str(configured_fields or "").split(","):
        cleaned = path.strip()
        if not cleaned:
            continue
        candidate: Any = record
        for segment in cleaned.split("."):
            if not isinstance(candidate, dict):
                candidate = None
                break
            candidate = candidate.get(segment)
        value = as_text(candidate)
        if value:
            return value

    direct_keys = (
        "compoundCode",
        "compound_code",
        "compoundcode",
        "productCompoundCode",
        "product_compound_code",
        "compound",
    )
    for key in direct_keys:
        value = as_text(record.get(key))
        if value:
            return value

    custom_containers = (
        "customField",
        "customFields",
        "custom_fields",
        "productCustomField",
        "productCustomFields",
        "customFieldValues",
        "custom_field_values",
        "additionalFields",
        "additional_fields",
        "userDefinedFields",
        "user_defined_fields",
        "udf",
    )
    container_set = {name.lower() for name in custom_containers}
    needles = {"compound", "compoundcode", "compound_code", "compound code"}
    def extract_from_container(container: Any) -> str:
        if isinstance(container, list):
            fallback_value = ""
            for item in container:
                if not isinstance(item, dict):
                    continue
                label = str(
                    item.get("label")
                    or item.get("name")
                    or item.get("fieldName")
                    or item.get("field_name")
                    or item.get("key")
                    or ""
                ).strip()
                short = str(item.get("shortName") or item.get("short_name") or "").strip()
                label_lower = label.lower()
                short_lower = short.lower()
                value = as_text(
                    item.get("value")
                    or item.get("fieldValue")
                    or item.get("field_value")
                    or item.get("displayValue")
                    or item.get("display_value")
                )
                if not value:
                    continue

                for name in (label_lower, short_lower):
                    if not name:
                        continue
                    if name in needles:
                        return value
                    if "compound" in name and "code" in name:
                        return value

                if not fallback_value and ("compound" in label_lower or "compound" in short_lower):
                    fallback_value = value
            if fallback_value:
                return fallback_value
        elif isinstance(container, dict):
            for name_key, value_candidate in container.items():
                name = str(name_key).strip().lower()
                if name not in needles and not ("compound" in name and "code" in name):
                    continue
                value = as_text(value_candidate)
                if value:
                    return value
        return ""

    for container_key in custom_containers:
        extracted = extract_from_container(record.get(container_key))
        if extracted:
            return extracted

    def search(obj: Any) -> str:
        if isinstance(obj, dict):
            for key, value in obj.items():
                lowered = str(key).lower()
                if lowered in container_set:
                    extracted = extract_from_container(value)
                    if extracted:
                        return extracted
                if "compound" in lowered and "code" in lowered:
                    extracted = as_text(value)
                    if extracted:
                        return extracted
                    if isinstance(value, dict):
                        for nested_key in ("compoundCode", "compound_code", "code"):
                            nested_value = as_text(value.get(nested_key))
                            if nested_value:
                                return nested_value
                    if isinstance(value, list):
                        nested = search(value)
                        if nested:
                            return nested
                nested = search(value)
                if nested:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = search(item)
                if nested:
                    return nested
        return ""

    return search(record)


def _extract_weight_from_deskera_workorder(record: Dict[str, Any]) -> str:
    def has_value(candidate: Any) -> bool:
        if candidate is None:
            return False
        if isinstance(candidate, str):
            return candidate.strip() not in {"", "-", "—"}
        return candidate != "" and candidate != []

    def as_text(candidate: Any) -> str:
        if not has_value(candidate):
            return ""
        if isinstance(candidate, (int, float, bool)):
            return str(candidate).strip()
        if isinstance(candidate, str):
            raw = candidate.strip()
            if not raw:
                return ""
            if raw[:1] in {'"', "[", "{"} and raw[-1:] in {'"', "]", "}"}:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = None
                else:
                    if isinstance(decoded, (int, float, bool)):
                        return str(decoded).strip()
                    if isinstance(decoded, str):
                        return decoded.strip()
            return raw
        return str(candidate).strip()

    configured_fields = getattr(settings, "DESKERA_WORKORDER_WEIGHT_FIELDS", "")
    for path in str(configured_fields or "").split(","):
        cleaned = path.strip()
        if not cleaned:
            continue
        candidate: Any = record
        for segment in cleaned.split("."):
            if not isinstance(candidate, dict):
                candidate = None
                break
            candidate = candidate.get(segment)
        value = as_text(candidate)
        if value:
            return value

    direct_keys = (
        "weight",
        "totalWeight",
        "netWeight",
        "grossWeight",
        "workOrderWeight",
        "productWeight",
        "itemWeight",
    )
    for key in direct_keys:
        value = as_text(record.get(key))
        if value:
            return value

    custom_containers = (
        "customField",
        "customFields",
        "custom_fields",
        "productCustomField",
        "productCustomFields",
        "customFieldValues",
        "custom_field_values",
        "additionalFields",
        "additional_fields",
        "userDefinedFields",
        "user_defined_fields",
        "udf",
    )
    needles_exact = {"weight", "net weight", "gross weight"}

    def extract_from_container(container: Any) -> str:
        if isinstance(container, list):
            exact_matches: Dict[str, str] = {}
            best_fallback = ""
            for item in container:
                if not isinstance(item, dict):
                    continue
                label = str(
                    item.get("label")
                    or item.get("name")
                    or item.get("fieldName")
                    or item.get("field_name")
                    or item.get("key")
                    or ""
                ).strip()
                short = str(item.get("shortName") or item.get("short_name") or "").strip()
                label_lower = label.lower()
                short_lower = short.lower()
                value = as_text(
                    item.get("value")
                    or item.get("fieldValue")
                    or item.get("field_value")
                    or item.get("displayValue")
                    or item.get("display_value")
                )
                if not value:
                    continue

                for name in (label_lower, short_lower):
                    if not name:
                        continue
                    if name in needles_exact and name not in exact_matches:
                        exact_matches[name] = value
                        continue
                    if "weight" in name and not best_fallback:
                        best_fallback = value

                if not best_fallback and "weight" in label_lower:
                    best_fallback = value

            for preferred in ("weight", "net weight", "gross weight"):
                if preferred in exact_matches:
                    return exact_matches[preferred]
            return best_fallback
        if isinstance(container, dict):
            exact_matches: Dict[str, str] = {}
            best_fallback = ""
            for name_key, value_candidate in container.items():
                name = str(name_key).strip().lower()
                if name not in needles_exact and "weight" not in name:
                    continue
                value = as_text(value_candidate)
                if value:
                    if name in needles_exact and name not in exact_matches:
                        exact_matches[name] = value
                    elif not best_fallback:
                        best_fallback = value

            for preferred in ("weight", "net weight", "gross weight"):
                if preferred in exact_matches:
                    return exact_matches[preferred]
            if best_fallback:
                return best_fallback
        return ""

    for container_key in custom_containers:
        extracted = extract_from_container(record.get(container_key))
        if extracted:
            return extracted

    return ""


def _extract_purchase_order_from_deskera_workorder(record: Dict[str, Any]) -> str:
    def has_value(candidate: Any) -> bool:
        if candidate is None:
            return False
        if isinstance(candidate, str):
            return candidate.strip() not in {"", "-", "—"}
        return candidate != "" and candidate != []

    def as_text(candidate: Any) -> str:
        if not has_value(candidate):
            return ""
        if isinstance(candidate, (int, float, bool)):
            return str(candidate).strip()
        if isinstance(candidate, str):
            raw = candidate.strip()
            if not raw:
                return ""
            if raw[:1] in {'"', "[", "{"} and raw[-1:] in {'"', "]", "}"}:
                try:
                    decoded = json.loads(raw)
                except Exception:
                    decoded = None
                else:
                    if isinstance(decoded, (int, float, bool)):
                        return str(decoded).strip()
                    if isinstance(decoded, str):
                        return decoded.strip()
            return raw
        return str(candidate).strip()

    configured_fields = getattr(settings, "DESKERA_WORKORDER_PO_FIELDS", "")
    for path in str(configured_fields or "").split(","):
        cleaned = path.strip()
        if not cleaned:
            continue
        candidate: Any = record
        for segment in cleaned.split("."):
            if not isinstance(candidate, dict):
                candidate = None
                break
            candidate = candidate.get(segment)
        value = as_text(candidate)
        if value:
            return value

    direct_keys = (
        "purchaseOrder",
        "purchase_order",
        "purchaseOrderNumber",
        "purchaseOrderNo",
        "purchaseOrderCode",
        "customerPurchaseOrder",
        "customerPo",
        "customerPO",
        "poNumber",
        "poNo",
        "po",
        "PO",
        "salesOrderCustomerOrderNumber",
    )
    for key in direct_keys:
        value = as_text(record.get(key))
        if value:
            return value

    return _extract_named_custom_field_text(
        record,
        "po",
        "purchase order",
        "purchase order number",
        "purchaseOrder",
        "purchaseOrderNumber",
        "purchaseOrderNo",
        "po number",
        "po no",
        "customer po",
    )


def _extract_cut_length_from_deskera_workorder(record: Dict[str, Any]) -> str:
    return _extract_named_custom_field_text(
        record,
        "cut length",
        "cut lenght",
        "cutLength",
        "cutLenght",
    )


def _extract_job_status_from_deskera_workorder(record: Dict[str, Any]) -> str:
    def has_value(candidate: Any) -> bool:
        if candidate is None:
            return False
        if isinstance(candidate, str):
            return candidate.strip() not in {"", "-", "—"}
        return candidate != "" and candidate != []

    def as_text(candidate: Any) -> str:
        if not has_value(candidate):
            return ""
        if isinstance(candidate, (int, float, bool)):
            return str(candidate).strip()
        if isinstance(candidate, str):
            return candidate.strip()
        return str(candidate).strip()

    def as_int(candidate: Any) -> Optional[int]:
        if candidate in (None, "", []):
            return None
        if isinstance(candidate, bool):
            return int(candidate)
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, float):
            return int(candidate)
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if not cleaned:
                return None
            try:
                return int(Decimal(cleaned))
            except (InvalidOperation, ValueError):
                return None
        return None

    def normalize_name(candidate: Any) -> str:
        raw = str(candidate or "").strip().lower()
        return "".join(character for character in raw if character.isalnum())

    target_names = {
        normalize_name(name)
        for name in (
            "job status",
            "jobStatus",
            "job_status",
            "job card status",
            "jobCardStatus",
            "job progress",
            "jobProgress",
        )
    }
    custom_containers = (
        "customField",
        "customFields",
        "custom_fields",
        "productCustomField",
        "productCustomFields",
        "customFieldValues",
        "custom_field_values",
        "additionalFields",
        "additional_fields",
        "userDefinedFields",
        "user_defined_fields",
        "udf",
    )
    container_set = {name.lower() for name in custom_containers}

    def matches(candidate: Any) -> bool:
        return normalize_name(candidate) in target_names

    def extract_from_container(container: Any) -> str:
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                candidate_names = (
                    item.get("label"),
                    item.get("name"),
                    item.get("fieldName"),
                    item.get("field_name"),
                    item.get("key"),
                    item.get("shortName"),
                    item.get("short_name"),
                )
                if not any(matches(candidate_name) for candidate_name in candidate_names):
                    continue
                value = as_text(
                    item.get("value")
                    or item.get("fieldValue")
                    or item.get("field_value")
                    or item.get("displayValue")
                    or item.get("display_value")
                )
                if value:
                    return value
            return ""
        if isinstance(container, dict):
            for name_key, value_candidate in container.items():
                if not matches(name_key):
                    continue
                value = as_text(value_candidate)
                if value:
                    return value
        return ""

    for container_key in custom_containers:
        extracted = extract_from_container(record.get(container_key))
        if extracted:
            return extracted

    def search(obj: Any) -> str:
        if isinstance(obj, dict):
            for key, value in obj.items():
                lowered = str(key).lower()
                if lowered in container_set:
                    extracted = extract_from_container(value)
                    if extracted:
                        return extracted
                if matches(key):
                    extracted = as_text(value)
                    if extracted:
                        return extracted
                nested = search(value)
                if nested:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = search(item)
                if nested:
                    return nested
        return ""

    explicit = search(record)
    if explicit:
        return explicit

    direct_keys = (
        "jobStatus",
        "job_status",
        "jobCardStatus",
        "job_card_status",
        "jobProgressStatus",
        "jobProgress",
    )
    for key in direct_keys:
        value = record.get(key)
        if value not in (None, "", []):
            text = str(value).strip()
            if text:
                return text

    total = as_int(record.get("totalJobCards"))
    completed = as_int(record.get("totalCompletedJobCards")) or 0
    in_progress = as_int(record.get("totalInProgressJobCards")) or 0
    all_completed = record.get("allJobCardsCompleted")

    if total is None or total <= 0:
        return "—"

    if bool(all_completed) or completed >= total:
        return "Completed"
    if in_progress > 0:
        return f"In Progress ({in_progress}/{total})"
    if completed > 0:
        return f"In Progress ({completed}/{total})"
    return "Pending"


def _deskera_workorder_columns(workorders: List[Dict[str, Any]], *, mode: str = "compact") -> List[str]:
    keys: set[str] = set()
    for order in workorders:
        raw = order.get("raw")
        if isinstance(raw, dict):
            keys.update(str(k) for k in raw.keys())
        for key in order.keys():
            if key in {"raw", "all_values", "portal_url", "_search"}:
                continue
            keys.add(str(key))
    # Always include the derived compound code column (renders as "—" when absent).
    keys.add("compoundCode")
    keys.add("cutLength")
    keys.add("jobStatus")
    keys.add("weight")
    keys.add("purchaseOrder")
    keys.add("documentSequenceCode")
    keys.add("workOrderCode")
    keys.add("parentWorkOrderCode")
    keys.add("parentWorkOrderSeqCode")

    preferred_compact = [
        "documentSequenceCode",
        "workOrderCode",
        "parentWorkOrderSeqCode",
        "parentWorkOrderCode",
        "description",
        "compoundCode",
        "product",
        "cutLength",
        "customer",
        "purchaseOrder",
        "weight",
        "quantity",
        "status",
        "jobStatus",
        "due_date",
        "created_date",
    ]
    preferred_all = [
        *preferred_compact,
        "productName",
        "contactName",
        "description",
        "manufactureQuantity",
        "actualQuantity",
        "deliveryDate",
        "plannedStartDate",
        "plannedEndDate",
        "actualStartDate",
        "actualEndDate",
        "qcStatus",
        "totalJobCards",
        "totalInProgressJobCards",
        "totalCompletedJobCards",
        "updatedOn",
        "id",
    ]
    mode_normalized = (mode or "").strip().lower()
    preferred = preferred_all if mode_normalized == "all" else preferred_compact
    ordered = [key for key in preferred if key in keys]
    if mode_normalized != "all":
        return ordered

    ordered_set = set(ordered)
    ordered.extend(sorted(keys - ordered_set))
    return ordered


def _deskera_jobcard_columns(jobcards: List[Dict[str, Any]]) -> List[str]:
    keys: set[str] = set()
    for card in jobcards:
        raw = card.get("raw")
        if isinstance(raw, dict):
            keys.update(str(k) for k in raw.keys())

    preferred = [
        "documentSequenceCode",
        "jobCardCode",
        "workOrderSequenceCode",
        "workOrderCode",
        "salesOrderSequenceCode",
        "salesOrderCustomerOrderNumber",
        "workOrderItemName",
        "operationName",
        "workstationId",
        "status",
        "requestedQuantity",
        "plannedStartDate",
        "plannedEndDate",
        "actualStartDate",
        "actualEndDate",
        "operatorDetails",
        "workstationDetails",
        "attachmentDetails",
        "id",
    ]
    ordered = [key for key in preferred if key in keys]
    ordered_set = set(ordered)
    ordered.extend(sorted(keys - ordered_set))
    return ordered


def _fetch_normalized_workorders(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    page: int = 0,
    limit: int = 25,
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
    fetch_attachment_details: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    result_meta = {"page": page, "limit": limit, "total": None, "has_more": False}
    workorders: List[Dict[str, Any]] = []
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

    try:
        fetch_kwargs: Dict[str, Any] = {
            'search': search,
            'status': status,
            'page': page,
            'limit': limit,
            'fetch_attachment_details': fetch_attachment_details,
        }
        if sort_field:
            fetch_kwargs['sort'] = sort_field
        if sort_dir:
            fetch_kwargs['sort_dir'] = sort_dir

        result = client.fetch_workorders(**fetch_kwargs)
        raw_response = result
        meta = result.get('meta') or {}
        result_meta.update(meta)
        for record in result.get('records', []):
            workorders.append(
                _normalize_workorder_record(
                    record,
                    client=client,
                    portal_base=portal_base,
                )
            )
    except DeskeraMRPConfigError as exc:
        error = str(exc)
    except DeskeraMRPError as exc:
        error = str(exc)

    return workorders, result_meta, error, raw_response


def _deskera_user_facing_warning(error: Optional[str], *, has_cached_snapshot: bool = True) -> str:
    message = str(error or "").strip()
    lowered = message.lower()

    if has_cached_snapshot:
        if "read timed out" in lowered or "connect timeout" in lowered or "connectionpool" in lowered:
            detail = (
                "Deskera is taking longer than usual to respond right now. "
                "Showing the last saved data while the connection catches up."
            )
        elif "responded with 403" in lowered or "forbidden" in lowered:
            detail = (
                "Deskera is temporarily not accepting this request. "
                "Showing the last saved data for now."
            )
        elif "rejected the credentials" in lowered or "invalid bearer token" in lowered:
            detail = (
                "Deskera access needs attention. "
                "Showing the last saved data until the connection is fixed."
            )
        else:
            detail = (
                "Deskera is temporarily unavailable. "
                "Showing the last saved data for now."
            )
        return f"{detail} You can keep working from the cached snapshot and refresh again shortly."

    if "read timed out" in lowered or "connect timeout" in lowered or "connectionpool" in lowered:
        detail = (
            "Deskera is taking longer than usual to respond right now. "
            "No saved Deskera snapshot is available yet."
        )
    elif "responded with 403" in lowered or "forbidden" in lowered:
        detail = (
            "Deskera is not accepting API requests from this server right now. "
            "No saved Deskera snapshot is available yet."
        )
    elif "rejected the credentials" in lowered or "invalid bearer token" in lowered:
        detail = (
            "Deskera access needs attention. "
            "No saved Deskera snapshot is available yet."
        )
    else:
        detail = (
            "Deskera is temporarily unavailable. "
            "No saved Deskera snapshot is available yet."
        )
    return f"{detail} Refresh will resume once Deskera accepts the request or a new snapshot is imported."


def _fetch_normalized_workstations(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    page: int = 0,
    limit: int = 50,
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    result_meta = {"page": page, "limit": limit, "total": None, "has_more": False}
    stations: List[Dict[str, Any]] = []
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

    try:
        fetch_kwargs: Dict[str, Any] = {
            'search': search,
            'status': status,
            'page': page,
            'limit': limit,
        }
        if sort_field:
            fetch_kwargs['sort'] = sort_field
        if sort_dir:
            fetch_kwargs['sort_dir'] = sort_dir

        result = client.fetch_workstations(**fetch_kwargs)
        raw_response = result
        meta = result.get('meta') or {}
        result_meta.update(meta)
        for record in result.get('records', []):
            stations.append(
                _normalize_workstation_record(
                    record,
                    client=client,
                    portal_base=portal_base,
                )
            )
    except DeskeraMRPConfigError as exc:
        error = str(exc)
    except DeskeraMRPError as exc:
        error = str(exc)

    return stations, result_meta, error, raw_response


def _fetch_normalized_jobcards(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    page: int = 0,
    limit: int = 50,
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    result_meta = {"page": page, "limit": limit, "total": None, "has_more": False}
    jobcards: List[Dict[str, Any]] = []
    error: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

    try:
        fetch_kwargs: Dict[str, Any] = {
            'search': search,
            'status': status,
            'page': page,
            'limit': limit,
        }
        if sort_field:
            fetch_kwargs['sort'] = sort_field
        if sort_dir:
            fetch_kwargs['sort_dir'] = sort_dir

        result = client.fetch_jobcards(**fetch_kwargs)
        raw_response = result
        meta = result.get('meta') or {}
        result_meta.update(meta)
        for record in result.get('records', []):
            jobcards.append(
                _normalize_jobcard_record(
                    record,
                    client=client,
                    portal_base=portal_base,
                )
            )
    except DeskeraMRPConfigError as exc:
        error = str(exc)
    except DeskeraMRPError as exc:
        error = str(exc)

    return jobcards, result_meta, error, raw_response


def _fetch_all_workorders(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: Optional[int] = None,
    max_pages: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    limit_value = limit if limit is not None else getattr(settings, 'DESKERA_WORKORDER_PAGE_SIZE', 100)
    configured_max_pages = max_pages if max_pages is not None else getattr(settings, 'DESKERA_WORKORDER_MAX_PAGES', 10)
    hard_max_pages = getattr(settings, 'DESKERA_PAGINATION_HARD_MAX_PAGES', 250)
    if hard_max_pages is not None and hard_max_pages <= 0:
        hard_max_pages = None

    max_pages_value: Optional[int] = configured_max_pages
    if max_pages_value is not None and max_pages_value <= 0:
        max_pages_value = None

    aggregated_workorders: List[Dict[str, Any]] = []
    aggregated_meta: Dict[str, Any] = {"page": 0, "limit": limit_value, "total": None, "has_more": False}
    aggregated_raw: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    fetched_pages = 0

    page = 0
    while True:
        if max_pages_value is not None and page >= max_pages_value:
            aggregated_meta['has_more'] = True
            break
        if hard_max_pages is not None and page >= hard_max_pages:
            aggregated_meta['has_more'] = True
            break

        page_workorders, page_meta, page_error, raw_response = _fetch_normalized_workorders(
            client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=page,
            limit=limit_value,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
        if page_error:
            error = page_error
            break
        fetched_pages += 1
        aggregated_workorders.extend(page_workorders)
        aggregated_meta = page_meta or aggregated_meta
        if raw_response:
            aggregated_raw = raw_response
        if not page_meta.get('has_more'):
            break
        if not page_workorders:
            aggregated_meta['has_more'] = False
            break
        page += 1

    meta_summary = {
        'total': aggregated_meta.get('total'),
        'has_more': aggregated_meta.get('has_more'),
        'limit': limit_value,
        'fetched_pages': fetched_pages,
    }

    return aggregated_workorders, meta_summary, error, aggregated_raw


def _fetch_all_workstations(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: Optional[int] = None,
    max_pages: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    limit_value = limit if limit is not None else getattr(settings, 'DESKERA_WORKSTATION_PAGE_SIZE', 100)
    configured_max_pages = max_pages if max_pages is not None else getattr(settings, 'DESKERA_WORKSTATION_MAX_PAGES', 10)
    hard_max_pages = getattr(settings, 'DESKERA_PAGINATION_HARD_MAX_PAGES', 250)
    if hard_max_pages is not None and hard_max_pages <= 0:
        hard_max_pages = None

    max_pages_value: Optional[int] = configured_max_pages
    if max_pages_value is not None and max_pages_value <= 0:
        max_pages_value = None

    aggregated: List[Dict[str, Any]] = []
    aggregated_meta: Dict[str, Any] = {"page": 0, "limit": limit_value, "total": None, "has_more": False}
    aggregated_raw: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    fetched_pages = 0

    page = 0
    while True:
        if max_pages_value is not None and page >= max_pages_value:
            aggregated_meta['has_more'] = True
            break
        if hard_max_pages is not None and page >= hard_max_pages:
            aggregated_meta['has_more'] = True
            break

        page_records, page_meta, page_error, raw_payload = _fetch_normalized_workstations(
            client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=page,
            limit=limit_value,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
        if page_error:
            error = page_error
            break
        fetched_pages += 1
        aggregated.extend(page_records)
        aggregated_meta = page_meta or aggregated_meta
        if raw_payload:
            aggregated_raw = raw_payload
        if not page_meta.get('has_more'):
            break
        if not page_records:
            aggregated_meta['has_more'] = False
            break
        page += 1

    meta_summary = {
        'total': aggregated_meta.get('total'),
        'has_more': aggregated_meta.get('has_more'),
        'limit': limit_value,
        'fetched_pages': fetched_pages,
    }

    return aggregated, meta_summary, error, aggregated_raw


def _fetch_all_jobcards(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    search: str = '',
    status: str = '',
    sort_field: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: Optional[int] = None,
    max_pages: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str], Optional[Dict[str, Any]]]:
    limit_value = limit if limit is not None else getattr(settings, 'DESKERA_JOBCARD_PAGE_SIZE', 100)
    configured_max_pages = max_pages if max_pages is not None else getattr(settings, 'DESKERA_JOBCARD_MAX_PAGES', 10)
    hard_max_pages = getattr(settings, 'DESKERA_PAGINATION_HARD_MAX_PAGES', 250)
    if hard_max_pages is not None and hard_max_pages <= 0:
        hard_max_pages = None

    max_pages_value: Optional[int] = configured_max_pages
    if max_pages_value is not None and max_pages_value <= 0:
        max_pages_value = None

    aggregated: List[Dict[str, Any]] = []
    aggregated_meta: Dict[str, Any] = {"page": 0, "limit": limit_value, "total": None, "has_more": False}
    aggregated_raw: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    fetched_pages = 0

    page = 0
    while True:
        if max_pages_value is not None and page >= max_pages_value:
            aggregated_meta['has_more'] = True
            break
        if hard_max_pages is not None and page >= hard_max_pages:
            aggregated_meta['has_more'] = True
            break

        page_records, page_meta, page_error, raw_payload = _fetch_normalized_jobcards(
            client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=page,
            limit=limit_value,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
        if page_error:
            error = page_error
            break
        fetched_pages += 1
        aggregated.extend(page_records)
        aggregated_meta = page_meta or aggregated_meta
        if raw_payload:
            aggregated_raw = raw_payload
        if not page_meta.get('has_more'):
            break
        if not page_records:
            aggregated_meta['has_more'] = False
            break
        page += 1

    meta_summary = {
        'total': aggregated_meta.get('total'),
        'has_more': aggregated_meta.get('has_more'),
        'limit': limit_value,
        'fetched_pages': fetched_pages,
    }

    return aggregated, meta_summary, error, aggregated_raw


_JOBCARD_SNAPSHOT_VERSION = 1
_JOBCARD_SNAPSHOT_PREFIX = "magicwand:deskera:jobcards:snapshot"
_JOBCARD_SNAPSHOT_LOCK_PREFIX = "magicwand:deskera:jobcards:snapshot:lock"
_JOBCARD_STAKEHOLDER_SUMMARY_VERSION = 1
_JOBCARD_STAKEHOLDER_SUMMARY_PREFIX = "magicwand:deskera:jobcards:stakeholder_summary"
_JOBCARD_STAKEHOLDER_SUMMARY_LOCK_PREFIX = "magicwand:deskera:jobcards:stakeholder_summary:lock"
_JOB_CARD_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _deskera_cache_namespace(client: DeskeraMRPClient) -> str:
    base = (getattr(client, "base_url", "") or "").strip().lower()
    tenant = (getattr(client, "tenant", "") or "").strip().lower()
    digest = hashlib.sha256(f"{base}|{tenant}".encode("utf-8")).hexdigest()
    return digest[:16]


def _deskera_snapshot_has_records(snapshot: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(snapshot, dict):
        return False
    records = snapshot.get("records")
    return isinstance(records, list) and len(records) > 0


def _deskera_snapshot_is_empty_error(snapshot: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if _deskera_snapshot_has_records(snapshot):
        return False
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("error"))


def _deskera_snapshot_error_payload(message: str) -> Dict[str, Any]:
    return {
        "message": str(message or "").strip(),
        "at_ts": time.time(),
    }


def _deskera_snapshot_error_message(raw_error: Any) -> Optional[str]:
    if isinstance(raw_error, dict):
        message = str(raw_error.get("message") or "").strip()
        return message or None
    rendered = str(raw_error or "").strip()
    return rendered or None


def _deskera_snapshot_error_is_recent(raw_error: Any, *, cooldown_seconds: int = 300) -> bool:
    if not isinstance(raw_error, dict):
        return False
    try:
        at_ts = float(raw_error.get("at_ts"))
    except (TypeError, ValueError):
        return False
    return (time.time() - at_ts) < max(1, int(cooldown_seconds))


def _deskera_primary_snapshot_cache_key(cache_key: str) -> bool:
    if not isinstance(cache_key, str):
        return False
    if ":lock" in cache_key or cache_key.endswith(":last_error") or cache_key.endswith(":last_good"):
        return False
    return cache_key.startswith(
        (
            "magicwand:deskera:jobcards:snapshot",
            "magicwand:deskera:workorders:snapshot",
        )
    )


def _deskera_last_good_snapshot_key(cache_key: str) -> str:
    return f"{cache_key}:last_good"


def _deskera_last_good_snapshot_retention_seconds() -> int:
    configured = getattr(settings, "DESKERA_LAST_GOOD_SNAPSHOT_RETENTION_SECONDS", 2592000)
    try:
        retention_seconds = int(configured)
    except (TypeError, ValueError):
        retention_seconds = 2592000
    return max(86400, retention_seconds)


def _deskera_persistent_cache_enabled() -> bool:
    configured = getattr(settings, "DESKERA_PERSISTENT_SNAPSHOT_CACHE_ENABLED", True)
    if isinstance(configured, str):
        return configured.strip().lower() not in {"0", "false", "no", "off"}
    return bool(configured)


def _deskera_persistent_cache_model():
    from mrp.models import DeskeraCacheEntry

    return DeskeraCacheEntry


def _deskera_cache_expires_at(timeout: Optional[int]) -> Optional[datetime]:
    if timeout is None:
        return None
    try:
        timeout_seconds = max(1, int(timeout))
    except (TypeError, ValueError):
        timeout_seconds = 300
    return timezone.now() + timedelta(seconds=timeout_seconds)


def _deskera_persistent_cache_timeout_seconds(cache_key: str, timeout: Optional[int]) -> Optional[int]:
    if timeout is None:
        return None
    try:
        timeout_seconds = max(1, int(timeout))
    except (TypeError, ValueError):
        timeout_seconds = 300

    if cache_key.endswith(":last_good"):
        return _deskera_last_good_snapshot_retention_seconds()

    if _deskera_primary_snapshot_cache_key(cache_key):
        configured_retention = getattr(settings, "DESKERA_PERSISTENT_SNAPSHOT_RETENTION_SECONDS", 86400)
        try:
            retention_seconds = max(3600, int(configured_retention))
        except (TypeError, ValueError):
            retention_seconds = 86400
        return max(timeout_seconds, retention_seconds)

    return timeout_seconds


def _deskera_persistent_cache_get(cache_key: str) -> Any:
    try:
        entry_model = _deskera_persistent_cache_model()
        entry = entry_model.objects.filter(cache_key=cache_key).first()
        if not entry:
            return None

        now = timezone.now()
        if entry.expires_at and entry.expires_at <= now:
            entry.delete()
            return None

        timeout: Optional[int] = None
        if entry.expires_at:
            timeout = max(1, int((entry.expires_at - now).total_seconds()))
        cache.set(cache_key, entry.payload, timeout=timeout)
        return entry.payload
    except (DatabaseError, ImportError, OperationalError, ProgrammingError) as exc:
        logger.debug("Deskera persistent cache read skipped for %s: %s", cache_key, exc)
        return None


def _deskera_cache_get(cache_key: str) -> Any:
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    if not _deskera_persistent_cache_enabled():
        return None

    payload = _deskera_persistent_cache_get(cache_key)
    if payload is not None:
        return payload

    if _deskera_primary_snapshot_cache_key(cache_key):
        last_good_key = _deskera_last_good_snapshot_key(cache_key)
        last_good_payload = _deskera_persistent_cache_get(last_good_key)
        if _deskera_snapshot_has_records(last_good_payload):
            cache.set(
                cache_key,
                last_good_payload,
                timeout=_deskera_persistent_cache_timeout_seconds(cache_key, _workorder_snapshot_ttl_seconds()),
            )
            return last_good_payload

    return None


def _deskera_cache_store_last_good(cache_key: str, payload: Any) -> None:
    if not _deskera_primary_snapshot_cache_key(cache_key) or not _deskera_snapshot_has_records(payload):
        return

    last_good_key = _deskera_last_good_snapshot_key(cache_key)
    retention_seconds = _deskera_last_good_snapshot_retention_seconds()
    cache.set(last_good_key, payload, timeout=retention_seconds)

    if not _deskera_persistent_cache_enabled():
        return

    try:
        entry_model = _deskera_persistent_cache_model()
        entry_model.objects.update_or_create(
            cache_key=last_good_key,
            defaults={
                "payload": payload,
                "expires_at": _deskera_cache_expires_at(retention_seconds),
            },
        )
    except (DatabaseError, ImportError, OperationalError, ProgrammingError, TypeError, ValueError) as exc:
        logger.debug("Deskera last-good snapshot write skipped for %s: %s", last_good_key, exc)


def _deskera_cache_set(cache_key: str, payload: Any, *, timeout: Optional[int] = None) -> None:
    cache.set(cache_key, payload, timeout=timeout)
    _deskera_cache_store_last_good(cache_key, payload)

    if not _deskera_persistent_cache_enabled():
        return

    try:
        entry_model = _deskera_persistent_cache_model()
        entry_model.objects.update_or_create(
            cache_key=cache_key,
            defaults={
                "payload": payload,
                "expires_at": _deskera_cache_expires_at(
                    _deskera_persistent_cache_timeout_seconds(cache_key, timeout)
                ),
            },
        )
    except (DatabaseError, ImportError, OperationalError, ProgrammingError, TypeError, ValueError) as exc:
        logger.debug("Deskera persistent cache write skipped for %s: %s", cache_key, exc)


def _deskera_cache_delete(cache_key: str) -> None:
    cache.delete(cache_key)

    if not _deskera_persistent_cache_enabled():
        return

    try:
        entry_model = _deskera_persistent_cache_model()
        entry_model.objects.filter(cache_key=cache_key).delete()
    except (DatabaseError, ImportError, OperationalError, ProgrammingError) as exc:
        logger.debug("Deskera persistent cache delete skipped for %s: %s", cache_key, exc)


def _deskera_refresh_lock_present(lock_key: str, *, max_age_seconds: Optional[int] = None) -> bool:
    current = _deskera_cache_get(lock_key)
    if current is None:
        return False

    if max_age_seconds is not None and isinstance(current, dict):
        try:
            at_ts = float(current.get("at_ts"))
            max_age = max(1, int(max_age_seconds))
        except (TypeError, ValueError):
            at_ts = 0.0
            max_age = 0
        if at_ts > 0 and max_age > 0 and (time.time() - at_ts) > max_age:
            _deskera_cache_delete(lock_key)
            return False

    return True


def _deskera_refresh_lock_acquire(lock_key: str, timeout_seconds: int) -> Optional[str]:
    try:
        timeout_value = max(1, int(timeout_seconds))
    except (TypeError, ValueError):
        timeout_value = 300

    if _deskera_refresh_lock_present(lock_key, max_age_seconds=timeout_value):
        return None

    token = secrets.token_hex(16)
    payload = {"token": token, "at_ts": time.time()}

    if _deskera_persistent_cache_enabled():
        try:
            entry_model = _deskera_persistent_cache_model()
            entry_model.objects.create(
                cache_key=lock_key,
                payload=payload,
                expires_at=_deskera_cache_expires_at(timeout_value),
            )
            cache.set(lock_key, payload, timeout=timeout_value)
            return token
        except IntegrityError:
            return None
        except (DatabaseError, ImportError, OperationalError, ProgrammingError, TypeError, ValueError) as exc:
            logger.debug("Deskera persistent refresh lock skipped for %s: %s", lock_key, exc)

    if cache.add(lock_key, payload, timeout=timeout_value):
        return token
    return None


def _deskera_refresh_lock_release(lock_key: str, lock_token: Optional[str]) -> None:
    current = _deskera_cache_get(lock_key)
    if (
        lock_token
        and isinstance(current, dict)
        and current.get("token")
        and current.get("token") != lock_token
    ):
        return
    _deskera_cache_delete(lock_key)


def _jobcard_snapshot_cache_key(client: DeskeraMRPClient) -> str:
    return f"{_JOBCARD_SNAPSHOT_PREFIX}:v{_JOBCARD_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}"


def _jobcard_snapshot_lock_key(client: DeskeraMRPClient) -> str:
    return f"{_JOBCARD_SNAPSHOT_LOCK_PREFIX}:v{_JOBCARD_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}"


def _jobcard_snapshot_error_key(client: DeskeraMRPClient) -> str:
    return f"{_JOBCARD_SNAPSHOT_PREFIX}:v{_JOBCARD_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}:last_error"


def _jobcard_stakeholder_summary_cache_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    ts_value = 0
    if isinstance(snapshot_fetched_at_ts, (int, float)):
        try:
            ts_value = int(float(snapshot_fetched_at_ts) * 1000)
        except (TypeError, ValueError):
            ts_value = 0
    return (
        f"{_JOBCARD_STAKEHOLDER_SUMMARY_PREFIX}:v{_JOBCARD_STAKEHOLDER_SUMMARY_VERSION}:"
        f"{_deskera_cache_namespace(client)}:{ts_value}"
    )


def _jobcard_stakeholder_summary_lock_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    ts_value = 0
    if isinstance(snapshot_fetched_at_ts, (int, float)):
        try:
            ts_value = int(float(snapshot_fetched_at_ts) * 1000)
        except (TypeError, ValueError):
            ts_value = 0
    return (
        f"{_JOBCARD_STAKEHOLDER_SUMMARY_LOCK_PREFIX}:v{_JOBCARD_STAKEHOLDER_SUMMARY_VERSION}:"
        f"{_deskera_cache_namespace(client)}:{ts_value}"
    )


def _jobcard_stakeholder_summary_error_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    return f"{_jobcard_stakeholder_summary_cache_key(client, snapshot_fetched_at_ts=snapshot_fetched_at_ts)}:error"


def _jobcard_stakeholder_summary_timeout_seconds() -> int:
    ttl = _jobcard_snapshot_ttl_seconds() * 4
    return max(120, ttl)


def _jobcard_snapshot_ttl_seconds() -> int:
    configured = getattr(settings, 'DESKERA_JOBCARD_SNAPSHOT_TTL_SECONDS', 900)
    try:
        ttl_value = int(configured)
    except (TypeError, ValueError):
        ttl_value = 900
    return max(60, ttl_value)


def _jobcard_snapshot_lock_seconds() -> int:
    return max(60, min(_jobcard_snapshot_ttl_seconds(), 180))


def _jobcard_search_blob(card: Dict[str, Any]) -> str:
    parts = (
        card.get("identifier"),
        card.get("work_order"),
        card.get("sales_order"),
        card.get("product"),
        card.get("station"),
        card.get("due_date"),
        card.get("operator"),
        card.get("status"),
        card.get("attachments"),
    )
    return " ".join(str(value) for value in parts if value and value != "—").lower()


def _jobcard_snapshot_record(card: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "identifier": card.get("identifier") or "",
        "work_order": card.get("work_order") or "—",
        "sales_order": card.get("sales_order") or "—",
        "product": card.get("product") or "—",
        "station": card.get("station") or "—",
        "status": card.get("status") or "—",
        "quantity": card.get("quantity") or "—",
        "due_date": card.get("due_date") or "—",
        "operator": card.get("operator") or "—",
        "attachments": card.get("attachments") or "—",
    }
    record["_search"] = _jobcard_search_blob(record)
    return record


def _build_jobcard_snapshot(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    sort_field = JOBCARD_SORT_FIELD_MAP.get(JOBCARD_DEFAULT_SORT_KEY, "jobCardCode")
    jobcards, meta, error, _ = _fetch_all_jobcards(
        client,
        portal_base=portal_base,
        sort_field=sort_field,
        sort_dir="DESC",
        max_pages=0,
    )
    if error:
        return None, error

    snapshot_records = [_jobcard_snapshot_record(card) for card in jobcards]
    snapshot = {
        "fetched_at_ts": time.time(),
        "meta": {
            **(meta or {}),
            "records": len(snapshot_records),
        },
        "records": snapshot_records,
    }
    return snapshot, None


def _jobcard_snapshot_is_stale(snapshot: Dict[str, Any]) -> bool:
    fetched_at_ts = snapshot.get("fetched_at_ts")
    if not isinstance(fetched_at_ts, (int, float)):
        return True
    return (time.time() - float(fetched_at_ts)) > _jobcard_snapshot_ttl_seconds()


def _refresh_jobcard_snapshot_worker(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    cache_key: str,
    lock_key: str,
    lock_token: Optional[str] = None,
) -> None:
    ttl_seconds = _jobcard_snapshot_ttl_seconds()
    snapshot_timeout = ttl_seconds * 4
    started_at_ts = time.time()

    sort_field = JOBCARD_SORT_FIELD_MAP.get(JOBCARD_DEFAULT_SORT_KEY, "jobCardCode")
    try:
        page_limit = int(getattr(settings, 'DESKERA_JOBCARD_PAGE_SIZE', 100))
    except (TypeError, ValueError):
        page_limit = 100
    page_limit = max(1, min(page_limit, 100))

    hard_max_pages = getattr(settings, 'DESKERA_PAGINATION_HARD_MAX_PAGES', 250)
    try:
        hard_max_pages_value: Optional[int] = int(hard_max_pages) if hard_max_pages is not None else None
    except (TypeError, ValueError):
        hard_max_pages_value = 250
    if hard_max_pages_value is not None and hard_max_pages_value <= 0:
        hard_max_pages_value = None

    aggregated: List[Dict[str, Any]] = []
    last_meta: Dict[str, Any] = {"page": 0, "limit": page_limit, "total": None, "has_more": True}
    last_error: Optional[str] = None
    skip_final_write = False

    try:
        page = 0
        while True:
            if hard_max_pages_value is not None and page >= hard_max_pages_value:
                last_meta = {**last_meta, "has_more": True}
                break

            page_records, page_meta, page_error, _ = _fetch_normalized_jobcards(
                client,
                portal_base=portal_base,
                page=page,
                limit=page_limit,
                sort_field=sort_field,
                sort_dir="DESC",
            )
            if page_error:
                last_error = page_error
                break

            for record in page_records:
                aggregated.append(_jobcard_snapshot_record(record))

            last_meta = page_meta or last_meta
            building = bool(last_meta.get("has_more"))
            meta = {
                **(last_meta or {}),
                "records": len(aggregated),
                "fetched_pages": page + 1,
                "building": building,
                "partial": building,
            }
            snapshot = {
                "started_at_ts": started_at_ts,
                "fetched_at_ts": time.time(),
                "meta": meta,
                "records": aggregated,
            }
            _deskera_cache_set(cache_key, snapshot, timeout=snapshot_timeout)

            if not last_meta.get("has_more"):
                break
            if not page_records:
                last_meta = {**last_meta, "has_more": False}
                break
            page += 1

        fetched_pages = last_meta.get("page")
        try:
            fetched_pages = int(fetched_pages) + 1
        except (TypeError, ValueError):
            fetched_pages = None
        meta_final: Dict[str, Any] = {
            **(last_meta or {}),
            "records": len(aggregated),
            "fetched_pages": (fetched_pages if fetched_pages is not None else None),
            "building": False,
        }

        reached_cap = hard_max_pages_value is not None and (meta_final.get("fetched_pages") or 0) >= hard_max_pages_value
        if last_error:
            meta_final["partial"] = True
            meta_final["error"] = last_error
            _deskera_cache_set(
                _jobcard_snapshot_error_key(client),
                _deskera_snapshot_error_payload(last_error),
                timeout=ttl_seconds,
            )

            if not aggregated:
                existing = _deskera_cache_get(cache_key)
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("records"), list)
                    and len(existing.get("records") or []) > 0
                ):
                    skip_final_write = True
        else:
            meta_final["partial"] = bool(meta_final.get("has_more") or reached_cap)
            if reached_cap:
                meta_final["has_more"] = True
            _deskera_cache_delete(_jobcard_snapshot_error_key(client))

        snapshot_final = {
            "started_at_ts": started_at_ts,
            "fetched_at_ts": time.time(),
            "meta": meta_final,
            "records": aggregated,
        }
        if not skip_final_write:
            _deskera_cache_set(cache_key, snapshot_final, timeout=snapshot_timeout)
    finally:
        _deskera_refresh_lock_release(lock_key, lock_token)


def _ensure_jobcard_snapshot_refresh(client: DeskeraMRPClient, *, portal_base: str, force: bool = False) -> str:
    cache_key = _jobcard_snapshot_cache_key(client)
    lock_key = _jobcard_snapshot_lock_key(client)
    lock_seconds = _jobcard_snapshot_lock_seconds()
    snapshot = _deskera_cache_get(cache_key)
    lock_present = _deskera_refresh_lock_present(lock_key, max_age_seconds=lock_seconds)
    recent_error = _deskera_cache_get(_jobcard_snapshot_error_key(client))

    if snapshot and isinstance(snapshot, dict) and not force and not _jobcard_snapshot_is_stale(snapshot):
        meta = snapshot.get("meta") or {}
        if lock_present:
            return "refreshing"
        if meta.get("building"):
            force = True
        else:
            return "ready"

    if (
        snapshot
        and isinstance(snapshot, dict)
        and _deskera_snapshot_error_is_recent(recent_error)
        and _deskera_snapshot_has_records(snapshot)
    ):
        return "stale"

    if _deskera_snapshot_error_is_recent(recent_error) and not _deskera_snapshot_has_records(snapshot):
        return "error"

    lock_token = _deskera_refresh_lock_acquire(lock_key, lock_seconds)
    if not lock_token:
        return "refreshing"

    Thread(
        target=_refresh_jobcard_snapshot_worker,
        kwargs={
            "client": client,
            "portal_base": portal_base,
            "cache_key": cache_key,
            "lock_key": lock_key,
            "lock_token": lock_token,
        },
        daemon=True,
    ).start()
    return "refreshing"


def _get_or_build_jobcard_snapshot(client: DeskeraMRPClient, *, portal_base: str) -> tuple[Optional[Dict[str, Any]], Optional[str], str]:
    cache_key = _jobcard_snapshot_cache_key(client)
    snapshot = _deskera_cache_get(cache_key)
    if snapshot and isinstance(snapshot, dict):
        meta = snapshot.get("meta") or {}
        if _jobcard_snapshot_is_stale(snapshot):
            recent_error = _deskera_cache_get(_jobcard_snapshot_error_key(client))
            if _deskera_snapshot_error_is_recent(recent_error) and _deskera_snapshot_has_records(snapshot):
                return snapshot, _deskera_snapshot_error_message(recent_error), "stale"
            _ensure_jobcard_snapshot_refresh(client, portal_base=portal_base, force=True)
            return snapshot, None, "stale"
        if meta.get("building") or meta.get("partial"):
            return snapshot, None, "refreshing"
        return snapshot, None, "ready"

    status = _ensure_jobcard_snapshot_refresh(client, portal_base=portal_base, force=True)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        snapshot = _deskera_cache_get(cache_key)
        if snapshot and isinstance(snapshot, dict):
            meta = snapshot.get("meta") or {}
            if meta.get("building") or meta.get("partial"):
                return snapshot, None, "refreshing"
            return snapshot, None, "ready"
        time.sleep(0.25)

    recent_error = _deskera_cache_get(_jobcard_snapshot_error_key(client))
    recent_error_message = _deskera_snapshot_error_message(recent_error)
    if recent_error_message:
        return None, recent_error_message, "error"
    return None, None, status


_WORKORDER_SNAPSHOT_VERSION = 4
_WORKORDER_SNAPSHOT_PREFIX = "magicwand:deskera:workorders:snapshot"
_WORKORDER_SNAPSHOT_LOCK_PREFIX = "magicwand:deskera:workorders:snapshot:lock"
_WORKORDER_STAKEHOLDER_SUMMARY_VERSION = 1
_WORKORDER_STAKEHOLDER_SUMMARY_PREFIX = "magicwand:deskera:workorders:stakeholder_summary"
_WORKORDER_STAKEHOLDER_SUMMARY_LOCK_PREFIX = "magicwand:deskera:workorders:stakeholder_summary:lock"


def _workorder_snapshot_cache_key(client: DeskeraMRPClient) -> str:
    return f"{_WORKORDER_SNAPSHOT_PREFIX}:v{_WORKORDER_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}"


def _workorder_snapshot_lock_key(client: DeskeraMRPClient) -> str:
    return f"{_WORKORDER_SNAPSHOT_LOCK_PREFIX}:v{_WORKORDER_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}"


def _workorder_snapshot_error_key(client: DeskeraMRPClient) -> str:
    return f"{_WORKORDER_SNAPSHOT_PREFIX}:v{_WORKORDER_SNAPSHOT_VERSION}:{_deskera_cache_namespace(client)}:last_error"


def _workorder_stakeholder_summary_cache_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    ts_value = 0
    if isinstance(snapshot_fetched_at_ts, (int, float)):
        try:
            ts_value = int(float(snapshot_fetched_at_ts) * 1000)
        except (TypeError, ValueError):
            ts_value = 0
    return (
        f"{_WORKORDER_STAKEHOLDER_SUMMARY_PREFIX}:v{_WORKORDER_STAKEHOLDER_SUMMARY_VERSION}:"
        f"{_deskera_cache_namespace(client)}:{ts_value}"
    )


def _workorder_stakeholder_summary_lock_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    ts_value = 0
    if isinstance(snapshot_fetched_at_ts, (int, float)):
        try:
            ts_value = int(float(snapshot_fetched_at_ts) * 1000)
        except (TypeError, ValueError):
            ts_value = 0
    return (
        f"{_WORKORDER_STAKEHOLDER_SUMMARY_LOCK_PREFIX}:v{_WORKORDER_STAKEHOLDER_SUMMARY_VERSION}:"
        f"{_deskera_cache_namespace(client)}:{ts_value}"
    )


def _workorder_stakeholder_summary_error_key(client: DeskeraMRPClient, *, snapshot_fetched_at_ts: Optional[float]) -> str:
    return f"{_workorder_stakeholder_summary_cache_key(client, snapshot_fetched_at_ts=snapshot_fetched_at_ts)}:error"


def _workorder_stakeholder_summary_timeout_seconds() -> int:
    ttl = _workorder_snapshot_ttl_seconds() * 4
    return max(120, ttl)


def _workorder_snapshot_ttl_seconds() -> int:
    configured = getattr(settings, 'DESKERA_WORKORDER_SNAPSHOT_TTL_SECONDS', 900)
    try:
        ttl_value = int(configured)
    except (TypeError, ValueError):
        ttl_value = 900
    return max(60, ttl_value)


def _workorder_snapshot_lock_seconds() -> int:
    return max(60, min(_workorder_snapshot_ttl_seconds(), 180))


def _workorder_search_blob(order: Dict[str, Any]) -> str:
    parts = (
        order.get("identifier"),
        order.get("documentSequenceCode"),
        order.get("workOrderCode"),
        order.get("description"),
        order.get("product"),
        order.get("compoundCode"),
        order.get("cutLength"),
        order.get("weight"),
        order.get("purchaseOrder"),
        order.get("parentWorkOrderSeqCode"),
        order.get("parentWorkOrderCode"),
        order.get("customer"),
        order.get("status"),
        order.get("jobStatus"),
        order.get("priority"),
        order.get("quantity"),
        order.get("due_date"),
        order.get("created_date"),
    )
    return " ".join(str(value) for value in parts if value and value != "—").lower()


def _workorder_snapshot_record(order: Dict[str, Any]) -> Dict[str, Any]:
    record = {
        "identifier": order.get("identifier") or "",
        "documentSequenceCode": order.get("documentSequenceCode") or order.get("identifier") or "",
        "workOrderCode": order.get("workOrderCode") or "—",
        "description": order.get("description") or "—",
        "product": order.get("product") or "—",
        "compoundCode": order.get("compoundCode") or "—",
        "cutLength": order.get("cutLength") or "—",
        "weight": order.get("weight") or "—",
        "purchaseOrder": order.get("purchaseOrder") or "—",
        "parentWorkOrderSeqCode": order.get("parentWorkOrderSeqCode") or "—",
        "parentWorkOrderCode": order.get("parentWorkOrderCode") or "—",
        "customer": order.get("customer") or "—",
        "quantity": order.get("quantity") or "—",
        "status": order.get("status") or "—",
        "jobStatus": order.get("jobStatus") or "—",
        "due_date": order.get("due_date") or "—",
        "created_date": order.get("created_date") or "—",
        "priority": order.get("priority") or "—",
    }
    record["_search"] = _workorder_search_blob(record)
    return record


def _workorder_snapshot_is_stale(snapshot: Dict[str, Any]) -> bool:
    fetched_at_ts = snapshot.get("fetched_at_ts")
    if not isinstance(fetched_at_ts, (int, float)):
        return True
    return (time.time() - float(fetched_at_ts)) > _workorder_snapshot_ttl_seconds()


def _refresh_workorder_snapshot_worker(
    client: DeskeraMRPClient,
    *,
    portal_base: str,
    cache_key: str,
    lock_key: str,
    lock_token: Optional[str] = None,
) -> None:
    ttl_seconds = _workorder_snapshot_ttl_seconds()
    snapshot_timeout = ttl_seconds * 4
    started_at_ts = time.time()

    sort_field = SORT_FIELD_MAP.get(DEFAULT_SORT_KEY, "createdOn")
    try:
        page_limit = int(getattr(settings, 'DESKERA_WORKORDER_PAGE_SIZE', 100))
    except (TypeError, ValueError):
        page_limit = 100
    page_limit = max(1, min(page_limit, 100))

    hard_max_pages = getattr(settings, 'DESKERA_PAGINATION_HARD_MAX_PAGES', 250)
    try:
        hard_max_pages_value: Optional[int] = int(hard_max_pages) if hard_max_pages is not None else None
    except (TypeError, ValueError):
        hard_max_pages_value = 250
    if hard_max_pages_value is not None and hard_max_pages_value <= 0:
        hard_max_pages_value = None

    aggregated: List[Dict[str, Any]] = []
    last_meta: Dict[str, Any] = {"page": 0, "limit": page_limit, "total": None, "has_more": True}
    last_error: Optional[str] = None
    skip_final_write = False

    try:
        page = 0
        while True:
            if hard_max_pages_value is not None and page >= hard_max_pages_value:
                last_meta = {**last_meta, "has_more": True}
                break

            page_records, page_meta, page_error, _ = _fetch_normalized_workorders(
                client,
                portal_base=portal_base,
                search="",
                status="",
                page=page,
                limit=page_limit,
                sort_field=sort_field,
                sort_dir="DESC",
                fetch_attachment_details=False,
            )
            if page_error:
                last_error = page_error
                break

            for record in page_records:
                aggregated.append(_workorder_snapshot_record(record))

            last_meta = page_meta or last_meta
            building = bool(last_meta.get("has_more"))
            meta = {
                **(last_meta or {}),
                "records": len(aggregated),
                "fetched_pages": page + 1,
                "building": building,
                "partial": building,
            }
            snapshot = {
                "started_at_ts": started_at_ts,
                "fetched_at_ts": time.time(),
                "meta": meta,
                "records": aggregated,
            }
            _deskera_cache_set(cache_key, snapshot, timeout=snapshot_timeout)

            if not last_meta.get("has_more"):
                break
            if not page_records:
                last_meta = {**last_meta, "has_more": False}
                break
            page += 1

        fetched_pages = last_meta.get("page")
        try:
            fetched_pages = int(fetched_pages) + 1
        except (TypeError, ValueError):
            fetched_pages = None
        meta_final: Dict[str, Any] = {
            **(last_meta or {}),
            "records": len(aggregated),
            "fetched_pages": (fetched_pages if fetched_pages is not None else None),
            "building": False,
        }

        reached_cap = hard_max_pages_value is not None and (meta_final.get("fetched_pages") or 0) >= hard_max_pages_value
        if last_error:
            meta_final["partial"] = True
            meta_final["error"] = last_error
            _deskera_cache_set(
                _workorder_snapshot_error_key(client),
                _deskera_snapshot_error_payload(last_error),
                timeout=ttl_seconds,
            )

            # If the refresh fails before loading any records (for example, a transient 403/5xx),
            # keep the last good snapshot instead of overwriting it with an empty placeholder.
            if not aggregated:
                existing = _deskera_cache_get(cache_key)
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("records"), list)
                    and len(existing.get("records") or []) > 0
                ):
                    skip_final_write = True
                else:
                    _deskera_cache_delete(cache_key)
                    skip_final_write = True
        else:
            meta_final["partial"] = bool(meta_final.get("has_more") or reached_cap)
            if reached_cap:
                meta_final["has_more"] = True
            _deskera_cache_delete(_workorder_snapshot_error_key(client))

        snapshot_final = {
            "started_at_ts": started_at_ts,
            "fetched_at_ts": time.time(),
            "meta": meta_final,
            "records": aggregated,
        }
        if not skip_final_write:
            _deskera_cache_set(cache_key, snapshot_final, timeout=snapshot_timeout)
    finally:
        _deskera_refresh_lock_release(lock_key, lock_token)


def _ensure_workorder_snapshot_refresh(client: DeskeraMRPClient, *, portal_base: str, force: bool = False) -> str:
    cache_key = _workorder_snapshot_cache_key(client)
    lock_key = _workorder_snapshot_lock_key(client)
    lock_seconds = _workorder_snapshot_lock_seconds()
    snapshot = _deskera_cache_get(cache_key)
    if _deskera_snapshot_is_empty_error(snapshot):
        _deskera_cache_delete(cache_key)
        snapshot = None
    lock_present = _deskera_refresh_lock_present(lock_key, max_age_seconds=lock_seconds)
    recent_error = _deskera_cache_get(_workorder_snapshot_error_key(client))

    if snapshot and isinstance(snapshot, dict) and not force and not _workorder_snapshot_is_stale(snapshot):
        meta = snapshot.get("meta") or {}
        if lock_present:
            return "refreshing"
        if meta.get("building"):
            force = True
        else:
            return "ready"

    if (
        snapshot
        and isinstance(snapshot, dict)
        and _deskera_snapshot_error_is_recent(recent_error)
        and _deskera_snapshot_has_records(snapshot)
    ):
        return "stale"

    if _deskera_snapshot_error_is_recent(recent_error) and not _deskera_snapshot_has_records(snapshot):
        return "error"

    lock_token = _deskera_refresh_lock_acquire(lock_key, lock_seconds)
    if not lock_token:
        return "refreshing"

    Thread(
        target=_refresh_workorder_snapshot_worker,
        kwargs={
            "client": client,
            "portal_base": portal_base,
            "cache_key": cache_key,
            "lock_key": lock_key,
            "lock_token": lock_token,
        },
        daemon=True,
    ).start()
    return "refreshing"


def _get_or_build_workorder_snapshot(client: DeskeraMRPClient, *, portal_base: str) -> tuple[Optional[Dict[str, Any]], Optional[str], str]:
    cache_key = _workorder_snapshot_cache_key(client)
    snapshot = _deskera_cache_get(cache_key)
    if _deskera_snapshot_is_empty_error(snapshot):
        _deskera_cache_delete(cache_key)
        snapshot = None
    if snapshot and isinstance(snapshot, dict):
        meta = snapshot.get("meta") or {}
        if _workorder_snapshot_is_stale(snapshot):
            recent_error = _deskera_cache_get(_workorder_snapshot_error_key(client))
            if _deskera_snapshot_error_is_recent(recent_error) and _deskera_snapshot_has_records(snapshot):
                return snapshot, _deskera_snapshot_error_message(recent_error), "stale"
            _ensure_workorder_snapshot_refresh(client, portal_base=portal_base, force=True)
            return snapshot, None, "stale"
        if meta.get("building") or meta.get("partial"):
            return snapshot, None, "refreshing"
        return snapshot, None, "ready"

    status = _ensure_workorder_snapshot_refresh(client, portal_base=portal_base, force=True)
    deadline = time.time() + 2.0
    while time.time() < deadline:
        snapshot = _deskera_cache_get(cache_key)
        if snapshot and isinstance(snapshot, dict):
            meta = snapshot.get("meta") or {}
            if meta.get("building") or meta.get("partial"):
                return snapshot, None, "refreshing"
            return snapshot, None, "ready"
        time.sleep(0.25)

    recent_error = _deskera_cache_get(_workorder_snapshot_error_key(client))
    recent_error_message = _deskera_snapshot_error_message(recent_error)
    if recent_error_message:
        return None, recent_error_message, "error"
    return None, None, status


def _internal_warmup_auth_config() -> tuple[str, str]:
    header_name = (
        str(getattr(settings, "INTERNAL_WARMUP_HEADER", "") or getattr(settings, "AUTH_BYPASS_HEADER", "X-Service-Auth"))
        .strip()
        or "X-Service-Auth"
    )
    token = str(getattr(settings, "INTERNAL_WARMUP_TOKEN", "") or getattr(settings, "AUTH_BYPASS_TOKEN", "")).strip()
    return header_name, token


def _internal_warmup_authorized(request) -> tuple[bool, str]:
    header_name, expected_token = _internal_warmup_auth_config()
    if not expected_token:
        return False, "Internal warmup token is not configured."

    candidates = [
        str(request.headers.get(header_name, "") or "").strip(),
    ]
    authorization = str(request.headers.get("Authorization", "") or "").strip()
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())

    for candidate in candidates:
        if candidate and secrets.compare_digest(candidate, expected_token):
            return True, ""
    return False, "Missing or invalid internal warmup token."


def _warmup_database_status() -> Dict[str, Any]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"ok": True, "status": "ok"}
    except Exception as exc:
        logger.exception("Warmup database check failed")
        return {"ok": False, "status": "error", "message": str(exc)}


def _snapshot_record_count(snapshot: Optional[Dict[str, Any]]) -> int:
    if not isinstance(snapshot, dict):
        return 0
    records = snapshot.get("records")
    return len(records) if isinstance(records, list) else 0


def _warmup_workorder_snapshot_status(*, force_refresh: bool) -> Dict[str, Any]:
    try:
        client = _build_deskera_client()
    except DeskeraMRPConfigError as exc:
        return {
            "ok": False,
            "ready": False,
            "status": "not_configured",
            "cache_source": "none",
            "record_count": 0,
            "message": str(exc),
        }

    cache_key = _workorder_snapshot_cache_key(client)
    snapshot = _deskera_cache_get(cache_key)
    record_count = _snapshot_record_count(snapshot)
    if record_count:
        refresh_status = _ensure_workorder_snapshot_refresh(
            client,
            portal_base=_deskera_portal_base(),
            force=force_refresh,
        )
        return {
            "ok": True,
            "ready": True,
            "status": refresh_status,
            "cache_source": "active_cache",
            "record_count": record_count,
            "stale": _workorder_snapshot_is_stale(snapshot) if isinstance(snapshot, dict) else False,
        }

    load_error = ""
    try:
        from mrp.services import _load_cached_deskera_workorder_snapshot

        fallback_snapshot, fallback_source, load_error = _load_cached_deskera_workorder_snapshot()
    except Exception as exc:
        fallback_snapshot = None
        fallback_source = "none"
        load_error = str(exc)

    fallback_record_count = _snapshot_record_count(fallback_snapshot)
    if fallback_record_count:
        _deskera_cache_set(
            cache_key,
            fallback_snapshot,
            timeout=_workorder_snapshot_ttl_seconds() * 4,
        )
        refresh_status = _ensure_workorder_snapshot_refresh(
            client,
            portal_base=_deskera_portal_base(),
            force=force_refresh,
        )
        return {
            "ok": True,
            "ready": True,
            "status": refresh_status,
            "cache_source": fallback_source or "persistent_cache",
            "record_count": fallback_record_count,
            "stale": _workorder_snapshot_is_stale(fallback_snapshot) if isinstance(fallback_snapshot, dict) else False,
        }

    refresh_status = _ensure_workorder_snapshot_refresh(
        client,
        portal_base=_deskera_portal_base(),
        force=True,
    )
    recent_error = _deskera_snapshot_error_message(_deskera_cache_get(_workorder_snapshot_error_key(client)))
    message = recent_error or load_error or "No cached Deskera work-order snapshot is ready yet."
    return {
        "ok": False,
        "ready": False,
        "status": refresh_status,
        "cache_source": fallback_source or "none",
        "record_count": 0,
        "message": message,
    }


def _start_best_effort_workorder_warmup() -> Dict[str, Any]:
    try:
        client = _build_deskera_client()
        status = _ensure_workorder_snapshot_refresh(
            client,
            portal_base=_deskera_portal_base(),
            force=False,
        )
        snapshot = _deskera_cache_get(_workorder_snapshot_cache_key(client))
        return {
            "ok": True,
            "status": status,
            "record_count": _snapshot_record_count(snapshot),
        }
    except Exception as exc:
        logger.info("Best-effort Deskera warmup skipped: %s", exc)
        return {"ok": False, "status": "skipped", "message": str(exc)}


@require_http_methods(["GET"])
def app_engine_warmup(request):
    """App Engine warmup hook; keep it lightweight and always non-blocking."""

    database = _warmup_database_status()
    deskera = _start_best_effort_workorder_warmup()
    return JsonResponse(
        {
            "ok": True,
            "checks": {
                "database": database,
                "deskera_workorders": deskera,
            },
        }
    )


@require_http_methods(["GET"])
def internal_deskera_warmup(request):
    """Protected deploy smoke endpoint used before moving traffic to a new version."""

    authorized, auth_error = _internal_warmup_authorized(request)
    if not authorized:
        status_code = 503 if "not configured" in auth_error else 401
        return JsonResponse({"ok": False, "ready": False, "message": auth_error}, status=status_code)

    database = _warmup_database_status()
    force_refresh = str(request.GET.get("refresh", "") or "").strip().lower() in {"1", "true", "yes", "force"}
    deskera = _warmup_workorder_snapshot_status(force_refresh=force_refresh)
    ready = bool(database.get("ok") and deskera.get("ready"))
    status_code = 200 if ready else 503
    return JsonResponse(
        {
            "ok": ready,
            "ready": ready,
            "checks": {
                "database": database,
                "deskera_workorders": deskera,
            },
        },
        status=status_code,
    )


def _deskera_snapshot_page(
    snapshot: Dict[str, Any],
    *,
    search: str,
    status: str,
    page: int,
    limit: int,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    snapshot_records = snapshot.get("records") or []
    filtered_records: List[Dict[str, Any]] = [
        dict(record)
        for record in snapshot_records
        if isinstance(record, dict)
    ]

    status_normalized = status.strip().lower()
    if status_normalized and status_normalized not in {"all", "any"}:
        filtered_records = [
            record
            for record in filtered_records
            if str(record.get("status") or "").strip().lower() == status_normalized
        ]

    if search:
        search_lower = search.lower()
        filtered_records = [
            record
            for record in filtered_records
            if search_lower in str(record.get("_search") or "").lower()
        ]

    start_idx = page * limit
    end_idx = start_idx + limit
    paged_records = filtered_records[start_idx:end_idx]
    total_filtered = len(filtered_records)

    return paged_records, {
        "page": page,
        "limit": limit,
        "total": total_filtered,
        "has_more": end_idx < total_filtered,
        "source": "snapshot",
    }


def _tokenize_jobcard_question(question: str) -> List[str]:
    tokens = [token.lower() for token in _JOB_CARD_TOKEN_RE.findall(question or "")]
    stopwords = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "is",
        "are",
        "was",
        "were",
        "show",
        "list",
        "give",
        "tell",
        "me",
        "please",
        "job",
        "jobs",
        "card",
        "cards",
        "jobcard",
        "jobcards",
        "work",
        "order",
        "orders",
        "workorder",
        "workorders",
        "wo",
        "so",
    }
    filtered: List[str] = []
    for token in tokens:
        if token in stopwords:
            continue
        if len(token) > 2:
            filtered.append(token)
            continue
        if token.isdigit() and len(token) >= 2:
            filtered.append(token)
    return filtered


def _normalize_compound_code(value: Any) -> str:
    trimmed = str(value or "").strip().upper()
    if not trimmed or trimmed in {"—", "N/A"}:
        return ""
    normalized = "".join(ch for ch in trimmed if ch.isalnum())
    if normalized.startswith("AG") and len(normalized) > 2:
        return normalized[2:]
    return normalized


def _extract_compound_reference_from_question(
    question: str,
    workorders: List[Dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    question_norm = "".join(ch for ch in str(question or "").upper() if ch.isalnum())
    if not question_norm:
        return None, None
    question_norm_no_prefix = question_norm[2:] if question_norm.startswith("AG") else question_norm

    candidates: Dict[str, str] = {}
    for order in workorders:
        raw = str(order.get("compoundCode") or "").strip()
        normalized = _normalize_compound_code(raw)
        if not normalized:
            continue
        if normalized not in candidates and raw and raw != "—":
            candidates[normalized] = raw

    for normalized in sorted(candidates.keys(), key=len, reverse=True):
        if normalized and (normalized in question_norm or normalized in question_norm_no_prefix):
            return normalized, candidates.get(normalized) or normalized
    return None, None


def _parse_decimal_quantity(value: Any) -> Optional[Decimal]:
    if value in (None, "", "—", []):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text or text == "—":
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _format_decimal(value: Decimal) -> str:
    if value is None:
        return "—"
    try:
        if value == value.to_integral_value():
            return str(int(value))
    except (InvalidOperation, ValueError):
        pass
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _question_mentions_this_week(question: str) -> bool:
    lowered = (question or "").lower()
    return any(token in lowered for token in ("this week", "this week's", "this weeks", "this week’s"))


def _question_mentions_total_weight(question: str) -> bool:
    lowered = (question or "").lower()
    if "weight" in lowered:
        return True
    if "kg" in lowered or "kgs" in lowered or "kilogram" in lowered or "kilograms" in lowered:
        return True
    return False


def _this_week_window() -> tuple[date, date]:
    start = timezone.localdate()
    end = start + timedelta(days=6)
    return start, end


def _select_relevant_jobcards(question: str, jobcards: List[Dict[str, Any]], *, limit: int = 60) -> List[Dict[str, Any]]:
    if not jobcards:
        return []

    tokens = _tokenize_jobcard_question(question)
    if not tokens:
        return jobcards[:limit]

    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for index, card in enumerate(jobcards):
        haystack = (card.get("_search") or "").lower()
        if not haystack:
            continue
        score = 0
        for token in tokens:
            if token in haystack:
                score += 1
        if score:
            scored.append((score, -index, card))

    if not scored:
        return jobcards[:limit]

    scored.sort(reverse=True)
    return [card for _, __, card in scored[:limit]]


def _summarize_jobcards(jobcards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    total = meta.get("total")
    record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(jobcards)
    if meta.get("building") or meta.get("partial"):
        source = meta.get("source")
        if source == "recent_page":
            header = f"Dataset is syncing; using the most recent {record_count} job cards"
        else:
            header = f"Dataset is syncing; loaded {record_count} job cards so far"
    else:
        header = f"Snapshot contains {record_count} job cards"

    if total is not None:
        header = f"{header} (Deskera reports total={total})."
    else:
        header = f"{header}."

    status_counts = Counter(card.get("status") or "—" for card in jobcards)
    station_counts = Counter(card.get("station") or "—" for card in jobcards)

    top_status = ", ".join(f"{label}:{count}" for label, count in status_counts.most_common(6))
    top_stations = ", ".join(f"{label}:{count}" for label, count in station_counts.most_common(6))

    return "\n".join(
        line
        for line in (
            header,
            f"Top statuses: {top_status}" if top_status else "",
            f"Top stations: {top_stations}" if top_stations else "",
        )
        if line
    )


def _build_jobcard_stakeholder_summary_dataset(jobcards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(jobcards)
    total_reported = meta.get("total")
    fetched_at_ts = meta.get("fetched_at_ts")
    fetched_at_local = None
    if isinstance(fetched_at_ts, (int, float)):
        try:
            fetched_at_local = timezone.localtime(datetime.fromtimestamp(float(fetched_at_ts), tz=timezone.utc))
        except (OSError, ValueError, TypeError):
            fetched_at_local = None

    today = timezone.localdate()
    week_start, week_end = _this_week_window()
    done_statuses = {"completed", "closed", "cancelled", "canceled"}

    status_counts = Counter(str(card.get("status") or "—").strip() or "—" for card in jobcards)
    station_counts = Counter(str(card.get("station") or "—").strip() or "—" for card in jobcards)
    work_order_counts = Counter(str(card.get("work_order") or "—").strip() or "—" for card in jobcards)

    active_cards: List[Dict[str, Any]] = []
    done_count = 0
    missing_due: List[str] = []
    overdue: List[tuple[datetime, Dict[str, Any]]] = []
    due_this_week: List[tuple[datetime, Dict[str, Any]]] = []

    for card in jobcards:
        status = str(card.get("status") or "").strip()
        status_lower = status.lower()
        if status_lower in done_statuses:
            done_count += 1
            continue

        active_cards.append(card)
        due_dt = _parse_jobcard_due_date(card.get("due_date"))
        if due_dt is None:
            identifier = str(card.get("identifier") or "").strip()
            if identifier:
                missing_due.append(identifier)
            continue

        due_date = due_dt.date()
        if due_date < today:
            overdue.append((due_dt, card))
        if week_start <= due_date <= week_end:
            due_this_week.append((due_dt, card))

    overdue.sort(key=lambda item: (item[0], str(item[1].get("identifier") or "")))
    due_this_week.sort(key=lambda item: (item[0], str(item[1].get("identifier") or "")))

    station_week_stats: Dict[str, Dict[str, Any]] = {}
    work_order_week_stats: Dict[str, Dict[str, Any]] = {}
    missing_qty: List[str] = []

    for due_dt, card in due_this_week:
        station = str(card.get("station") or "—").strip() or "—"
        work_order = str(card.get("work_order") or "—").strip() or "—"
        identifier = str(card.get("identifier") or "").strip()

        qty_value = _parse_decimal_quantity(card.get("quantity"))

        station_stat = station_week_stats.setdefault(
            station,
            {"job_cards": 0, "quantity_total": Decimal("0"), "missing_qty": 0},
        )
        station_stat["job_cards"] += 1

        work_order_stat = work_order_week_stats.setdefault(
            work_order,
            {"job_cards": 0, "quantity_total": Decimal("0"), "missing_qty": 0},
        )
        work_order_stat["job_cards"] += 1

        if qty_value is None:
            station_stat["missing_qty"] += 1
            work_order_stat["missing_qty"] += 1
            if identifier:
                missing_qty.append(identifier)
        else:
            station_stat["quantity_total"] += qty_value
            work_order_stat["quantity_total"] += qty_value

    station_sorted = sorted(
        station_week_stats.items(),
        key=lambda item: (item[1]["quantity_total"], item[1]["job_cards"]),
        reverse=True,
    )
    work_order_sorted = sorted(
        work_order_week_stats.items(),
        key=lambda item: (item[1]["quantity_total"], item[1]["job_cards"]),
        reverse=True,
    )

    lines: List[str] = []
    header = f"Job card snapshot: {record_count} records"
    if total_reported is not None:
        header += f" (Deskera reports total={total_reported})"
    if fetched_at_local is not None:
        header += f" • fetched {fetched_at_local.strftime('%Y-%m-%d %H:%M %Z')}"
    lines.append(header)
    lines.append(f"Today: {today.isoformat()} • This week window: {week_start.isoformat()} → {week_end.isoformat()}")
    lines.append("")

    lines.append("At a glance:")
    lines.append(f"- Total job cards: {record_count}")
    lines.append(f"- Active (not completed/closed/cancelled): {len(active_cards)}")
    lines.append(f"- Done: {done_count}")
    lines.append(f"- Overdue (active): {len(overdue)}")
    lines.append(f"- Due this week (active): {len(due_this_week)}")
    lines.append(f"- Missing due dates (active): {len(missing_due)}")
    lines.append(f"- Missing quantity for this-week totals: {len(set(missing_qty))}")
    lines.append("")

    lines.append("Status counts (all records):")
    for label, count in status_counts.most_common(12):
        lines.append(f"- {label}: {count}")
    lines.append("")

    job_status_counts = Counter(str(order.get("jobStatus") or "—").strip() or "—" for order in workorders)
    lines.append("Job status counts (all records):")
    for label, count in job_status_counts.most_common(12):
        lines.append(f"- {label}: {count}")
    lines.append("")

    lines.append("Top stations (all records):")
    for station, count in station_counts.most_common(12):
        lines.append(f"- {station}: {count}")
    lines.append("")

    lines.append("Top work orders (all records):")
    for work_order, count in work_order_counts.most_common(10):
        lines.append(f"- {work_order}: {count}")
    lines.append("")

    lines.append("This week (by station):")
    lines.append("| Station | Job cards | Total Qty | Missing qty |")
    lines.append("|---|---:|---:|---:|")
    for station, stat in station_sorted[:15]:
        lines.append(
            "| "
            + " | ".join(
                [
                    station,
                    str(stat["job_cards"]),
                    _format_decimal(stat["quantity_total"]),
                    str(stat["missing_qty"]),
                ]
            )
            + " |"
        )
    if not station_sorted:
        lines.append("| — | 0 | 0 | 0 |")
    lines.append("")

    lines.append("This week (by work order):")
    lines.append("| Work order | Job cards | Total Qty | Missing qty |")
    lines.append("|---|---:|---:|---:|")
    for work_order, stat in work_order_sorted[:15]:
        lines.append(
            "| "
            + " | ".join(
                [
                    work_order,
                    str(stat["job_cards"]),
                    _format_decimal(stat["quantity_total"]),
                    str(stat["missing_qty"]),
                ]
            )
            + " |"
        )
    if not work_order_sorted:
        lines.append("| — | 0 | 0 | 0 |")
    lines.append("")

    def build_sample_table(title: str, items: List[tuple[datetime, Dict[str, Any]]]) -> None:
        lines.append(title)
        lines.append("| Job Card | Work Order | Station | Due Date | Status | Quantity | Operator |")
        lines.append("|---|---|---|---|---|---:|---|")
        for due_dt, card in items[:25]:
            identifier = str(card.get("identifier") or "—").strip() or "—"
            work_order = str(card.get("work_order") or "—").strip() or "—"
            station = str(card.get("station") or "—").strip() or "—"
            status = str(card.get("status") or "—").strip() or "—"
            operator = str(card.get("operator") or "—").strip() or "—"
            qty_value = _parse_decimal_quantity(card.get("quantity"))
            qty_display = _format_decimal(qty_value) if qty_value is not None else str(card.get("quantity") or "—").strip()
            due_label = due_dt.strftime("%d %b %Y") if due_dt else (str(card.get("due_date") or "—").strip() or "—")
            lines.append(f"| {identifier} | {work_order} | {station} | {due_label} | {status} | {qty_display} | {operator} |")
        if not items:
            lines.append("| — | — | — | — | — | — | — |")
        lines.append("")

    build_sample_table("Overdue job cards (sample):", overdue)
    build_sample_table("Due this week job cards (sample):", due_this_week)

    if missing_due:
        preview = ", ".join(missing_due[:25])
        suffix = f", +{len(missing_due) - 25} more" if len(missing_due) > 25 else ""
        lines.append(f"Active job cards missing due dates ({len(missing_due)}): {preview}{suffix}")

    return "\n".join(lines).strip()


def _refresh_jobcard_stakeholder_summary_worker(
    client: DeskeraMRPClient,
    *,
    cache_key: str,
    lock_key: str,
    error_key: str,
    snapshot_fetched_at_ts: Optional[float],
    jobcards: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> None:
    timeout_seconds = _jobcard_stakeholder_summary_timeout_seconds()
    try:
        dataset = _build_jobcard_stakeholder_summary_dataset(jobcards, meta)
        ai_service = AIReplyService()
        prompt = (
            "You are an operations analyst writing a stakeholder-ready snapshot summary for Deskera Job Cards.\n"
            "Only use the dataset below; do not invent fields or numbers.\n\n"
            "Formatting rules:\n"
            "- Output in Markdown.\n"
            "- No email greetings, subjects, or signatures.\n"
            "- Keep it concise and executive-friendly.\n"
            "- Include an 'At a glance' table, plus at least one planning table (e.g., stations due this week).\n"
            "- If there are data gaps (missing due dates / quantities), call them out explicitly.\n"
            "- Do not wrap tables in code fences.\n\n"
            "Dataset:\n"
            f"{dataset}\n\n"
            "Summary:"
        )
        summary = ai_service.generate_reply(
            AIReplyRequest(
                thread_summary=prompt,
                tone="operations analyst",
                purpose="raw",
            )
        )
        cache.set(
            cache_key,
            {
                "generated_at_ts": time.time(),
                "snapshot_fetched_at_ts": snapshot_fetched_at_ts,
                "summary": (summary or "").strip(),
            },
            timeout=timeout_seconds,
        )
        cache.delete(error_key)
    except (AIServiceError, Exception) as exc:
        cache.set(error_key, str(exc), timeout=timeout_seconds)
    finally:
        cache.delete(lock_key)


def _parse_jobcard_due_date(value: Any) -> Optional[datetime]:
    if value in (None, "", "—"):
        return None
    trimmed = str(value).strip()
    if not trimmed or trimmed == "—":
        return None
    for fmt in ("%d %b %Y %H:%M IST", "%d %b %Y"):
        try:
            return datetime.strptime(trimmed, fmt)
        except ValueError:
            continue
    return None


def _question_mentions_overdue(question: str) -> bool:
    lowered = (question or "").lower()
    return any(token in lowered for token in ("overdue", "past due", "past-due", "late", "delayed"))


def _question_mentions_parent_workorders(question: str) -> bool:
    lowered = (question or "").lower()
    if "parent" not in lowered:
        return False
    return any(token in lowered for token in ("work order", "workorder", "order", "orders", "wo"))


def _question_requests_parent_linkage(question: str) -> bool:
    lowered = (question or "").lower()
    return any(
        token in lowered
        for token in (
            "linked",
            "linkage",
            "child",
            "children",
            "parent-child",
            "parent child",
            "hierarchy",
            "relationship",
            "under",
            "belongs",
            "associated",
        )
    )


def _find_overdue_jobcards(jobcards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = timezone.localdate()
    done_statuses = {"completed", "closed", "cancelled", "canceled"}

    overdue: List[tuple[datetime, Dict[str, Any]]] = []
    for card in jobcards:
        status = str(card.get("status") or "").strip().lower()
        if status in done_statuses:
            continue
        due_dt = _parse_jobcard_due_date(card.get("due_date"))
        if due_dt is None:
            continue
        if due_dt.date() < today:
            overdue.append((due_dt, card))

    overdue.sort(key=lambda item: item[0])
    return [card for _, card in overdue]


def _build_jobcard_copilot_context(question: str, jobcards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    summary = _summarize_jobcards(jobcards, meta)
    if _question_mentions_overdue(question):
        overdue_cards = _find_overdue_jobcards(jobcards)
        lines: List[str] = []
        for card in overdue_cards[:60]:
            lines.append(
                f"{card.get('identifier')}: WO={card.get('work_order')} SO={card.get('sales_order')} "
                f"product={card.get('product')} station={card.get('station')} "
                f"operator={card.get('operator')} qty={card.get('quantity')} status={card.get('status')} due={card.get('due_date')}"
            )
        return (
            summary
            + f"\n\nOverdue job cards (showing {min(len(overdue_cards), 60)} of {len(overdue_cards)}):\n"
            + "\n".join(lines or ["No overdue job cards found in the current snapshot."])
        )

    relevant = _select_relevant_jobcards(question, jobcards, limit=60)

    lines: List[str] = []
    for card in relevant:
        lines.append(
            f"{card.get('identifier')}: WO={card.get('work_order')} SO={card.get('sales_order')} "
            f"product={card.get('product')} station={card.get('station')} "
            f"operator={card.get('operator')} qty={card.get('quantity')} status={card.get('status')} due={card.get('due_date')}"
        )

    if not lines:
        lines.append("No relevant job cards matched the question tokens; showing most recent records only.")

    return summary + "\n\nRelevant job cards:\n" + "\n".join(lines[:60])


def _select_relevant_workorders(question: str, workorders: List[Dict[str, Any]], *, limit: int = 60) -> List[Dict[str, Any]]:
    if not workorders:
        return []

    tokens = _tokenize_jobcard_question(question)
    if not tokens:
        return workorders[:limit]

    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for index, order in enumerate(workorders):
        haystack = (order.get("_search") or "").lower()
        if not haystack:
            continue
        score = 0
        for token in tokens:
            if token in haystack:
                score += 1
        if score:
            scored.append((score, -index, order))

    if not scored:
        return workorders[:limit]

    scored.sort(reverse=True)
    return [order for _, __, order in scored[:limit]]


def _find_overdue_workorders(workorders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    today = timezone.localdate()
    done_statuses = {"completed", "closed", "cancelled", "canceled"}

    overdue: List[tuple[datetime, Dict[str, Any]]] = []
    for order in workorders:
        status = str(order.get("status") or "").strip().lower()
        if status in done_statuses:
            continue
        due_dt = _parse_jobcard_due_date(order.get("due_date"))
        if due_dt is None:
            continue
        if due_dt.date() < today:
            overdue.append((due_dt, order))

    overdue.sort(key=lambda item: item[0])
    return [order for _, order in overdue]


def _summarize_workorders(workorders: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    total = meta.get("total")
    record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(workorders)

    if meta.get("building") or meta.get("partial"):
        source = meta.get("source")
        if source == "recent_page":
            header = f"Dataset is syncing; using the most recent {record_count} work orders"
        else:
            header = f"Dataset is syncing; loaded {record_count} work orders so far"
    else:
        header = f"Snapshot contains {record_count} work orders"

    if total is not None:
        header = f"{header} (Deskera reports total={total})."
    else:
        header = f"{header}."

    status_counts = Counter(order.get("status") or "—" for order in workorders)
    job_status_counts = Counter(order.get("jobStatus") or "—" for order in workorders)
    customer_counts = Counter(order.get("customer") or "—" for order in workorders)
    description_count = sum(1 for order in workorders if str(order.get("description") or "").strip() not in {"", "—"})
    cut_length_count = sum(1 for order in workorders if str(order.get("cutLength") or "").strip() not in {"", "—"})
    purchase_order_count = sum(1 for order in workorders if str(order.get("purchaseOrder") or "").strip() not in {"", "—"})

    top_status = ", ".join(f"{label}:{count}" for label, count in status_counts.most_common(6))
    top_job_status = ", ".join(f"{label}:{count}" for label, count in job_status_counts.most_common(6))
    top_customers = ", ".join(f"{label}:{count}" for label, count in customer_counts.most_common(6))

    return "\n".join(
        line
        for line in (
            header,
            f"Top statuses: {top_status}" if top_status else "",
            f"Top job statuses: {top_job_status}" if top_job_status else "",
            f"Top customers: {top_customers}" if top_customers else "",
            (
                "Description populated: "
                f"{description_count}; Cut Length populated: {cut_length_count}; PO populated: {purchase_order_count}"
            ),
        )
        if line
    )


def _format_workorder_copilot_line(order: Dict[str, Any], *, include_parent: bool = False) -> str:
    fragments = [
        f"product={order.get('product')}",
        f"description={order.get('description')}",
        f"cut_length={order.get('cutLength')}",
        f"compound={order.get('compoundCode')}",
        f"customer={order.get('customer')}",
        f"purchase_order={order.get('purchaseOrder')}",
        f"qty={order.get('quantity')}",
        f"weight={order.get('weight')}",
        f"status={order.get('status')}",
        f"job_status={order.get('jobStatus')}",
        f"due={order.get('due_date')}",
        f"created={order.get('created_date')}",
    ]
    if include_parent:
        parent_seq = order.get("parentWorkOrderSeqCode")
        parent_code = order.get("parentWorkOrderCode")
        parent_label = parent_seq if parent_seq not in (None, "", "—") else parent_code
        if parent_label not in (None, "", "—"):
            fragments.append(f"parent={parent_label}")
            fragments.append(f"parent_seq={parent_seq}")
            fragments.append(f"parent_code={parent_code}")
    return f"{order.get('identifier')}: " + " ".join(fragments)


def _build_workorder_stakeholder_summary_dataset(workorders: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(workorders)
    total_reported = meta.get("total")
    fetched_at_ts = meta.get("fetched_at_ts")
    fetched_at_local = None
    if isinstance(fetched_at_ts, (int, float)):
        try:
            fetched_at_local = timezone.localtime(datetime.fromtimestamp(float(fetched_at_ts), tz=timezone.utc))
        except (OSError, ValueError, TypeError):
            fetched_at_local = None

    today = timezone.localdate()
    week_start, week_end = _this_week_window()
    done_statuses = {"completed", "closed", "cancelled", "canceled"}

    status_counts = Counter(str(order.get("status") or "—").strip() or "—" for order in workorders)

    active_orders: List[Dict[str, Any]] = []
    done_count = 0
    missing_due: List[str] = []
    overdue: List[tuple[datetime, Dict[str, Any]]] = []
    due_this_week: List[tuple[datetime, Dict[str, Any]]] = []

    for order in workorders:
        status = str(order.get("status") or "").strip()
        status_lower = status.lower()
        if status_lower in done_statuses:
            done_count += 1
            continue

        active_orders.append(order)
        due_dt = _parse_jobcard_due_date(order.get("due_date"))
        if due_dt is None:
            identifier = str(order.get("identifier") or "").strip()
            if identifier:
                missing_due.append(identifier)
            continue

        due_date = due_dt.date()
        if due_date < today:
            overdue.append((due_dt, order))
        if week_start <= due_date <= week_end:
            due_this_week.append((due_dt, order))

    overdue.sort(key=lambda item: (item[0], str(item[1].get("identifier") or "")))
    due_this_week.sort(key=lambda item: (item[0], str(item[1].get("identifier") or "")))

    compound_week_stats: Dict[str, Dict[str, Any]] = {}
    customer_week_stats: Dict[str, Dict[str, Any]] = {}
    missing_weight_or_qty: List[str] = []

    for due_dt, order in due_this_week:
        compound = str(order.get("compoundCode") or "—").strip() or "—"
        customer = str(order.get("customer") or "—").strip() or "—"
        identifier = str(order.get("identifier") or "").strip()

        qty_value = _parse_decimal_quantity(order.get("quantity"))
        weight_value = _parse_decimal_quantity(order.get("weight"))

        compound_stat = compound_week_stats.setdefault(
            compound,
            {"work_orders": 0, "quantity_total": Decimal("0"), "weight_total_kg": Decimal("0"), "missing": 0},
        )
        compound_stat["work_orders"] += 1

        customer_stat = customer_week_stats.setdefault(
            customer,
            {"work_orders": 0, "quantity_total": Decimal("0"), "weight_total_kg": Decimal("0"), "missing": 0},
        )
        customer_stat["work_orders"] += 1

        if qty_value is not None:
            compound_stat["quantity_total"] += qty_value
            customer_stat["quantity_total"] += qty_value

        if qty_value is None or weight_value is None:
            compound_stat["missing"] += 1
            customer_stat["missing"] += 1
            if identifier:
                missing_weight_or_qty.append(identifier)
        else:
            try:
                required_kg = (weight_value * qty_value) / Decimal("1000")
            except (InvalidOperation, ValueError):
                compound_stat["missing"] += 1
                customer_stat["missing"] += 1
                if identifier:
                    missing_weight_or_qty.append(identifier)
            else:
                compound_stat["weight_total_kg"] += required_kg
                customer_stat["weight_total_kg"] += required_kg

    compound_sorted = sorted(
        compound_week_stats.items(),
        key=lambda item: (item[1]["weight_total_kg"], item[1]["quantity_total"], item[1]["work_orders"]),
        reverse=True,
    )
    customer_sorted = sorted(
        customer_week_stats.items(),
        key=lambda item: (item[1]["weight_total_kg"], item[1]["quantity_total"], item[1]["work_orders"]),
        reverse=True,
    )
    purchase_order_count = sum(1 for order in workorders if str(order.get("purchaseOrder") or "").strip() not in {"", "—"})

    lines: List[str] = []
    header = f"Work order snapshot: {record_count} records"
    if total_reported is not None:
        header += f" (Deskera reports total={total_reported})"
    if fetched_at_local is not None:
        header += f" • fetched {fetched_at_local.strftime('%Y-%m-%d %H:%M %Z')}"
    lines.append(header)
    lines.append(f"Today: {today.isoformat()} • This week window: {week_start.isoformat()} → {week_end.isoformat()}")
    lines.append("")
    lines.append("At a glance:")
    lines.append(f"- Total work orders: {record_count}")
    lines.append(f"- Active (not completed/closed/cancelled): {len(active_orders)}")
    lines.append(f"- Done: {done_count}")
    lines.append(f"- Overdue (active): {len(overdue)}")
    lines.append(f"- Due this week (active): {len(due_this_week)}")
    lines.append(f"- Missing due dates (active): {len(missing_due)}")
    lines.append(f"- Missing weight or quantity for this-week kg calc: {len(set(missing_weight_or_qty))}")
    lines.append(f"- Purchase Order populated: {purchase_order_count}")
    lines.append("")

    lines.append("Status counts (all records):")
    for label, count in status_counts.most_common(12):
        lines.append(f"- {label}: {count}")
    lines.append("")

    lines.append("Formula for required weight:")
    lines.append("- `Required (kg) = Weight (g) × Quantity ÷ 1000`")
    lines.append("")

    lines.append("This week (by compound):")
    lines.append("| Compound | Work orders | Total Qty | Total Required (kg) | Missing weight/qty |")
    lines.append("|---|---:|---:|---:|---:|")
    for compound, stat in compound_sorted[:15]:
        lines.append(
            "| "
            + " | ".join(
                [
                    compound,
                    str(stat["work_orders"]),
                    _format_decimal(stat["quantity_total"]),
                    _format_decimal(stat["weight_total_kg"].quantize(Decimal("0.001")) if stat["weight_total_kg"] else Decimal("0")),
                    str(stat["missing"]),
                ]
            )
            + " |"
        )
    if not compound_sorted:
        lines.append("| — | 0 | 0 | 0 | 0 |")
    lines.append("")

    lines.append("This week (by customer):")
    lines.append("| Customer | Work orders | Total Qty | Total Required (kg) | Missing weight/qty |")
    lines.append("|---|---:|---:|---:|---:|")
    for customer, stat in customer_sorted[:15]:
        lines.append(
            "| "
            + " | ".join(
                [
                    customer,
                    str(stat["work_orders"]),
                    _format_decimal(stat["quantity_total"]),
                    _format_decimal(stat["weight_total_kg"].quantize(Decimal("0.001")) if stat["weight_total_kg"] else Decimal("0")),
                    str(stat["missing"]),
                ]
            )
            + " |"
        )
    if not customer_sorted:
        lines.append("| — | 0 | 0 | 0 | 0 |")
    lines.append("")

    def build_sample_table(title: str, items: List[tuple[datetime, Dict[str, Any]]]) -> None:
        lines.append(title)
        lines.append("| Work Order | PO | Compound | Description | Cut Length | Customer | Due Date | Status | Job Status | Qty | Weight (g) | Required (kg) |")
        lines.append("|---|---|---|---|---|---|---|---|---|---:|---:|---:|")
        for due_dt, order in items[:20]:
            identifier = str(order.get("identifier") or "—").strip() or "—"
            purchase_order = str(order.get("purchaseOrder") or "—").strip() or "—"
            compound = str(order.get("compoundCode") or "—").strip() or "—"
            description = str(order.get("description") or "—").strip() or "—"
            cut_length = str(order.get("cutLength") or "—").strip() or "—"
            customer = str(order.get("customer") or "—").strip() or "—"
            status = str(order.get("status") or "—").strip() or "—"
            job_status = str(order.get("jobStatus") or "—").strip() or "—"
            qty_value = _parse_decimal_quantity(order.get("quantity"))
            weight_value = _parse_decimal_quantity(order.get("weight"))
            qty_display = _format_decimal(qty_value) if qty_value is not None else str(order.get("quantity") or "—").strip()
            weight_display = _format_decimal(weight_value) if weight_value is not None else str(order.get("weight") or "—").strip()
            required_display = "—"
            if qty_value is not None and weight_value is not None:
                try:
                    required_display = _format_decimal(((weight_value * qty_value) / Decimal("1000")).quantize(Decimal("0.001")))
                except (InvalidOperation, ValueError):
                    required_display = "—"
            due_label = due_dt.strftime("%d %b %Y") if due_dt else (str(order.get("due_date") or "—").strip() or "—")
            lines.append(
                f"| {identifier} | {purchase_order} | {compound} | {description} | {cut_length} | {customer} | {due_label} | {status} | {job_status} | {qty_display} | {weight_display} | {required_display} |"
            )
        if not items:
            lines.append("| — | — | — | — | — | — | — | — | — | — | — | — |")
        lines.append("")

    build_sample_table("Overdue work orders (sample):", overdue)
    build_sample_table("Due this week work orders (sample):", due_this_week)

    if missing_due:
        preview = ", ".join(missing_due[:25])
        suffix = f", +{len(missing_due) - 25} more" if len(missing_due) > 25 else ""
        lines.append(f"Active work orders missing due dates ({len(missing_due)}): {preview}{suffix}")

    return "\n".join(lines).strip()


def _refresh_workorder_stakeholder_summary_worker(
    client: DeskeraMRPClient,
    *,
    cache_key: str,
    lock_key: str,
    error_key: str,
    snapshot_fetched_at_ts: Optional[float],
    workorders: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> None:
    timeout_seconds = _workorder_stakeholder_summary_timeout_seconds()
    try:
        dataset = _build_workorder_stakeholder_summary_dataset(workorders, meta)
        ai_service = AIReplyService()
        prompt = (
            "You are an operations analyst writing a stakeholder-ready snapshot summary for Deskera Work Orders.\n"
            "Only use the dataset below; do not invent fields or numbers.\n\n"
            "Formatting rules:\n"
            "- Output in Markdown.\n"
            "- No email greetings, subjects, or signatures.\n"
            "- Keep it concise and executive-friendly.\n"
            "- Include an 'At a glance' table, plus at least one table that helps planning (e.g., top compounds due this week).\n"
            "- Include both Status and Job Status when citing specific work orders if those fields are populated.\n"
            "- Include Purchase Order (PO) whenever it is populated for a cited work order.\n"
            "- When citing specific work orders, include Description and Cut Length whenever those fields are populated.\n"
            "- If there are data gaps (missing due dates / weights), call them out explicitly.\n"
            "- Do not wrap tables in code fences.\n\n"
            "Dataset:\n"
            f"{dataset}\n\n"
            "Summary:"
        )
        summary = ai_service.generate_reply(
            AIReplyRequest(
                thread_summary=prompt,
                tone="operations analyst",
                purpose="raw",
            )
        )
        cache.set(
            cache_key,
            {
                "generated_at_ts": time.time(),
                "snapshot_fetched_at_ts": snapshot_fetched_at_ts,
                "summary": (summary or "").strip(),
            },
            timeout=timeout_seconds,
        )
        cache.delete(error_key)
    except (AIServiceError, Exception) as exc:
        cache.set(error_key, str(exc), timeout=timeout_seconds)
    finally:
        cache.delete(lock_key)


def _build_workorder_copilot_context(question: str, workorders: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    summary = _summarize_workorders(workorders, meta)
    question_mentions_parent = _question_mentions_parent_workorders(question)
    question_wants_parent_linkage = question_mentions_parent and _question_requests_parent_linkage(question)

    if question_wants_parent_linkage:
        linked: List[Dict[str, Any]] = [
            order
            for order in workorders
            if (order.get("parentWorkOrderSeqCode") not in (None, "", "—"))
            or (order.get("parentWorkOrderCode") not in (None, "", "—"))
        ]
        lines: List[str] = []
        for order in linked[:120]:
            lines.append(_format_workorder_copilot_line(order, include_parent=True))
        return (
            summary
            + f"\n\nWork orders linked to a parent work order (showing {min(len(linked), 120)} of {len(linked)}):\n"
            + "\n".join(lines or ["No work orders with parent linkage found in the current snapshot."])
        )

    if _question_mentions_overdue(question):
        overdue_orders = _find_overdue_workorders(workorders)
        lines: List[str] = []
        for order in overdue_orders[:60]:
            lines.append(_format_workorder_copilot_line(order))
        return (
            summary
            + f"\n\nOverdue work orders (showing {min(len(overdue_orders), 60)} of {len(overdue_orders)}):\n"
            + "\n".join(lines or ["No overdue work orders found in the current snapshot."])
        )

    relevant = _select_relevant_workorders(question, workorders, limit=60)
    lines: List[str] = []
    for order in relevant:
        lines.append(_format_workorder_copilot_line(order, include_parent=True))
    if not lines:
        lines.append("No relevant work orders matched the question tokens; showing most recent records only.")

    return summary + "\n\nRelevant work orders:\n" + "\n".join(lines[:60])


def _describe_workorders_dataset(workorders: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    if not workorders:
        return "No work orders available in the current dataset."

    lines: List[str] = []
    for order in workorders:
        lines.append(_format_workorder_copilot_line(order))

    total = meta.get('total')
    header = f"Showing {len(workorders)} work orders (Deskera reports total={total if total is not None else 'unknown'})."
    return header + "\n" + "\n".join(lines)


def _generate_deskera_answer(question: str, workorders: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    if _question_mentions_parent_workorders(question) and _question_requests_parent_linkage(question):
        def normalize_parent_value(value: Any) -> str:
            trimmed = str(value or "").strip()
            return "" if trimmed in {"", "—"} else trimmed

        parent_pairs: List[tuple[str, str]] = []
        for order in workorders:
            child = str(order.get("identifier") or "").strip()
            parent_seq = normalize_parent_value(order.get("parentWorkOrderSeqCode"))
            parent_code = normalize_parent_value(order.get("parentWorkOrderCode"))
            parent = parent_seq or parent_code
            if parent:
                parent_pairs.append((child or "—", parent))

        if not parent_pairs:
            return (
                "No work orders in the current snapshot have parent linkage populated "
                "(fields: parentWorkOrderSeqCode / parentWorkOrderCode)."
            )

        grouped: Dict[str, List[str]] = {}
        for child, parent in parent_pairs:
            grouped.setdefault(parent, [])
            if child not in grouped[parent]:
                grouped[parent].append(child)

        lines: List[str] = []
        for parent, children in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
            preview = ", ".join(children[:15])
            suffix = f", +{len(children) - 15} more" if len(children) > 15 else ""
            lines.append(f"Parent {parent}: {preview}{suffix}")
            if len(lines) >= 60:
                break

        header = (
            f"Found {len(parent_pairs)} child work orders linked to {len(grouped)} parent work orders."
        )
        return header + "\n" + "\n".join(lines)

    if _question_mentions_this_week(question):
        compound_key, compound_label = _extract_compound_reference_from_question(question, workorders)
        if compound_key:
            start_date, end_date = _this_week_window()
            wants_weight = _question_mentions_total_weight(question)

            matches: List[Dict[str, Any]] = []
            total_qty = Decimal("0")
            total_weight_kg = Decimal("0")
            missing_qty: List[str] = []
            missing_weight: List[str] = []
            missing_weight_or_qty: List[str] = []

            for order in workorders:
                due_dt = _parse_jobcard_due_date(order.get("due_date"))
                if due_dt is None:
                    continue
                due_date = due_dt.date()
                if due_date < start_date or due_date > end_date:
                    continue

                order_compound = order.get("compoundCode")
                if _normalize_compound_code(order_compound) != compound_key:
                    continue

                qty = _parse_decimal_quantity(order.get("quantity"))
                weight_g = _parse_decimal_quantity(order.get("weight"))
                if qty is None:
                    identifier = str(order.get("identifier") or "").strip()
                    if identifier:
                        missing_qty.append(identifier)
                else:
                    total_qty += qty

                if wants_weight:
                    identifier = str(order.get("identifier") or "").strip()
                    if weight_g is None and identifier:
                        missing_weight.append(identifier)
                    if qty is None or weight_g is None:
                        if identifier:
                            missing_weight_or_qty.append(identifier)
                    else:
                        try:
                            total_weight_kg += (weight_g * qty) / Decimal("1000")
                        except (InvalidOperation, ValueError):
                            if identifier:
                                missing_weight_or_qty.append(identifier)

                matches.append(order)

            matches.sort(key=lambda o: (_parse_jobcard_due_date(o.get("due_date")) or datetime.max, str(o.get("identifier") or "")))

            start_label = start_date.strftime("%d %b %Y")
            end_label = end_date.strftime("%d %b %Y")
            header = f"This week ({start_label} → {end_label}) · Compound: {compound_label}"

            if not matches:
                return (
                    f"{header}\n\n"
                    f"No work orders found for this compound within the week window based on the current snapshot."
                )

            if wants_weight:
                total_kg_display = _format_decimal(total_weight_kg.quantize(Decimal("0.001")))
                lines: List[str] = [
                    header,
                    "",
                    "Formula: `Weight (g) × Quantity ÷ 1000 = kg`",
                    "",
                    "| Compound | Work Order | PO | Description | Cut Length | Due Date | Weight (g) | Quantity | Required (kg) | Status | Job Status |",
                    "|---|---|---|---|---|---|---:|---:|---:|---|---|",
                ]
                for order in matches[:200]:
                    compound = str(order.get("compoundCode") or "—").strip() or "—"
                    identifier = str(order.get("identifier") or "—").strip() or "—"
                    purchase_order = str(order.get("purchaseOrder") or "—").strip() or "—"
                    description = str(order.get("description") or "—").strip() or "—"
                    cut_length = str(order.get("cutLength") or "—").strip() or "—"
                    due_dt = _parse_jobcard_due_date(order.get("due_date"))
                    due = due_dt.strftime("%d %b %Y") if due_dt else (str(order.get("due_date") or "—").strip() or "—")
                    qty_value = _parse_decimal_quantity(order.get("quantity"))
                    qty_display = _format_decimal(qty_value) if qty_value is not None else str(order.get("quantity") or "—").strip()
                    weight_value = _parse_decimal_quantity(order.get("weight"))
                    weight_display = _format_decimal(weight_value) if weight_value is not None else str(order.get("weight") or "—").strip()
                    required_kg: Optional[Decimal] = None
                    if qty_value is not None and weight_value is not None:
                        try:
                            required_kg = ((weight_value * qty_value) / Decimal("1000")).quantize(Decimal("0.001"))
                        except (InvalidOperation, ValueError):
                            required_kg = None
                    required_display = _format_decimal(required_kg) if required_kg is not None else "—"
                    status = str(order.get("status") or "—").strip() or "—"
                    job_status = str(order.get("jobStatus") or "—").strip() or "—"
                    lines.append(f"| {compound} | {identifier} | {purchase_order} | {description} | {cut_length} | {due} | {weight_display} | {qty_display} | {required_display} | {status} | {job_status} |")

                lines.extend(["", f"Total required weight (kg): {total_kg_display}"])

                if missing_weight_or_qty:
                    preview = ", ".join(missing_weight_or_qty[:12])
                    suffix = f", +{len(missing_weight_or_qty) - 12} more" if len(missing_weight_or_qty) > 12 else ""
                    lines.extend(["", f"Note: {len(missing_weight_or_qty)} work orders were excluded from the kg total due to missing/non-numeric weight or quantity: {preview}{suffix}"])

                return "\n".join(lines)

            lines: List[str] = [
                header,
                "",
                "| Compound | Work Order | PO | Description | Cut Length | Due Date | Quantity | Status | Job Status |",
                "|---|---|---|---|---|---|---:|---|---|",
            ]
            for order in matches[:200]:
                compound = str(order.get("compoundCode") or "—").strip() or "—"
                identifier = str(order.get("identifier") or "—").strip() or "—"
                purchase_order = str(order.get("purchaseOrder") or "—").strip() or "—"
                description = str(order.get("description") or "—").strip() or "—"
                cut_length = str(order.get("cutLength") or "—").strip() or "—"
                due_dt = _parse_jobcard_due_date(order.get("due_date"))
                due = due_dt.strftime("%d %b %Y") if due_dt else (str(order.get("due_date") or "—").strip() or "—")
                qty_value = _parse_decimal_quantity(order.get("quantity"))
                qty_display = _format_decimal(qty_value) if qty_value is not None else str(order.get("quantity") or "—").strip()
                status = str(order.get("status") or "—").strip() or "—"
                job_status = str(order.get("jobStatus") or "—").strip() or "—"
                lines.append(f"| {compound} | {identifier} | {purchase_order} | {description} | {cut_length} | {due} | {qty_display} | {status} | {job_status} |")

            total_line = f"Total (from Quantity column): {_format_decimal(total_qty)}"
            lines.extend(["", total_line])

            if missing_qty:
                preview = ", ".join(missing_qty[:12])
                suffix = f", +{len(missing_qty) - 12} more" if len(missing_qty) > 12 else ""
                lines.extend(["", f"Note: {len(missing_qty)} work orders have non-numeric quantity and were excluded: {preview}{suffix}"])

            return "\n".join(lines)

    ai_service = AIReplyService()
    dataset_summary = _build_workorder_copilot_context(question, workorders, meta)
    prompt = (
        "You are Deskera Copilot (operations analyst).\n"
        "Only answer using the dataset below. If required fields/records are missing, say what's missing.\n\n"
        "Formatting rules:\n"
        "- Output in Markdown.\n"
        "- Do NOT include an email subject, greeting, or signature.\n"
        "- When listing more than 2 work orders, use a Markdown table (not bullet points).\n"
        "- Include Job Status in answers whenever that field is available for the listed work orders.\n"
        "- Include Purchase Order (PO) in answers whenever that field is available for the listed work orders.\n"
        "- Do not omit Description or Cut Length when those fields are present for listed work orders.\n"
        "- Prefer columns: Compound | Work Order | PO | Description | Cut Length | Due Date | Quantity | Status | Job Status (add Customer if helpful).\n"
        "- If grouping by compound, include a per-compound total quantity.\n"
        "- Do not wrap tables in code fences.\n\n"
        "Dataset:\n"
        f"{dataset_summary}\n\n"
        "Planner question:\n"
        f"{question}\n\n"
        "Answer:"
    )
    return ai_service.generate_reply(
        AIReplyRequest(
            thread_summary=prompt,
            tone="operations analyst",
            purpose="raw",
        )
    )


def _describe_workstations_dataset(workstations: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    if not workstations:
        return "No work stations available in the current dataset."

    lines: List[str] = []
    for station in workstations:
        identifier = station.get('identifier') or 'N/A'
        name = station.get('name') or 'Unnamed'
        center = station.get('work_center') or 'Unknown center'
        resource = station.get('resource') or 'Unassigned'
        status = station.get('status') or '—'
        capacity = station.get('capacity') or '—'
        shift = station.get('shift') or '—'
        lines.append(
            f"{identifier} ({name}): center={center} resource={resource} status={status} capacity={capacity} shift={shift}"
        )

    total = meta.get('total')
    header = (
        f"Showing {len(workstations)} work stations"
        + (f" (Deskera reports total={total})" if total is not None else "")
    )
    return header + "\n" + "\n".join(lines)


def _generate_workstation_answer(question: str, workstations: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    ai_service = AIReplyService()
    dataset_summary = _describe_workstations_dataset(workstations, meta)
    prompt = (
        "You are Deskera Copilot (production planning assistant).\n"
        "Only answer using the dataset below. If the information is missing, say it's not available.\n\n"
        "Formatting rules:\n"
        "- Output in Markdown.\n"
        "- Do NOT include an email greeting/signature.\n"
        "- Use tables when listing multiple stations.\n\n"
        "Dataset:\n"
        f"{dataset_summary}\n\n"
        "Planner question:\n"
        f"{question}\n\n"
        "Answer:"
    )
    return ai_service.generate_reply(
        AIReplyRequest(
            thread_summary=prompt,
            tone="operations analyst",
            purpose="raw",
        )
    )


def _alloc_mtc_certificate_numbers() -> Dict[str, str]:
    """
    Allocate MTC/CJC numbers for MTC generation.

    Format: mtc-DD-MM-YYYY-<seq starting at 100>, cjc-DD-MM-YYYY-<seq starting at 100>
    """
    today = timezone.localdate()
    year = today.year
    date_str = today.strftime("%d-%m-%Y")
    try:
        mtc_seq = next_certificate_serial("mtc", year=year)
        cjc_seq = next_certificate_serial("cjc", year=year)
        mtc_no = f"mtc-{date_str}-{mtc_seq}"
        cjc_no = f"cjc-{date_str}-{cjc_seq}"
    except Exception:
        # If migrations haven't been applied yet (or DB is unavailable), avoid breaking MTC downloads.
        # Fall back to a time-based number while still honoring the requested format.
        seed = int(time.time()) % 900000
        mtc_no = f"mtc-{date_str}-{100 + seed}"
        cjc_no = f"cjc-{date_str}-{100 + seed}"
    return {
        "mtc_no": mtc_no,
        "cjc_no": cjc_no,
        # Common template variants
        "MTC_NO": mtc_no,
        "CJC_NO": cjc_no,
        # Backwards compatibility with older templates naming this as "MJC".
        "mjc_no": mtc_no,
        "MJC_NO": mtc_no,
    }


def _deskera_mtc_text(value: Any) -> str:
    if value in (None, "", [], "-", "—"):
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw in {"-", "—"}:
            return ""
        if raw[:1] in {'"', "[", "{"} and raw[-1:] in {'"', "]", "}"}:
            try:
                decoded = json.loads(raw)
            except Exception:
                decoded = None
            else:
                if isinstance(decoded, (int, float, bool)):
                    return str(decoded).strip()
                if isinstance(decoded, str):
                    return decoded.strip()
        return raw
    return ""


def _pick_deskera_mtc_text(record: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _deskera_mtc_text(record.get(key))
        if value:
            return value
    return ""


def _extract_mtc_named_custom_field_text(record: Dict[str, Any], *field_names: str) -> str:
    def normalize_name(candidate: Any) -> str:
        raw = str(candidate or "").strip().lower()
        return "".join(character for character in raw if character.isalnum())

    target_names = {normalize_name(name) for name in field_names if str(name or "").strip()}
    if not target_names:
        return ""

    custom_containers = (
        "customField",
        "customFields",
        "custom_fields",
        "customFieldValues",
        "custom_field_values",
        "additionalFields",
        "additional_fields",
        "userDefinedFields",
        "user_defined_fields",
        "udf",
    )
    container_set = {name.lower() for name in custom_containers}

    def matches(name: Any) -> bool:
        return normalize_name(name) in target_names

    def extract_item_value(item: Dict[str, Any]) -> str:
        for key in ("value", "fieldValue", "field_value", "displayValue", "display_value"):
            value = _deskera_mtc_text(item.get(key))
            if value:
                return value
        return ""

    def extract_from_container(container: Any) -> str:
        if isinstance(container, list):
            for item in container:
                if not isinstance(item, dict):
                    continue
                if not any(
                    matches(item.get(name_key))
                    for name_key in ("label", "name", "fieldName", "field_name", "key", "shortName", "short_name")
                ):
                    continue
                value = extract_item_value(item)
                if value:
                    return value
            return ""
        if isinstance(container, dict):
            for name_key, value_candidate in container.items():
                if not matches(name_key):
                    continue
                value = _deskera_mtc_text(value_candidate)
                if value:
                    return value
        return ""

    for container_key in custom_containers:
        extracted = extract_from_container(record.get(container_key))
        if extracted:
            return extracted

    def search(obj: Any) -> str:
        if isinstance(obj, dict):
            for key, value in obj.items():
                lowered = str(key).lower()
                if lowered in container_set:
                    extracted = extract_from_container(value)
                    if extracted:
                        return extracted
                if matches(key):
                    extracted = _deskera_mtc_text(value)
                    if extracted:
                        return extracted
                nested = search(value)
                if nested:
                    return nested
        elif isinstance(obj, list):
            for item in obj:
                nested = search(item)
                if nested:
                    return nested
        return ""

    return search(record)


def _extract_mtc_invoice_no_from_workorder(record: Dict[str, Any]) -> str:
    value = _pick_deskera_mtc_text(
        record,
        "invoiceNo",
        "invoiceNumber",
        "invoiceCode",
        "invoiceDocumentSequenceCode",
        "salesInvoiceNo",
        "salesInvoiceNumber",
        "salesInvoiceCode",
        "salesInvoiceSequenceCode",
        "salesInvoiceDocumentSequenceCode",
    )
    if value:
        return value
    return _extract_mtc_named_custom_field_text(
        record,
        "invoice no",
        "invoice number",
        "invoice code",
        "sales invoice no",
        "sales invoice number",
        "sales invoice code",
    )


def _extract_mtc_cjc_no_from_workorder(record: Dict[str, Any]) -> str:
    value = _pick_deskera_mtc_text(
        record,
        "cjcNo",
        "cjcNumber",
        "cjcCode",
        "customerJobCardNo",
        "customerJobCardNumber",
        "customerJCNo",
        "customerJCNumber",
    )
    if value:
        return value
    return _extract_mtc_named_custom_field_text(
        record,
        "cjc no",
        "cjc number",
        "cjc code",
        "customer job card no",
        "customer job card number",
        "customer jc no",
        "customer jc number",
    )


def _extract_mtc_jc_number_from_workorder(record: Dict[str, Any]) -> str:
    value = _pick_deskera_mtc_text(
        record,
        "jcNumber",
        "jcNo",
        "jobCardNo",
        "jobCardNumber",
        "jobCardCode",
        "jobCardDocumentSequenceCode",
    )
    if value:
        return value
    return _extract_mtc_named_custom_field_text(
        record,
        "jc number",
        "jc no",
        "job card number",
        "job card no",
        "jobcard number",
        "jobcard no",
    )


def _extract_mtc_item_description_from_workorder(record: Dict[str, Any], normalized: Dict[str, Any]) -> str:
    value = _extract_mtc_named_custom_field_text(
        record,
        "description",
        "item description",
        "item descriptions",
        "description of goods",
        "goods description",
        "product description",
    )
    if value:
        return value
    return (
        _deskera_mtc_text(normalized.get("description"))
        or _pick_deskera_mtc_text(record, "description", "productDescription", "itemDescription", "goodsDescription")
        or _deskera_mtc_text(normalized.get("product"))
    )


def _extract_mtc_item_quantity_from_workorder(record: Dict[str, Any], normalized: Dict[str, Any]) -> str:
    value = _pick_deskera_mtc_text(
        record,
        "packingQuantity",
        "packingQty",
        "packageQuantity",
        "packQuantity",
        "quantityWithUom",
        "quantityWithUOM",
    )
    if value:
        return value
    value = _extract_mtc_named_custom_field_text(
        record,
        "packing quantity",
        "package quantity",
        "pack quantity",
        "quantity with uom",
        "quantity",
        "qty",
    )
    if value:
        return value
    return _deskera_mtc_text(normalized.get("quantity")) or _pick_deskera_mtc_text(
        record,
        "quantity",
        "manufactureQuantity",
        "actualQuantity",
        "plannedQuantity",
        "orderQuantity",
    )


def _extract_mtc_no_of_boxes_from_workorder(record: Dict[str, Any]) -> str:
    value = _pick_deskera_mtc_text(
        record,
        "noOfBoxes",
        "noOfBox",
        "numberOfBoxes",
        "boxCount",
        "boxes",
        "cartons",
        "cartonCount",
    )
    if value:
        return value
    return _extract_mtc_named_custom_field_text(
        record,
        "no of boxes",
        "no of box",
        "no. of boxes",
        "no. of box",
        "number of boxes",
        "box count",
        "boxes",
        "cartons",
    )


def _jobcard_identifier_from_raw(record: Dict[str, Any]) -> str:
    return _pick_deskera_mtc_text(
        record,
        "documentSequenceCode",
        "jobCardCode",
        "jobCardNo",
        "jobCardNumber",
        "jobCard",
        "code",
        "id",
    )


def _fetch_mtc_jobcard_number_for_workorder(
    client: DeskeraMRPClient,
    *,
    work_order_code: str,
    work_order_identifier: str,
) -> str:
    candidates: List[str] = []
    for candidate in (work_order_code, work_order_identifier):
        cleaned = str(candidate or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    for candidate in candidates:
        try:
            result = client.fetch_jobcards(
                page=0,
                limit=10,
                sort="jobCardCode",
                sort_dir="DESC",
                extra_filters={"workOrderCode": candidate},
            )
        except DeskeraMRPError:
            continue
        for record in result.get("records") or []:
            if not isinstance(record, dict):
                continue
            identifier = _jobcard_identifier_from_raw(record)
            if identifier:
                return identifier

    for candidate in candidates:
        try:
            result = client.fetch_jobcards(
                search=candidate,
                page=0,
                limit=10,
                sort="jobCardCode",
                sort_dir="DESC",
            )
        except DeskeraMRPError:
            continue
        for record in result.get("records") or []:
            if not isinstance(record, dict):
                continue
            identifier = _jobcard_identifier_from_raw(record)
            if identifier:
                return identifier

    return ""


def _build_mtc_deskera_context(
    *,
    client: DeskeraMRPClient,
    record: Dict[str, Any],
    normalized: Dict[str, Any],
) -> Dict[str, Any]:
    current_date = timezone.localdate().strftime("%d %b %Y")
    customer_name = _deskera_mtc_text(normalized.get("customer")) or _pick_deskera_mtc_text(
        record,
        "customerName",
        "accountName",
        "customer",
        "contactName",
    )
    po_no = _deskera_mtc_text(normalized.get("purchaseOrder")) or _extract_purchase_order_from_deskera_workorder(record)
    invoice_no = _extract_mtc_invoice_no_from_workorder(record)
    cjc_no = _extract_mtc_cjc_no_from_workorder(record)
    jc_number = _extract_mtc_jc_number_from_workorder(record)
    item_description = _extract_mtc_item_description_from_workorder(record, normalized)
    item_quantity = _extract_mtc_item_quantity_from_workorder(record, normalized)
    no_of_boxes = _extract_mtc_no_of_boxes_from_workorder(record)
    if not jc_number:
        jc_number = _fetch_mtc_jobcard_number_for_workorder(
            client,
            work_order_code=str(normalized.get("workOrderCode") or ""),
            work_order_identifier=str(normalized.get("identifier") or ""),
        )

    mtc_values = {
        "invoice_no": invoice_no,
        "customer_name": customer_name,
        "po_no": po_no,
        "purchase_order_no": po_no,
        "jc_number": jc_number,
        "job_card_no": jc_number,
        "mtc_date": current_date,
        "date": current_date,
        "item_description": item_description,
        "item_quantity": item_quantity,
        "packing_quantity": item_quantity,
        "no_of_boxes": no_of_boxes,
        "number_of_boxes": no_of_boxes,
        "boxes": no_of_boxes,
    }
    if item_description or item_quantity or no_of_boxes:
        mtc_values["mtc_items"] = [
            {
                "description": item_description,
                "quantity": item_quantity,
                "no_of_boxes": no_of_boxes,
            }
        ]
    if cjc_no:
        mtc_values["cjc_no"] = cjc_no
        mtc_values["cjc_number"] = cjc_no

    context: Dict[str, Any] = {
        **mtc_values,
        "INVOICE_NO": invoice_no,
        "CUSTOMER_NAME": customer_name,
        "PO_NO": po_no,
        "PURCHASE_ORDER_NO": po_no,
        "JC_NUMBER": jc_number,
        "JOB_CARD_NO": jc_number,
        "MTC_DATE": current_date,
        "DATE": current_date,
        "ITEM_DESCRIPTION": item_description,
        "ITEM_QUANTITY": item_quantity,
        "PACKING_QUANTITY": item_quantity,
        "NO_OF_BOXES": no_of_boxes,
        "NUMBER_OF_BOXES": no_of_boxes,
        "BOXES": no_of_boxes,
        "mtc_items": mtc_values.get("mtc_items", []),
        "MTC_ITEMS": mtc_values.get("mtc_items", []),
        "deskera_mtc": mtc_values,
    }
    if cjc_no:
        context["CJC_NO"] = cjc_no
        context["CJC_NUMBER"] = cjc_no
    return context


def _describe_jobcards_dataset(jobcards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    if not jobcards:
        return "No job cards available in the current dataset."

    lines: List[str] = []
    for card in jobcards:
        identifier = card.get('identifier') or 'N/A'
        wo = card.get('work_order') or 'Unknown WO'
        so = card.get('sales_order') or 'N/A'
        product = card.get('product') or 'Unknown product'
        station = card.get('station') or 'Unknown station'
        operator = card.get('operator') or 'Unassigned'
        status = card.get('status') or '—'
        qty = card.get('quantity') or '—'
        due = card.get('due_date') or '—'
        attachment_names = card.get('attachment_names') or []
        attachments_str = "none"
        if isinstance(attachment_names, (list, tuple)) and attachment_names:
            limited = list(attachment_names)[:3]
            attachments_str = ", ".join(map(str, limited))
            if len(attachment_names) > len(limited):
                attachments_str = f"{attachments_str} (+{len(attachment_names) - len(limited)} more)"
        lines.append(
            f"{identifier}: WO={wo} SO={so} product={product} station={station} "
            f"operator={operator} qty={qty} status={status} due={due} attachments=[{attachments_str}]"
        )

    total = meta.get('total')
    header = (
        f"Showing {len(jobcards)} job cards"
        + (f" (Deskera reports total={total})" if total is not None else "")
    )
    return header + "\n" + "\n".join(lines)


def _generate_jobcard_answer(question: str, jobcards: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    ai_service = AIReplyService()
    dataset_summary = _build_jobcard_copilot_context(question, jobcards, meta)
    prompt = (
        "You are Deskera Copilot (production scheduling assistant).\n"
        "Only answer using the dataset below. If required fields/records are missing, say what's missing.\n\n"
        "Formatting rules:\n"
        "- Output in Markdown.\n"
        "- Do NOT include an email greeting/signature.\n"
        "- When listing more than 2 job cards, use a Markdown table (not bullet points).\n"
        "- Prefer columns: Job Card | Work Order | Product | Station | Status | Due Date | Quantity.\n"
        "- Do not wrap tables in code fences.\n\n"
        "Dataset:\n"
        f"{dataset_summary}\n\n"
        "Planner question:\n"
        f"{question}\n\n"
        "Answer:"
    )
    return ai_service.generate_reply(
        AIReplyRequest(
            thread_summary=prompt,
            tone="operations analyst",
            purpose="raw",
        )
    )

def home(request):
    """
    Main MagicWand home page.
    Shows landing page for unauthenticated users, API info for authenticated users.
    """
    if request.user.is_authenticated:
        # Authenticated users should land on the hub view inside MagicWand
        return redirect('dashboard_hub')
    
    # Show landing page for unauthenticated users
    return render(request, 'magicwand/home.html')


def dev_login(request):
    """Local-only login shortcut for testing mobile layouts without OAuth."""
    if not getattr(settings, "DEBUG", False):
        raise Http404()

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username="local-mobile-test",
        defaults={
            "email": "local-mobile-test@example.com",
            "first_name": "Local",
            "last_name": "Tester",
            "is_staff": True,
            "is_superuser": True,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(request.GET.get("next") or "dashboard_hub")


def resolve_frontend_base(request):
    configured = (getattr(settings, 'MAGICWAND_FRONTEND_URL', '') or '').strip().rstrip('/')
    request_base = request.build_absolute_uri('/').rstrip('/')

    if not configured:
        return request_base

    configured_host = urlsplit(configured).netloc
    request_host = urlsplit(request_base).netloc

    if configured_host and request_host and configured_host.lower() != request_host.lower():
        return request_base

    return configured or request_base


def _get_google_reauth_url() -> str:
    try:
        return reverse("socialaccount_login", args=("google",))
    except Exception:
        return "/accounts/google/login/?process=login"


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


def service_redirect(request, setting_name, service_label):
    target = (getattr(settings, setting_name, '') or '').strip()
    if not target:
        return render(request, 'magicwand/service_missing.html', {'service_label': service_label}, status=502)
    if not target.startswith(('http://', 'https://')):
        target = request.build_absolute_uri(target)
    parsed = urlsplit(target)
    if not parsed.path:
        target = parsed._replace(path='/').geturl()
    return redirect(target)


@login_required
def magicwand_settings(request):
    """
    User-facing settings page.

    If an external settings portal is configured, surface it as a quick link but
    always provide a local fallback so the Settings nav never becomes a dead-end.
    """
    from django.contrib.auth.models import User

    from dashboard.models import UserServiceAccess

    settings_portal_url = (getattr(settings, "SETTINGS_PORTAL_URL", "") or "").strip()
    if settings_portal_url and not settings_portal_url.startswith(("http://", "https://")):
        settings_portal_url = request.build_absolute_uri(settings_portal_url)

    service_slugs = list(SERVICE_DEFINITIONS.keys())
    service_definitions = [{"slug": slug, "label": SERVICE_DEFINITIONS[slug]} for slug in service_slugs]

    can_manage_access = is_super_admin(request.user)

    if request.method == "POST" and can_manage_access:
        users = list(User.objects.all().order_by("username", "id"))
        for target_user in users:
            restrict_flag = request.POST.get(f"user_{target_user.id}_restrict") == "on"
            chosen = request.POST.getlist(f"user_{target_user.id}_services")
            allowed = sorted({slug for slug in chosen if slug in SERVICE_DEFINITIONS})

            if not restrict_flag:
                # Default allow: delete row to avoid accidental lock-outs.
                UserServiceAccess.objects.filter(user=target_user).delete()
                continue

            access, _created = UserServiceAccess.objects.get_or_create(user=target_user)
            access.restrict = True
            access.allowed_services = allowed
            access.save(update_fields=["restrict", "allowed_services", "updated_at"])

        messages.success(request, "User access settings saved.")
        return redirect("settings_portal")

    users = []
    access_rows = {}
    if can_manage_access:
        users = list(User.objects.all().order_by("username", "id"))
        access_rows = {
            row.user_id: row
            for row in UserServiceAccess.objects.select_related("user").all()
        }

    managed_users = []
    for target_user in users:
        access = access_rows.get(target_user.id)
        restricted = bool(access and access.restrict)
        allowed = list(access.allowed_services or []) if restricted else list(service_slugs)
        if restricted:
            from .access import expand_allowed_services

            allowed = expand_allowed_services(allowed)
            allowed = [slug for slug in allowed if slug in service_slugs]
        managed_users.append(
            {
                "user": target_user,
                "restricted": restricted,
                "allowed_services": allowed,
                "is_magicwand_admin": is_super_admin(target_user),
            }
        )

    return render(
        request,
        "magicwand/settings.html",
        {
            "settings_portal_url": settings_portal_url,
            "service_definitions": service_definitions,
            "managed_users": managed_users,
            "can_manage_access": can_manage_access,
        },
    )


def _cad_api(path: str) -> str:
    base = getattr(settings, "CAD_API_ROOT", "http://localhost:8300")
    return urljoin(base.rstrip('/') + "/", path.lstrip('/'))


def _cad_query_base_stem(value: Any) -> str:
    stem = Path(str(value or "")).stem.strip()
    if not stem:
        return ""
    return re.sub(r"^\d{10,}_", "", stem)


def _cad_extract_pdf_query_values(pdf_bytes: bytes, *, max_pages: int = 2, max_values: int = 80) -> list[str]:
    if not pdf_bytes or PdfReader is None:
        return []

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
    except Exception:
        return []

    extracted: list[str] = []
    seen: set[str] = set()
    for page in list(reader.pages)[:max_pages]:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if not page_text:
            continue
        for raw_line in page_text.splitlines():
            line = " ".join(raw_line.split()).strip()
            if len(line) < 3:
                continue
            if not re.search(r"\d", line):
                continue
            if line in seen:
                continue
            seen.add(line)
            extracted.append(line)
            if len(extracted) >= max_values:
                return extracted
    return extracted


def _cad_primary_code(value: Any) -> str:
    stem = _cad_query_base_stem(value)
    if not stem:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", stem.upper())


def _cad_relaxed_code(value: Any) -> str:
    primary = _cad_primary_code(value)
    if not primary:
        return ""
    return re.sub(r"^[A-Z]+", "", primary)


def _cad_numeric_code(value: Any) -> str:
    relaxed = _cad_relaxed_code(value)
    if not relaxed:
        return ""
    match = re.match(r"^(\d+)", relaxed)
    return match.group(1) if match else ""


def _cad_candidate_codes(*values: Any) -> set[str]:
    codes: set[str] = set()
    for value in values:
        for candidate in (
            _cad_primary_code(value),
            _cad_relaxed_code(value),
            _cad_numeric_code(value),
        ):
            if candidate:
                codes.add(candidate)
    return codes


def _cad_is_exact_query_match(*, query_values: Sequence[Any], candidate_values: Sequence[Any]) -> bool:
    candidate_codes = _cad_candidate_codes(*candidate_values)
    if not candidate_codes:
        return False

    for query in query_values:
        query_primary = _cad_primary_code(query)
        query_relaxed = _cad_relaxed_code(query)
        query_numeric = _cad_numeric_code(query)
        if query_primary and query_primary in candidate_codes:
            return True
        if query_relaxed and query_relaxed in candidate_codes:
            return True
        # Only fall back to numeric-only matching when the query itself has no
        # revision/suffix. This prevents AGR-1776 from being promoted when the
        # user actually uploaded AGR-1776 R1.
        if query_numeric and query_relaxed == query_numeric and query_numeric in candidate_codes:
            return True
    return False


def _promote_exact_cad_matches(match_results: Any, *, query_values: Sequence[Any]) -> Any:
    if not isinstance(match_results, dict):
        return match_results

    matches = match_results.get("matches")
    if not isinstance(matches, list):
        return match_results

    promoted = False
    for item in matches:
        if not isinstance(item, dict):
            continue
        drawing = item.get("drawing") if isinstance(item.get("drawing"), dict) else {}
        candidate_values = [
            drawing.get("id"),
            drawing.get("title"),
            drawing.get("storage_path"),
            item.get("shape_id"),
            item.get("shape_label"),
        ]
        if not _cad_is_exact_query_match(query_values=query_values, candidate_values=candidate_values):
            continue
        item["similarity"] = 1.0
        item["similarity_percent"] = 100.0
        item["verdict"] = "MATCH"
        promoted = True

    if promoted:
        matches.sort(
            key=lambda item: float(item.get("similarity", 0.0) or 0.0),
            reverse=True,
        )
        for idx, item in enumerate(matches):
            if isinstance(item, dict):
                item["rank"] = idx
    return match_results


def _cad_exact_match_fallback(
    drawings: Sequence[Dict[str, Any]],
    *,
    query_values: Sequence[Any],
    gasket_type: str = "",
) -> Dict[str, Any]:
    normalized_type = (gasket_type or "").strip().upper()
    matches: List[Dict[str, Any]] = []

    for idx, drawing in enumerate(drawings or []):
        if not isinstance(drawing, dict):
            continue
        drawing_type = str(drawing.get("gasket_type") or "").strip().upper()
        if normalized_type and drawing_type and drawing_type != normalized_type:
            continue
        candidate_values = [
            drawing.get("id"),
            drawing.get("title"),
            drawing.get("storage_path"),
            drawing.get("name"),
        ]
        if not _cad_is_exact_query_match(query_values=query_values, candidate_values=candidate_values):
            continue
        drawing_id = drawing.get("id") or drawing.get("title") or f"match-{idx}"
        matches.append(
            {
                "drawing": drawing,
                "similarity": 1.0,
                "similarity_percent": 100.0,
                "embedding_method": "exact_catalogue_match",
                "rank": idx,
                "shape_id": None,
                "shape_label": None,
                "shape_type": None,
                "bounds": None,
                "shape_viewer_url": None,
                "download_url": drawing.get("download_url") or f"/cad/download/{drawing_id}",
                "urn": drawing.get("urn"),
                "derivatives": drawing.get("derivatives") or [],
                "forge_status": drawing.get("forge_status"),
                "forge_error": drawing.get("forge_error"),
                "verdict": "MATCH",
            }
        )

    matches.sort(key=lambda item: str(item.get("drawing", {}).get("title") or item.get("drawing", {}).get("id") or ""))
    for rank, item in enumerate(matches):
        item["rank"] = rank
    return {"matches": matches}


def _ai_portal_normalize_context(raw_context: str) -> str:
    value = (raw_context or "").strip().lower().replace("-", "_")
    aliases = {
        "cad": "design",
        "cad_library": "design",
        "design_match": "design",
        "gmail_ai": "gmail",
        "discussion": "forum",
        "discussion_forum": "forum",
        "inquiries": "enquiries",
        "inquiry": "enquiries",
    }
    return aliases.get(value, value or "dashboard")


def _ai_portal_has_access(user, *services: str) -> bool:
    required = [service for service in services if service]
    if not required:
        return True
    return any(user_has_service_access(user, service) for service in required)


def _ai_portal_extract_title(message: str) -> Optional[str]:
    text = (message or "").strip()
    if not text:
        return None

    quoted = re.search(r"['\"]([^'\"]{3,120})['\"]", text)
    if quoted:
        return quoted.group(1).strip()

    patterns = [
        r"(?:called|named|title(?:d)? as|title(?:d)?|for project)\s+([A-Za-z0-9][A-Za-z0-9 ._/-]{2,120})",
        r"(?:create|open|start)\s+(?:an?\s+)?(?:enquiry|inquiry|sheet)\s+for\s+([A-Za-z0-9][A-Za-z0-9 ._/-]{2,120})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1).strip(" .,-")
        if candidate:
            return candidate
    return None


def _ai_portal_extract_gasket_type(message: str) -> str:
    match = re.search(r"\b(AGR|AGX|AGS|AGM)\b", (message or "").upper())
    return match.group(1) if match else ""


def _ai_portal_extract_gasket_id(message: str) -> str:
    match = re.search(r"\b((?:AGR|AGX|AGS|AGM)[\s_-]?\d+[A-Z0-9\s_-]*)\b", (message or "").upper())
    if not match:
        return ""
    return re.sub(r"[\s_]+", "-", match.group(1).strip())


def _ai_portal_infer_catalogue_metadata(uploaded_file: Any, *, message: str = "") -> tuple[str, str]:
    explicit_type = _ai_portal_extract_gasket_type(message)
    explicit_id = _ai_portal_extract_gasket_id(message)
    if explicit_type and explicit_id:
        return explicit_type, explicit_id

    stem = Path(getattr(uploaded_file, "name", "") or "").stem.upper().strip()
    stem = re.sub(r"[^A-Z0-9_-]+", "-", stem).strip("-")
    match = re.search(r"\b((?:AGR|AGX|AGS|AGM)[-_]?\d+[A-Z0-9_-]*)\b", stem)
    if match:
        gasket_id = match.group(1).replace("_", "-")
        gasket_type = explicit_type or gasket_id[:3]
        return gasket_type, explicit_id or gasket_id

    if explicit_type and explicit_id:
        return explicit_type, explicit_id
    return explicit_type, explicit_id


def _ai_portal_parse_history(raw_history: str) -> List[Dict[str, str]]:
    if not raw_history:
        return []
    try:
        decoded = json.loads(raw_history)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []

    history: List[Dict[str, str]] = []
    for item in decoded[-8:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        history.append({"role": role, "content": content[:1200]})
    return history


def _ai_portal_parse_json_payload(text: str) -> Dict[str, Any]:
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
    snippet = cleaned[start:end + 1]
    try:
        decoded = json.loads(snippet)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _ai_portal_action_catalog(user, context: str) -> List[Dict[str, str]]:
    ctx = _ai_portal_normalize_context(context)
    actions: List[Dict[str, str]] = []

    def add(action: str, label: str, description: str, *services: str) -> None:
        if services and not _ai_portal_has_access(user, *services):
            return
        actions.append(
            {
                "action": action,
                "label": label,
                "description": description,
            }
        )

    add("open_dashboard", "Open dashboard", "Go back to the main MagicWand dashboard.")
    add("open_enquiries", "Open enquiry board", "Open the enquiry board.", "enquiries")
    add("create_google_sheet_enquiry", "Create enquiry sheet", "Create a new enquiry and linked Google Sheet.", "enquiries")
    add("upload_pdf_enquiry", "Upload PDF RFQ", "Create a new enquiry from an attached PDF and generate a share link.", "enquiries")
    add("open_enquiry_summary", "Open enquiry summary", "Open the enquiry summary report.", "enquiries")
    add("open_enquiry_queue", "Open lead queue", "Open the Gmail lead queue for enquiries.", "enquiries")
    add("open_design", "Open design match", "Open the design intelligence workspace.", "design")
    add("design_match", "Match drawing to library", "Compare an attached customer PDF or CAD file against the design library.", "design")
    add("design_upload", "Upload drawing", "Upload an attached drawing into the design catalogue.", "design")
    add("open_design_library_files", "Browse design files", "Open the design library file browser.", "design")
    add("open_deskera", "Open Deskera", "Open the Deskera portal.", "deskera")
    add("open_deskera_workorders", "Monitor work orders", "Open the Deskera work orders view.", "deskera")
    add("open_deskera_jobcards", "View job cards", "Open the Deskera job cards view.", "deskera")
    add("open_deskera_kanban", "Open kanban board", "Open the Deskera MRP portal for kanban and related tools.", "deskera")
    add("open_mrp", "Open Manufacturing", "Open the manufacturing layer for work orders, job cards, integrations, and AI.", "mrp")
    add("open_compounding", "Open compounding planning", "Open the compounding planning board.", "compounding_planning", "compounding_board")
    add("open_compounding_display", "Open shop-floor board", "Open the compounding display board.", "compounding_board")
    add("open_compounding_iot", "Open IoT telemetry", "Open the compounding IoT telemetry dashboard.", "compounding_planning", "compounding_board")
    add("open_gmail", "Open Gmail AI", "Open the Gmail AI workspace.", "gmail_ai")
    add("open_forum", "Open discussion forum", "Open the discussion forum.", "discussion")
    add("open_accounts", "Open accounts workspace", "Open the accounts workspace.", "accounts")

    context_actions = {
        "enquiries": {"open_enquiries", "create_google_sheet_enquiry", "upload_pdf_enquiry", "open_enquiry_summary", "open_enquiry_queue"},
        "design": {"open_design", "design_match", "design_upload", "open_design_library_files"},
        "deskera": {"open_deskera", "open_deskera_workorders", "open_deskera_jobcards", "open_deskera_kanban"},
        "mrp": {"open_mrp"},
        "compounding": {"open_compounding", "open_compounding_display", "open_compounding_iot"},
        "gmail": {"open_gmail"},
        "forum": {"open_forum"},
        "accounts": {"open_accounts"},
    }

    if ctx == "dashboard":
        return actions
    preferred = context_actions.get(ctx)
    if not preferred:
        return actions
    primary = [item for item in actions if item["action"] in preferred]
    secondary = [item for item in actions if item["action"] not in preferred]
    return primary + secondary


def _ai_portal_is_small_talk(message: str) -> bool:
    cleaned = " ".join((message or "").lower().split())
    if not cleaned:
        return False
    if cleaned in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening", "yo"}:
        return True
    if cleaned.startswith(("hi ", "hello ", "hey ")):
        return True
    return False


def _ai_portal_is_capability_request(message: str) -> bool:
    cleaned = " ".join((message or "").lower().split())
    prompts = (
        "what can you do",
        "what all can you do",
        "help me",
        "help",
        "show actions",
        "available actions",
        "what actions",
        "how can you help",
    )
    return any(prompt in cleaned for prompt in prompts)


def _ai_portal_conversation_reply(context: str, message: str, actions: Sequence[Dict[str, str]]) -> str:
    highlighted = list(actions[:4])
    labels = ", ".join(item["label"].lower() for item in highlighted)

    if _ai_portal_is_small_talk(message):
        if labels:
            return f"I’m ready. In this workspace I can {labels}. Ask me naturally and I’ll either answer or run the matching action."
        return "I’m ready. Ask me what you want to do and I’ll help from here."

    if _ai_portal_is_capability_request(message):
        if not actions:
            return "This workspace does not have any actions available for your account."
        lines = [f"- {item['label']}: {item['description']}" for item in actions[:8]]
        return "Here’s what I can do in this workspace:\n" + "\n".join(lines)
    return ""


def _ai_portal_plan_with_ai(
    user,
    context: str,
    message: str,
    history: Sequence[Dict[str, str]],
    *,
    has_attachment: bool,
    available_actions: Sequence[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    if not message.strip():
        return None
    try:
        ai = AIReplyService()
    except AIServiceError:
        return None
    if getattr(ai, "_using_stub", False):
        return None

    action_lines = "\n".join(
        f"- {item['action']}: {item['label']} | {item['description']}"
        for item in available_actions
    )
    history_lines = "\n".join(
        f"{turn['role'].upper()}: {turn['content']}"
        for turn in list(history)[-6:]
    ) or "(none)"
    prompt = (
        "You are MagicWand's in-product copilot planner.\n"
        "Decide whether to continue the conversation or execute one app action.\n"
        "You must respond with ONLY valid JSON.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "kind": "chat" | "action",\n'
        '  "reply": "short conversational reply for the user",\n'
        '  "action": "one allowed action name or empty",\n'
        '  "title": "optional title",\n'
        '  "gasket_type": "optional design category",\n'
        '  "gasket_id": "optional design die number"\n'
        "}\n\n"
        "Rules:\n"
        "- If the user is greeting, asking what you can do, or clarifying information, return kind=chat.\n"
        "- Only choose an action from the allowed action list below.\n"
        "- Prefer action only when the user is clearly asking to do something in the product.\n"
        "- Do not invent APIs, routes, or actions.\n"
        "- If the request depends on an attachment, remember whether a file is currently attached.\n"
        "- Keep reply concise and product-focused.\n\n"
        f"Current workspace: {context}\n"
        f"Attachment available: {'yes' if has_attachment else 'no'}\n"
        f"Allowed actions:\n{action_lines}\n\n"
        f"Recent conversation:\n{history_lines}\n\n"
        f"Latest user message:\n{message.strip()}\n"
    )
    try:
        response_text = ai.generate_reply(AIReplyRequest(thread_summary=prompt, purpose="raw"))
    except AIServiceError:
        return None

    payload = _ai_portal_parse_json_payload(response_text)
    if not payload:
        return None

    kind = str(payload.get("kind") or "").strip().lower()
    action = str(payload.get("action") or "").strip()
    allowed_actions = {item["action"] for item in available_actions}
    if action and action not in allowed_actions:
        action = ""
    if kind not in {"chat", "action"}:
        kind = "action" if action else "chat"

    return {
        "kind": kind,
        "reply": str(payload.get("reply") or "").strip(),
        "action": action,
        "title": str(payload.get("title") or "").strip(),
        "gasket_type": str(payload.get("gasket_type") or "").strip().upper(),
        "gasket_id": str(payload.get("gasket_id") or "").strip().upper(),
    }


def _ai_portal_resolve_action(context: str, message: str, attachment: Any) -> Dict[str, Any]:
    ctx = _ai_portal_normalize_context(context)
    text = " ".join((message or "").lower().split())
    has_file = attachment is not None

    if not text and has_file:
        if ctx == "design":
            return {"action": "design_match"}
        if ctx == "enquiries":
            return {"action": "upload_pdf_enquiry"}
        raise ValueError("Describe what to do with the attached file.")

    if not text:
        raise ValueError("Type a request or attach a file.")

    if any(keyword in text for keyword in ("match", "compare")) and (
        any(keyword in text for keyword in ("design", "drawing", "cad", "pdf")) or ctx == "design"
    ):
        return {
            "action": "design_match",
            "gasket_type": _ai_portal_extract_gasket_type(message),
            "gasket_id": _ai_portal_extract_gasket_id(message),
            "title": _ai_portal_extract_title(message),
        }

    if any(keyword in text for keyword in ("upload", "attach")) and "pdf" in text and (
        any(keyword in text for keyword in ("enquiry", "inquiry", "rfq", "quote")) or ctx == "enquiries"
    ):
        return {
            "action": "upload_pdf_enquiry",
            "title": _ai_portal_extract_title(message),
        }

    if any(keyword in text for keyword in ("upload", "ingest", "catalog", "catalogue")) and (
        any(keyword in text for keyword in ("design", "drawing", "cad", "library")) or ctx == "design"
    ):
        return {"action": "design_upload"}

    if has_file:
        if any(keyword in text for keyword in ("match", "compare", "design", "drawing", "cad")) or ctx == "design":
            if any(keyword in text for keyword in ("upload to library", "upload drawing", "ingest", "catalog", "catalogue")):
                return {"action": "design_upload"}
            return {
                "action": "design_match",
                "gasket_type": _ai_portal_extract_gasket_type(message),
                "gasket_id": _ai_portal_extract_gasket_id(message),
                "title": _ai_portal_extract_title(message),
            }
        if any(keyword in text for keyword in ("enquiry", "inquiry", "rfq", "quote", "pdf")) or ctx == "enquiries":
            return {
                "action": "upload_pdf_enquiry",
                "title": _ai_portal_extract_title(message),
            }

    if "accounts" in text or any(keyword in text for keyword in ("ledger", "voucher", "journal entry", "tally", "finance")):
        return {"action": "open_accounts"}
    if any(keyword in text for keyword in ("forum", "discussion", "channel", "thread")):
        return {"action": "open_forum"}
    if "gmail" in text or any(keyword in text for keyword in ("mail", "email", "inbox")):
        return {"action": "open_gmail"}

    if "magicwand mrp" in text or "native mrp" in text or ctx == "mrp":
        return {"action": "open_mrp"}

    if "deskera" in text or "work order" in text or "job card" in text or "kanban" in text:
        if "job card" in text:
            return {"action": "open_deskera_jobcards"}
        if "kanban" in text:
            return {"action": "open_deskera_kanban"}
        if "work order" in text:
            return {"action": "open_deskera_workorders"}
        return {"action": "open_deskera"}

    if "compounding" in text or "shop-floor" in text or "shop floor" in text or "planning" in text:
        if "iot" in text or "telemetry" in text:
            return {"action": "open_compounding_iot"}
        if "display" in text or "board view" in text or "shop-floor" in text or "shop floor" in text:
            return {"action": "open_compounding_display"}
        return {"action": "open_compounding"}

    if "summary" in text and ("enquiry" in text or "inquiry" in text or ctx == "enquiries"):
        return {"action": "open_enquiry_summary"}
    if "queue" in text and ("lead" in text or "gmail" in text or ctx == "enquiries"):
        return {"action": "open_enquiry_queue"}
    if "board" in text and ("enquiry" in text or "inquiry" in text or ctx == "enquiries"):
        return {"action": "open_enquiries"}

    if any(keyword in text for keyword in ("design", "drawing", "cad", "library")) or ctx == "design":
        if "files" in text or "folder" in text:
            return {"action": "open_design_library_files"}
        return {"action": "open_design"}

    if any(keyword in text for keyword in ("enquiry", "inquiry", "rfq", "quote")) or ctx == "enquiries":
        if any(keyword in text for keyword in ("create", "new", "sheet", "start")):
            return {
                "action": "create_google_sheet_enquiry",
                "title": _ai_portal_extract_title(message),
            }
        return {"action": "open_enquiries"}

    if ctx == "gmail":
        return {"action": "open_gmail"}
    if ctx == "forum":
        return {"action": "open_forum"}
    if ctx == "accounts":
        return {"action": "open_accounts"}
    if ctx == "deskera":
        return {"action": "open_deskera"}
    if ctx == "mrp":
        return {"action": "open_mrp"}
    if ctx == "compounding":
        return {"action": "open_compounding"}

    return {"action": "open_dashboard"}


def _ai_portal_navigation(action: str) -> Optional[Dict[str, Any]]:
    routes: Dict[str, Dict[str, Any]] = {
        "open_dashboard": {"url": reverse("dashboard_hub"), "services": (), "message": "Opening Dashboard."},
        "open_enquiries": {"url": reverse("enquiries:board"), "services": ("enquiries",), "message": "Opening Enquiries."},
        "open_enquiry_summary": {"url": reverse("enquiries:summary"), "services": ("enquiries",), "message": "Opening the enquiry summary."},
        "open_enquiry_queue": {"url": reverse("enquiries:gmail_queue"), "services": ("enquiries",), "message": "Opening the lead queue."},
        "open_design": {"url": reverse("cad_library"), "services": ("design",), "message": "Opening Design Match."},
        "open_design_library_files": {"url": reverse("design_library_folder"), "services": ("design",), "message": "Opening library files."},
        "open_deskera": {"url": reverse("deskera_portal"), "services": ("deskera",), "message": "Opening Deskera."},
        "open_deskera_workorders": {"url": reverse("deskera_workorders"), "services": ("deskera",), "message": "Opening Deskera work orders."},
        "open_deskera_jobcards": {"url": reverse("deskera_jobcards"), "services": ("deskera",), "message": "Opening Deskera job cards."},
        "open_deskera_kanban": {"url": reverse("deskera_portal"), "services": ("deskera",), "message": "Opening Deskera."},
        "open_mrp": {"url": reverse("mrp:portal"), "services": ("mrp",), "message": "Opening Manufacturing."},
        "open_compounding": {"url": reverse("compounding:board"), "services": ("compounding_planning", "compounding_board"), "message": "Opening Compounding."},
        "open_compounding_display": {"url": reverse("compounding:display"), "services": ("compounding_board",), "message": "Opening the compounding board view."},
        "open_compounding_iot": {"url": reverse("compounding:iot_dashboard"), "services": ("compounding_planning", "compounding_board"), "message": "Opening IoT telemetry."},
        "open_gmail": {"url": reverse("gmail_ai"), "services": ("gmail_ai",), "message": "Opening Gmail AI."},
        "open_forum": {"url": reverse("discussion:board"), "services": ("discussion",), "message": "Opening the discussion forum."},
        "open_accounts": {"url": reverse("accounts_portal"), "services": ("accounts",), "message": "Opening Accounts."},
    }
    return routes.get(action)


def _coerce_dashboard_count(value: Any) -> Optional[int]:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return max(count, 0)


def _dashboard_design_catalogue_count() -> Optional[int]:
    try:
        response = requests.get(
            _cad_api("/cad/drawings/page"),
            params={"page": 1, "page_size": 1},
            timeout=getattr(settings, "CAD_API_TIMEOUT", 20),
        )
        response.raise_for_status()
        payload = response.json()
    except (RequestException, ValueError) as exc:
        logger.warning("Unable to load dashboard design count: %s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    return _coerce_dashboard_count(payload.get("total"))


def _dashboard_enquiries_total_count(request) -> Optional[int]:
    from enquiries import services as enquiry_services

    try:
        enquiries = enquiry_services.list_enquiries(getattr(settings, "ENQUIRY_API_ROOT", ""), request=request)
    except enquiry_services.EnquiryServiceError as exc:
        logger.warning("Unable to load dashboard enquiry count: %s", exc)
        return None
    except Exception as exc:
        logger.warning("Unexpected dashboard enquiry count failure: %s", exc)
        return None

    return _coerce_dashboard_count(len(enquiries or []))


def _dashboard_compounding_pending_count() -> int:
    plan = CompoundingPlan.objects.filter(plan_date=compounding_business_date()).first()
    if not plan:
        return 0

    pending_count = 0
    for line in plan.lines.all():
        planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
        planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
        actual_mb = int(getattr(line, "actual_mb", 0) or 0)
        actual_fb = int(getattr(line, "actual_fb", 0) or 0)
        sent_back_mb = int(getattr(line, "returned_to_planning_mb", 0) or 0)
        sent_back_fb = int(getattr(line, "returned_to_planning_fb", 0) or 0)
        remaining_mb = max(max(planned_mb - actual_mb, 0) - sent_back_mb, 0)
        remaining_fb = max(max(planned_fb - actual_fb, 0) - sent_back_fb, 0)

        if bool(getattr(line, "actuals_saved", False)) and remaining_mb <= 0 and remaining_fb <= 0:
            continue
        if planned_mb <= 0 and planned_fb <= 0 and actual_mb <= 0 and actual_fb <= 0 and remaining_mb <= 0 and remaining_fb <= 0:
            continue
        pending_count += 1

    return pending_count


@login_required
def dashboard_hub(request):
    """
    Dashboard hub that shows all available services.
    """
    design_count = None
    enquiry_count = None
    compounding_pending_count = 0

    if user_has_service_access(request.user, "design"):
        design_count = _dashboard_design_catalogue_count()
    if user_has_service_access(request.user, "enquiries"):
        enquiry_count = _dashboard_enquiries_total_count(request)
    if user_has_service_access(request.user, "compounding_planning") or user_has_service_access(request.user, "compounding_board"):
        compounding_pending_count = _dashboard_compounding_pending_count()

    context = {
        'user': request.user,
        'dashboard_design_count': design_count,
        'dashboard_enquiry_count': enquiry_count,
        'dashboard_compounding_pending_count': compounding_pending_count,
    }
    return render(request, 'magicwand/home.html', context)


@login_required
@require_http_methods(["GET"])
@require_service_access("gmail_ai")
def gmail_ai(request):
    return render(request, "magicwand/gmail_ai.html", {"user": request.user})


def _maybe_ai_to_gmail_query(message: str) -> Optional[str]:
    cleaned = (message or "").strip()
    if not cleaned:
        return None

    try:
        ai = AIReplyService()
    except AIServiceError:
        return None

    if getattr(ai, "_using_stub", False):
        return None

    prompt = (
        "Convert the user's request into a Gmail search query.\n"
        "Rules:\n"
        "- Output ONLY the Gmail query (no quotes, no explanation).\n"
        "- Prefer using operators like from:, subject:, has:attachment, after:, before:, newer_than:.\n"
        "- Do not include 'is:unread' unless explicitly requested.\n"
        "- Do not include 'in:inbox' unless explicitly requested.\n\n"
        f"User request:\n{cleaned}\n\n"
        "Gmail query:"
    )

    try:
        query = ai.generate_reply(AIReplyRequest(thread_summary=prompt, purpose="raw")).strip()
    except AIServiceError:
        return None

    query = query.strip().strip("`").strip()
    if not query:
        return None

    # Guard against accidental multi-line output.
    query = " ".join(query.splitlines()).strip()
    return query or None


@login_required
@require_http_methods(["POST"])
@require_service_access("gmail_ai", deny="json")
def gmail_ai_search(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "Message is required."}, status=400)

    query_used = _maybe_ai_to_gmail_query(message) or message

    gmail_service = GmailService(request.user)
    try:
        results = gmail_service.search_messages(query_used, limit=20)
    except GmailAPIError as exc:
        return JsonResponse(
            {
                "error": str(exc),
                "reauth_url": _get_google_reauth_url(),
            },
            status=502,
        )

    response_rows: List[Dict[str, Any]] = []
    for item in results:
        message_id = item.get("id")
        if not message_id:
            continue
        open_gmail_url = _gmail_web_message_url(request.user, message_id)
        response_rows.append(
            {
                "id": message_id,
                "sender": item.get("sender") or "",
                "subject": item.get("subject") or "",
                "snippet": item.get("snippet") or "",
                "date": item.get("date") or "",
                "url": open_gmail_url,
                "thread_url": reverse("gmail_ai_email", args=[message_id]),
                "follow_up_url": reverse("dashboard:compose_email", args=[message_id]),
            }
        )

    return JsonResponse({"query_used": query_used, "results": response_rows})


@login_required
@require_http_methods(["POST"])
def ai_portal_action(request):
    context = _ai_portal_normalize_context(request.POST.get("context") or "")
    message = (request.POST.get("message") or "").strip()
    attachment = request.FILES.get("attachment")
    history = _ai_portal_parse_history(request.POST.get("history") or "")
    selected_gasket_type = (request.POST.get("gasket_type") or "").strip().upper()
    selected_gasket_id = (request.POST.get("gasket_id") or "").strip().upper()
    selected_share_password = (request.POST.get("share_password") or "").strip()
    requested_top_k = (request.POST.get("top_k") or "").strip()
    try:
        design_match_top_k = int(requested_top_k or "3")
    except (TypeError, ValueError):
        design_match_top_k = 3
    design_match_top_k = max(1, min(design_match_top_k, 20))

    available_actions = _ai_portal_action_catalog(request.user, context)
    is_small_talk = _ai_portal_is_small_talk(message)
    is_capability_request = _ai_portal_is_capability_request(message)
    conversational_reply = _ai_portal_conversation_reply(context, message, available_actions)
    if conversational_reply and not attachment:
        return JsonResponse(
            {
                "ok": True,
                "action": "chat",
                "message": conversational_reply,
                "assistant_message": conversational_reply,
            }
        )

    ai_plan = _ai_portal_plan_with_ai(
        request.user,
        context,
        message,
        history,
        has_attachment=attachment is not None,
        available_actions=available_actions,
    )

    if ai_plan and ai_plan.get("kind") == "chat" and ai_plan.get("reply") and (is_small_talk or is_capability_request):
        return JsonResponse(
            {
                "ok": True,
                "action": "chat",
                "message": ai_plan["reply"],
                "assistant_message": ai_plan["reply"],
            }
        )

    try:
        if ai_plan and ai_plan.get("action"):
            resolved = {
                "action": ai_plan.get("action"),
                "title": ai_plan.get("title") or _ai_portal_extract_title(message),
                "gasket_type": ai_plan.get("gasket_type") or _ai_portal_extract_gasket_type(message),
                "gasket_id": ai_plan.get("gasket_id") or _ai_portal_extract_gasket_id(message),
            }
        else:
            resolved = _ai_portal_resolve_action(context, message, attachment)
    except ValueError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)

    if selected_gasket_type in PORTAL_CAD_GASKET_TYPES:
        resolved["gasket_type"] = selected_gasket_type
    if selected_gasket_id:
        normalized_selected_gasket_id = re.sub(r"[\s_]+", "-", selected_gasket_id).strip("-")
        if (
            selected_gasket_type in PORTAL_CAD_GASKET_TYPES
            and normalized_selected_gasket_id
            and not re.match(r"^(AGR|AGX|AGS|AGM)\b", normalized_selected_gasket_id)
        ):
            normalized_selected_gasket_id = f"{selected_gasket_type}-{normalized_selected_gasket_id}"
        resolved["gasket_id"] = normalized_selected_gasket_id

    action = resolved.get("action") or "open_dashboard"
    navigation = _ai_portal_navigation(action)
    if navigation is not None:
        services_required = tuple(navigation.get("services") or ())
        if not _ai_portal_has_access(request.user, *services_required):
            return JsonResponse(
                {"ok": False, "message": "You do not have access to that module."},
                status=403,
            )
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "message": navigation.get("message"),
                "assistant_message": (ai_plan or {}).get("reply") or navigation.get("message"),
                "navigate_url": navigation.get("url"),
                "auto_navigate": True,
            }
        )

    from enquiries import services as enquiry_services
    from enquiries.views import (
        _collect_share_values_from_enquiry,
        _create_google_sheet_enquiry,
        _create_pdf_enquiry,
        _create_share_link_from_values,
        _get_api_root,
    )

    if action == "create_google_sheet_enquiry":
        if not _ai_portal_has_access(request.user, "enquiries"):
            return JsonResponse({"ok": False, "message": "You do not have access to Enquiries."}, status=403)

        title = resolved.get("title") or f"AI Enquiry {timezone.localtime().strftime('%d %b %H:%M')}"
        try:
            result = _create_google_sheet_enquiry(request, sheet_title=title)
        except GmailAPIError as exc:
            return JsonResponse(
                {
                    "ok": False,
                    "message": str(exc),
                    "reauth_url": _get_google_reauth_url(),
                },
                status=400,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)
        except enquiry_services.EnquiryServiceError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)
        except RuntimeError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=502)

        links = [
            {"label": "Open Enquiries", "url": reverse("enquiries:board"), "style": "secondary"},
        ]
        if result.get("enquiry_detail_url"):
            links.insert(0, {"label": "Open Enquiry", "url": result["enquiry_detail_url"], "style": "primary"})
        if result.get("sheet_url"):
            links.append({"label": "Open Google Sheet", "url": result["sheet_url"], "style": "primary", "external": True})
        enquiry_id = result.get("enquiry_id")
        suffix = f" {enquiry_id}" if enquiry_id else ""
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "message": f"Created enquiry{suffix} and linked Google Sheet.",
                "assistant_message": (ai_plan or {}).get("reply") or f"I created enquiry{suffix} and linked the Google Sheet.",
                "links": links,
                "result": result,
            }
        )

    if action == "upload_pdf_enquiry":
        if not _ai_portal_has_access(request.user, "enquiries"):
            return JsonResponse({"ok": False, "message": "You do not have access to Enquiries."}, status=403)
        if attachment is None:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Attach a PDF to create an enquiry from it.",
                    "needs_file": True,
                    "accepted_types": ".pdf",
                },
                status=400,
            )
        if not selected_share_password:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Enter a share password to create the enquiry link.",
                    "accepted_types": ".pdf",
                },
                status=400,
            )
        pdf_snapshot = b""
        pdf_filename = getattr(attachment, "name", "") or "enquiry.pdf"
        pdf_content_type = getattr(attachment, "content_type", "") or "application/pdf"
        try:
            attachment.seek(0)
        except Exception:
            pass
        try:
            pdf_snapshot = attachment.read()
        except Exception:
            pdf_snapshot = b""
        try:
            attachment.seek(0)
        except Exception:
            pass
        if not pdf_snapshot:
            return JsonResponse({"ok": False, "message": "Attach a valid PDF to create the enquiry link."}, status=400)
        try:
            result = _create_pdf_enquiry(
                request,
                title=resolved.get("title"),
                pdf_file=attachment,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)
        except enquiry_services.EnquiryServiceError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)

        share_values: Dict[str, Any] = {}
        enquiry_id = str(result.get("enquiry_id") or "").strip()
        if enquiry_id:
            try:
                created_enquiry = enquiry_services.fetch_enquiry(_get_api_root(), enquiry_id, request=request)
            except enquiry_services.EnquiryServiceError:
                created_enquiry = {}
            share_values = _collect_share_values_from_enquiry(created_enquiry)
        if not share_values:
            resolved_title = result.get("title") or pdf_filename.rsplit(".", 1)[0] or "PDF Enquiry"
            share_values = {
                "subject": resolved_title,
                "customer_name": resolved_title,
                "project_name": resolved_title,
                "description": f"Uploaded from PDF {pdf_filename}",
                "status": "open",
                "priority": "medium",
                "channel": "email",
                "customer_reference": resolved_title,
            }
        try:
            share_result = _create_share_link_from_values(
                request,
                share_values,
                share_password=selected_share_password,
                enquiry_id=enquiry_id,
                pdf_bytes=pdf_snapshot,
                pdf_filename=pdf_filename,
                pdf_content_type=pdf_content_type,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)

        links = [
            {"label": "Open Enquiries", "url": reverse("enquiries:board"), "style": "secondary"},
            {"label": "Open Share Link", "url": share_result["share_url"], "style": "primary", "external": True},
        ]
        if result.get("enquiry_detail_url"):
            links.insert(0, {"label": "Open Enquiry", "url": result["enquiry_detail_url"], "style": "primary"})
        result["share_url"] = share_result["share_url"]
        result["share_password"] = share_result["share_password"]
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "message": "Created a new enquiry from the uploaded PDF and generated a share link.",
                "assistant_message": (ai_plan or {}).get("reply") or "I created the enquiry from the PDF and generated the share link.",
                "links": links,
                "result": result,
            }
        )

    if action == "design_match":
        if not _ai_portal_has_access(request.user, "design"):
            return JsonResponse({"ok": False, "message": "You do not have access to Design Match."}, status=403)
        if attachment is None:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Attach a PDF, DWG, or DXF file to run a design match.",
                    "needs_file": True,
                    "accepted_types": ".pdf,.dwg,.dxf",
                },
                status=400,
            )
        if not (resolved.get("gasket_type") or "").strip().upper():
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Select the category to match from.",
                    "accepted_types": ".pdf",
                },
                status=400,
            )
        try:
            result = _cad_match_uploaded_file(
                attachment,
                top_k=str(design_match_top_k),
                gasket_type=resolved.get("gasket_type") or "",
                match_gasket_id=resolved.get("gasket_id") or "",
                match_gasket_label=resolved.get("title") or "",
            )
        except RequestException as exc:
            return JsonResponse({"ok": False, "message": f"CAD match failed: {exc}"}, status=502)

        cad_api_root = (getattr(settings, "CAD_API_ROOT", "") or "").rstrip("/")
        matches = []
        for item in list(result.get("matches") or [])[:design_match_top_k]:
            if not isinstance(item, dict):
                continue
            drawing = item.get("drawing") if isinstance(item.get("drawing"), dict) else {}
            preview_url = drawing.get("preview_url") or ""
            download_url = item.get("download_url") or ""
            if cad_api_root:
                if preview_url and not str(preview_url).startswith(("http://", "https://")):
                    preview_url = f"{cad_api_root}{preview_url if str(preview_url).startswith('/') else '/' + str(preview_url)}"
                if download_url and not str(download_url).startswith(("http://", "https://")):
                    download_url = f"{cad_api_root}{download_url if str(download_url).startswith('/') else '/' + str(download_url)}"
            matches.append(
                {
                    "title": drawing.get("title") or drawing.get("id") or "Unknown",
                    "drawing_id": drawing.get("id") or "",
                    "project": drawing.get("project") or "",
                    "verdict": item.get("verdict") or "",
                    "similarity_percent": item.get("similarity_percent"),
                    "preview_url": preview_url,
                    "download_url": download_url,
                }
            )

        top_match = matches[0] if matches else None
        selected_category = (resolved.get("gasket_type") or "").strip().upper()
        message_text = "Design match completed."
        if top_match:
            confidence = top_match.get("similarity_percent")
            confidence_label = f" at {confidence:.0f}%" if isinstance(confidence, (int, float)) else ""
            message_text = f"Top match: {top_match['title']}{confidence_label}."
        elif selected_category:
            message_text = f"No matching drawings found in category {selected_category}."
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "message": message_text,
                "assistant_message": (ai_plan or {}).get("reply") or message_text,
                "selected_category": selected_category,
                "selected_match_count": design_match_top_k,
                "links": [
                    {"label": "Open Design Match", "url": reverse("cad_library"), "style": "secondary"},
                ],
                "matches": matches,
            }
        )

    if action == "design_upload":
        if not _ai_portal_has_access(request.user, "design"):
            return JsonResponse({"ok": False, "message": "You do not have access to Design Match."}, status=403)
        if attachment is None:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Attach a drawing file to upload it into the design library.",
                    "needs_file": True,
                    "accepted_types": ".pdf,.dwg,.dxf",
                },
                status=400,
            )
        inferred_gasket_type, inferred_gasket_id = _ai_portal_infer_catalogue_metadata(attachment, message=message)
        gasket_type = (resolved.get("gasket_type") or inferred_gasket_type or "").strip().upper()
        gasket_id = (resolved.get("gasket_id") or inferred_gasket_id or "").strip().upper()
        if not gasket_type:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Select the category to upload this drawing into.",
                    "accepted_types": ".pdf,.dwg,.dxf",
                },
                status=400,
            )
        if not gasket_id:
            return JsonResponse(
                {
                    "ok": False,
                    "message": "Enter the die number like AGR-1725, or upload a file named that way.",
                },
                status=400,
            )
        try:
            uploaded = _cad_upload_catalogue_file(
                attachment,
                gasket_id=gasket_id,
                gasket_type=gasket_type,
                display_name=resolved.get("title") or "",
            )
        except RequestException as exc:
            return JsonResponse({"ok": False, "message": f"CAD upload failed: {exc}"}, status=502)

        descriptor_name = uploaded.get("name") or getattr(attachment, "name", "drawing")
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "message": f"Uploaded {descriptor_name} to the design library.",
                "assistant_message": (ai_plan or {}).get("reply") or f"I uploaded {descriptor_name} into the design library.",
                "links": [
                    {"label": "Open Design Match", "url": reverse("cad_library"), "style": "primary"},
                    {"label": "Browse Library Files", "url": reverse("design_library_folder"), "style": "secondary"},
                ],
                "result": {
                    "name": descriptor_name,
                    "gasket_type": gasket_type,
                    "gasket_id": gasket_id,
                },
            }
        )

    return JsonResponse({"ok": False, "message": "I could not map that request to an action."}, status=400)


@login_required
@require_http_methods(["GET"])
@require_service_access("gmail_ai")
def gmail_ai_email(request, message_id: str):
    gmail_service = GmailService(request.user)
    try:
        message = gmail_service.get_message(message_id)
    except GmailAPIError as exc:
        messages.error(request, str(exc))
        return redirect("gmail_ai")

    thread_id = message.get("thread_id") or ""
    try:
        thread_messages = gmail_service.get_thread_messages(thread_id, limit=20) if thread_id else [message]
    except GmailAPIError:
        thread_messages = [message]

    conversation: List[Dict[str, Any]] = []
    for msg in thread_messages or [message]:
        conversation.append(
            {
                "id": msg.get("id"),
                "sender": msg.get("sender"),
                "to": msg.get("to"),
                "date": msg.get("parsed_date") or msg.get("date"),
                "subject": msg.get("subject"),
                "body": msg.get("body_text") or "",
            }
        )

    context = {
        "message_id": message_id,
        "subject": message.get("subject") or "",
        "conversation": conversation,
        "gmail_url": _gmail_web_message_url(request.user, message_id),
        "follow_up_url": reverse("dashboard:compose_email", args=[message_id]),
    }
    return render(request, "magicwand/gmail_email.html", context)


@login_required
@require_service_access("deskera")
def deskera_portal(request):
    """
    Launchpad with quick links into key Deskera MRP/Accounting modules.
    """
    configured_base = _deskera_portal_base()
    client = _build_deskera_client()

    context = {
        'links': [],
        'portal_base': configured_base,
        'internal_tools': [],
        'jobcards_status_endpoint': reverse('deskera_jobcards_copilot_status'),
        'workorders_status_endpoint': reverse('deskera_workorders_copilot_status'),
        'jobcards_chat_endpoint': reverse('deskera_jobcards_chat'),
        'workorders_chat_endpoint': reverse('deskera_workorders_chat'),
        'jobcards_copilot_status': 'unknown',
        'workorders_copilot_status': 'unknown',
    }

    if not configured_base:
        context['config_error'] = (
            "Deskera portal URL is not configured. Set DESKERA_PORTAL_BASE_URL "
            "or DESKERA_MRP_BASE_URL in your environment to enable these links."
        )
    else:
        base = configured_base.rstrip('/') + '/'
        for item in DESKERA_PORTAL_LINKS:
            context['links'].append({
                **item,
                'url': urljoin(base, item['path'].lstrip('/')),
            })

    context['internal_tools'].append({
        "label": "Work Orders (MagicWand)",
        "description": "Native list view powered by Deskera's API with search & filters. Use the MTC button on any row to download an editable DOCX draft.",
        "url": reverse('deskera_workorders'),
        "icon": "bi-list-check",
    })
    context['internal_tools'].append({
        "label": "MTC Generator (MagicWand)",
        "description": "Generate an editable Material Test Certificate (DOCX) draft from a Work Order code.",
        "url": reverse('deskera_mtc_generator'),
        "icon": "bi-file-earmark-text",
    })
    context['internal_tools'].append({
        "label": "Job Cards (MagicWand)",
        "description": "Review job cards with Copilot summaries.",
        "url": reverse('deskera_jobcards'),
        "icon": "bi-clipboard-data",
    })

    if client.is_configured:
        context['jobcards_copilot_status'] = _ensure_jobcard_snapshot_refresh(client, portal_base=configured_base)
        context['workorders_copilot_status'] = _ensure_workorder_snapshot_refresh(client, portal_base=configured_base)

    return render(request, 'magicwand/deskera_portal.html', context)


@login_required
@require_service_access("deskera")
def deskera_kanban(request):
    """
    Fetch Kanban data from Deskera MRP and render a lightweight board.
    """
    client = DeskeraMRPClient(
        base_url=getattr(settings, 'DESKERA_MRP_BASE_URL', ''),
        api_key=getattr(settings, 'DESKERA_MRP_API_KEY', ''),
        tenant=getattr(settings, 'DESKERA_MRP_TENANT', ''),
        kanban_path=getattr(settings, 'DESKERA_MRP_KANBAN_PATH', '/mrp/api/v1/kanban-board'),
        default_board_id=getattr(settings, 'DESKERA_MRP_BOARD_ID', ''),
        timeout=getattr(settings, 'DESKERA_MRP_TIMEOUT', 15),
    )

    requested_board_id = (request.GET.get('board_id') or request.GET.get('boardId') or '').strip()
    context = {
        'board_id': requested_board_id or client.default_board_id,
        'lanes': [],
        'source_url': '',
    }

    if not client.is_configured:
        # Fall back to the legacy redirect if the Deskera config is missing but a portal URL exists.
        if getattr(settings, 'KANBAN_PORTAL_URL', ''):
            return service_redirect(request, 'KANBAN_PORTAL_URL', 'Kanban Board')
        context['config_error'] = (
            "Deskera MRP is not configured yet. Set DESKERA_MRP_BASE_URL (and optionally "
            "DESKERA_MRP_API_KEY) in your environment to enable the integration."
        )
        return render(request, 'magicwand/deskera_kanban.html', context, status=502)

    try:
        board_payload = client.fetch_kanban_board(board_id=requested_board_id)
        context['lanes'] = board_payload.get('lanes', [])
        context['source_url'] = board_payload.get('source_url', '')
        context['board_id'] = board_payload.get('board_id') or context['board_id']
        context['refreshed_at'] = timezone.now()
    except DeskeraMRPConfigError as exc:
        context['config_error'] = str(exc)
        return render(request, 'magicwand/deskera_kanban.html', context, status=502)
    except DeskeraMRPError as exc:
        context['fetch_error'] = str(exc)
        return render(request, 'magicwand/deskera_kanban.html', context, status=502)

    return render(request, 'magicwand/deskera_kanban.html', context)


SORT_FIELD_MAP = {
    "work_order": "workOrderCode",
    "product": "productName",
    "customer": "customerName",
    "quantity": "quantity",
    "status": "status",
    "due_date": "promiseDate",
    "created": "createdOn",
}
DEFAULT_SORT_KEY = "created"
DEFAULT_SORT_DIR = "desc"

WORKSTATION_SORT_FIELD_MAP = {
    "name": "name",
    "work_center": "workCenter",
    "resource": "resourceName",
    "status": "status",
    "capacity": "capacity",
    "shift": "shift",
}
WORKSTATION_DEFAULT_SORT_KEY = "name"

JOBCARD_SORT_FIELD_MAP = {
    "job_card": "jobCardCode",
    "work_order": "workOrderSeqCode",
    "sales_order": "salesOrderSequenceCode",
    "product": "productName",
    "station": "workStationName",
    "status": "status",
    "due_date": "dueDate",
}
JOBCARD_DEFAULT_SORT_KEY = "job_card"


@login_required
@require_service_access("deskera")
def deskera_workorders(request):
    """
    Render a lightweight Work Order list sourced from Deskera's API.
    """
    snapshot_client = _build_deskera_client()
    ui_client = _build_deskera_client()
    try:
        ui_timeout = int(getattr(settings, "DESKERA_MRP_UI_TIMEOUT", 15))
    except (TypeError, ValueError):
        ui_timeout = 15
    ui_client.timeout = max(5, min(ui_timeout, 60))

    columns_mode = (request.GET.get("columns") or "compact").strip().lower()
    if columns_mode not in {"compact", "all"}:
        columns_mode = "compact"

    search = (request.GET.get('search') or '').strip()
    status = (request.GET.get('status') or '').strip()
    sort_key = (request.GET.get('sort') or DEFAULT_SORT_KEY).lower()
    if sort_key not in SORT_FIELD_MAP:
        sort_key = DEFAULT_SORT_KEY
    direction_param = (request.GET.get('dir') or DEFAULT_SORT_DIR).lower()
    sort_dir = 'ASC' if direction_param == 'asc' else 'DESC'
    sort_field = SORT_FIELD_MAP[sort_key]

    try:
        page_value = request.GET.get('page')
        if page_value in (None, ''):
            page = 0
        else:
            page = max(0, int(page_value))
    except (TypeError, ValueError):
        page = 0

    ui_page_size = getattr(settings, 'DESKERA_WORKORDER_UI_PAGE_SIZE', 50)
    try:
        ui_limit = max(1, min(int(ui_page_size), 100))
    except (TypeError, ValueError):
        ui_limit = 50

    attachments_param = (request.GET.get("attachments") or "").strip().lower()
    if attachments_param in {"1", "true", "yes", "on"}:
        fetch_attachment_details = True
    elif attachments_param in {"0", "false", "no", "off"}:
        fetch_attachment_details = False
    else:
        fetch_attachment_details = columns_mode == "all"
    live_fetch = (request.GET.get("live") or "").strip().lower() in {"1", "true", "yes", "on"}
    force_refresh = (request.GET.get("refresh") or "").strip().lower() in {"1", "true", "yes", "on"}

    portal_base = _deskera_portal_base()
    warning: Optional[str] = None
    workorders: List[Dict[str, Any]] = []
    meta_summary: Dict[str, Any] = {"page": page, "limit": ui_limit, "total": 0, "has_more": False}
    error: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None

    snapshot_key = _workorder_snapshot_cache_key(snapshot_client)
    snapshot_error_key = _workorder_snapshot_error_key(snapshot_client)
    snapshot = _deskera_cache_get(snapshot_key)
    last_snapshot_error = _deskera_cache_get(snapshot_error_key)
    last_snapshot_error_message = _deskera_snapshot_error_message(last_snapshot_error)
    if _deskera_snapshot_has_records(snapshot) and not live_fetch:
        workorders, meta_summary = _deskera_snapshot_page(
            snapshot,
            search=search,
            status=status,
            page=page,
            limit=ui_limit,
        )
        for record in workorders:
            identifier = record.get("identifier")
            if portal_base and identifier and not record.get("portal_url"):
                record["portal_url"] = f"{portal_base}/mrp/workorder/{identifier}"
        if _deskera_snapshot_error_is_recent(last_snapshot_error):
            warning = _deskera_user_facing_warning(last_snapshot_error_message or "Deskera MRP is temporarily unavailable.")
        elif _workorder_snapshot_is_stale(snapshot):
            warning = "Showing the cached Deskera work order snapshot while refreshing in the background."
        raw_payload = None
    elif live_fetch:
        workorders, meta_summary, error, raw_payload = _fetch_normalized_workorders(
            ui_client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=page,
            limit=ui_limit,
            sort_field=sort_field,
            sort_dir=sort_dir,
            fetch_attachment_details=fetch_attachment_details,
        )
    else:
        warning = (
            "Deskera work orders are syncing in the background. "
            "This page opened without waiting for Deskera; it will refresh when the first snapshot is ready."
        )

    sort_state = _build_sort_state(request, current_sort=sort_key, current_dir=direction_param)

    all_columns: List[Dict[str, str]] = []
    if error:
        _ensure_workorder_snapshot_refresh(snapshot_client, portal_base=portal_base)
        snapshot = _deskera_cache_get(snapshot_key)
        if _deskera_snapshot_has_records(snapshot):
            paged_records, snapshot_meta = _deskera_snapshot_page(
                snapshot,
                search=search,
                status=status,
                page=page,
                limit=ui_limit,
            )

            for record in paged_records:
                identifier = record.get("identifier")
                if portal_base and identifier and not record.get("portal_url"):
                    record["portal_url"] = f"{portal_base}/mrp/workorder/{identifier}"

            workorders = paged_records
            meta_summary = snapshot_meta
            warning = _deskera_user_facing_warning(error, has_cached_snapshot=True)
            error = None
            raw_payload = None
        else:
            error = _deskera_user_facing_warning(error, has_cached_snapshot=False)

    if not error and workorders:
        column_keys = _deskera_workorder_columns(workorders, mode=columns_mode)
        all_columns = [{"key": key, "label": _humanize_deskera_column(key)} for key in column_keys]
        for order in workorders:
            raw = order.get("raw")
            if not isinstance(raw, dict):
                raw = {}
            order["all_cells"] = [
                {
                    "key": key,
                    "value": _deskera_format_column_value(
                        ui_client,
                        key,
                        raw.get(key) if key in raw else order.get(key),
                    ),
                }
                for key in column_keys
            ]
            order["all_values"] = [cell["value"] for cell in order["all_cells"]]

    context = {
        'workorders': workorders,
        'search': search,
        'status': status,
        'meta': meta_summary,
        'warning': warning,
        'error': error,
        'portal_base': portal_base,
        'sort_state': sort_state,
        'raw_response': (raw_payload if not error else None),
        'copilot_status_endpoint': reverse('deskera_workorders_copilot_status'),
        'summary_endpoint': reverse('deskera_workorders_summary'),
        'chat_endpoint': reverse('deskera_workorders_chat'),
        'all_columns': all_columns,
        'columns_mode': columns_mode,
        'attachments_mode': fetch_attachment_details,
    }

    params = request.GET.copy()
    params['page'] = str(max(page - 1, 0))
    prev_query = params.urlencode()
    prev_url = f"{request.path}?{prev_query}" if page > 0 and prev_query else (request.path if page > 0 else None)

    params = request.GET.copy()
    params['page'] = str(page + 1)
    next_query = params.urlencode()
    next_url = (
        f"{request.path}?{next_query}"
        if meta_summary.get('has_more') and next_query
        else (request.path if meta_summary.get('has_more') else None)
    )

    total_records = meta_summary.get('total')
    total_pages = None
    if total_records is not None:
        try:
            total_pages = max(1, (int(total_records) + ui_limit - 1) // ui_limit)
        except (TypeError, ValueError):
            total_pages = None

    copilot_status = _ensure_workorder_snapshot_refresh(snapshot_client, portal_base=portal_base, force=force_refresh)
    snapshot = _deskera_cache_get(snapshot_key)
    context['pagination'] = {
        'page': page,
        'page_size': ui_limit,
        'prev_url': prev_url,
        'next_url': next_url,
        'total_pages': total_pages,
    }
    context['copilot'] = {
        'status': copilot_status,
        'fetched_at_ts': (snapshot.get('fetched_at_ts') if isinstance(snapshot, dict) else None),
        'records': (snapshot.get('meta', {}).get('records') if isinstance(snapshot, dict) else None),
    }
    context['reload_when_ready'] = bool(
        not workorders
        and not search
        and not status
        and copilot_status == "refreshing"
    )

    toggle_params = request.GET.copy()
    toggle_params['page'] = str(page)
    toggle_params['columns'] = 'compact'
    compact_query = toggle_params.urlencode()
    context['columns_compact_url'] = f"{request.path}?{compact_query}" if compact_query else request.path

    toggle_params['columns'] = 'all'
    all_query = toggle_params.urlencode()
    context['columns_all_url'] = f"{request.path}?{all_query}" if all_query else request.path

    return render(request, 'magicwand/deskera_workorders.html', context)


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera", deny="json")
def deskera_workorders_copilot_status(request):
    """
    Lightweight status endpoint for the work order snapshot used by Deskera Copilot.
    """
    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    if not client.is_configured:
        return JsonResponse(
            {
                "status": "error",
                "error": (
                    "Deskera MRP is not configured. Set DESKERA_MRP_BASE_URL (and optionally DESKERA_MRP_API_KEY)."
                ),
            },
            status=502,
        )

    snapshot, snapshot_error, snapshot_status = _get_or_build_workorder_snapshot(client, portal_base=portal_base)
    lock_present = _deskera_refresh_lock_present(
        _workorder_snapshot_lock_key(client),
        max_age_seconds=_workorder_snapshot_lock_seconds(),
    )
    last_error = _deskera_cache_get(_workorder_snapshot_error_key(client))

    records_count: Optional[int] = None
    fetched_at_ts: Optional[float] = None
    meta: Dict[str, Any] = {}
    if isinstance(snapshot, dict):
        fetched_at_ts_value = snapshot.get("fetched_at_ts")
        if isinstance(fetched_at_ts_value, (int, float)):
            fetched_at_ts = float(fetched_at_ts_value)
        meta = dict(snapshot.get("meta") or {})
        records_value = meta.get("records")
        if isinstance(records_value, int):
            records_count = records_value
        else:
            records_count = len(snapshot.get("records") or [])

    snapshot_usable = _deskera_snapshot_has_records(snapshot)
    error_message = None
    for candidate in (snapshot_error, last_error):
        candidate_message = _deskera_snapshot_error_message(candidate)
        if candidate_message:
            error_message = candidate_message
            break

    status_value = snapshot_status
    warning_message = None
    if error_message and snapshot_usable:
        status_value = "ready"
        warning_message = error_message
    elif error_message and not lock_present:
        status_value = "error"
        error_message = _deskera_user_facing_warning(error_message, has_cached_snapshot=False)

    return JsonResponse(
        {
            "status": status_value,
            "lock_present": lock_present,
            "cached": bool(snapshot_usable and warning_message),
            "records": records_count,
            "fetched_at_ts": fetched_at_ts,
            "meta": {
                "building": meta.get("building"),
                "partial": meta.get("partial"),
                "has_more": meta.get("has_more"),
                "fetched_pages": meta.get("fetched_pages"),
                "total": meta.get("total"),
            }
            if meta
            else None,
            "error": (error_message if status_value == "error" else None),
            "warning": warning_message,
        }
    )


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera")
def deskera_workorder_mtc(request):
    work_order_code = (
        (request.GET.get("work_order") or request.GET.get("workOrder") or request.GET.get("code") or "")
        .strip()
    )
    if not work_order_code:
        return HttpResponse("Missing `work_order` query parameter.", status=400, content_type="text/plain")

    template_override = (request.GET.get("template") or "").strip()
    compound_override = (request.GET.get("compound") or "").strip()
    folder_override = (request.GET.get("folder") or "").strip()
    auto_pick = (request.GET.get("auto") or "").strip().lower() in {"1", "true", "yes"}

    client = _build_deskera_client()
    portal_base = _deskera_portal_base()

    try:
        record = _fetch_workorder_record_by_identifier(client, work_order_code)
    except DeskeraMRPConfigError as exc:
        return HttpResponse(str(exc), status=500, content_type="text/plain")
    except DeskeraMRPError as exc:
        return HttpResponse(str(exc), status=502, content_type="text/plain")

    if not record:
        return HttpResponse(f"Work order not found: {work_order_code}", status=404, content_type="text/plain")

    normalized = _normalize_workorder_record(record, client=client, portal_base=portal_base)
    compound = compound_override or str(normalized.get("compoundCode") or record.get("productName") or normalized.get("product") or "").strip()
    customer = str(normalized.get("customer") or "")
    product = str(normalized.get("product") or "")

    try:
        if not template_override and not auto_pick:
            try:
                folders = list_mtc_template_folders()
                prefix = str(getattr(settings, "MTC_TEMPLATES_PREFIX", "") or "").strip().lstrip("/")
                prefix = f"{prefix}/" if prefix and not prefix.endswith("/") else prefix

                def display_label(folder_prefix: str) -> str:
                    label = folder_prefix or ""
                    if prefix and label.startswith(prefix):
                        label = label[len(prefix):]
                    return label.rstrip("/") or folder_prefix

                def match_key(value: str) -> str:
                    return re.sub(r"[^A-Z0-9]+", "", (value or "").upper())

                compound_key = match_key(compound)
                alt_key = compound_key[2:] if compound_key.startswith("AG") else ""

                selected_folder = folder_override
                if not selected_folder and compound_key:
                    scored: list[tuple[int, int, str]] = []
                    for folder in folders:
                        key = match_key(display_label(folder))
                        score = 0
                        if compound_key and compound_key in key:
                            score = 2
                        elif alt_key and alt_key in key:
                            score = 1
                        if score:
                            scored.append((score, -len(folder), folder))
                    scored.sort(reverse=True)
                    if scored:
                        selected_folder = scored[0][2]

                templates = list_mtc_templates(folder_prefix=selected_folder) if selected_folder else ()
                template_options = []
                for name in templates:
                    label = name
                    if selected_folder and name.startswith(selected_folder):
                        label = name[len(selected_folder):]
                    template_options.append({"object_name": name, "label": label})

                folder_options = [{"value": folder, "label": display_label(folder)} for folder in folders]

                return render(
                    request,
                    "magicwand/deskera_mtc_select.html",
                    {
                        "work_order": work_order_code,
                        "compound": compound,
                        "customer": customer,
                        "product": product,
                        "folder": selected_folder,
                        "template": template_override,
                        "folders": folder_options,
                        "templates": template_options,
                        "bucket": getattr(settings, "MTC_TEMPLATES_BUCKET", ""),
                        "prefix": prefix,
                        "local_dir": getattr(settings, "MTC_TEMPLATES_LOCAL_DIR", ""),
                    },
                )
            except MTCError as exc:
                return HttpResponse(str(exc), status=500, content_type="text/plain")

        resolution = resolve_mtc_template(
            compound=compound,
            template_override=template_override,
            customer=customer,
            product=product,
        )
        template_bytes = load_mtc_template_bytes(resolution)
        certificate_numbers = _alloc_mtc_certificate_numbers()
        mtc_deskera_context = _build_mtc_deskera_context(
            client=client,
            record=record,
            normalized=normalized,
        )
        cjc_from_deskera = str(mtc_deskera_context.get("cjc_no") or "").strip()
        if cjc_from_deskera:
            certificate_numbers["cjc_no"] = cjc_from_deskera
            certificate_numbers["CJC_NO"] = cjc_from_deskera
            certificate_numbers["cjc_number"] = cjc_from_deskera
            certificate_numbers["CJC_NUMBER"] = cjc_from_deskera
        context = {
            "work_order": record,
            "work_order_normalized": normalized,
            "work_order_code": normalized.get("identifier") or work_order_code,
            "product": normalized.get("product") or "",
            "compound": compound,
            "customer": normalized.get("customer") or "",
            "quantity": normalized.get("quantity") or "",
            "status": normalized.get("status") or "",
            "due_date": normalized.get("due_date") or "",
            "created_date": normalized.get("created_date") or "",
            "portal_url": normalized.get("portal_url") or "",
            "generated_at": timezone.now().strftime("%d %b %Y %H:%M"),
            **certificate_numbers,
            **mtc_deskera_context,
        }
        output_bytes = render_mtc_docx(template_bytes=template_bytes, context=context)
    except MTCError as exc:
        return HttpResponse(str(exc), status=500, content_type="text/plain")

    filename = build_mtc_filename(work_order_code=normalized.get("identifier") or work_order_code, compound=compound)
    response = HttpResponse(output_bytes, content_type=MTC_DOCX_MIME)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera")
def deskera_mtc_generator(request):
    return render(
        request,
        "magicwand/deskera_mtc.html",
        {
            "work_order": (request.GET.get("work_order") or "").strip(),
            "compound": (request.GET.get("compound") or "").strip(),
            "template": (request.GET.get("template") or "").strip(),
        },
    )


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera")
def deskera_mtc_template_download(request):
    template_override = (request.GET.get("template") or "").strip()
    if not template_override:
        return HttpResponse("Missing `template` query parameter.", status=400, content_type="text/plain")

    try:
        resolution = resolve_mtc_template(template_override=template_override)
        template_bytes = load_mtc_template_bytes(resolution)
    except MTCError as exc:
        return HttpResponse(str(exc), status=500, content_type="text/plain")

    filename = Path(resolution.object_name).name
    response = HttpResponse(template_bytes, content_type=MTC_DOCX_MIME)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_service_access("deskera")
def deskera_workstations(request):
    client = _build_deskera_client()

    search = (request.GET.get('search') or '').strip()
    status = (request.GET.get('status') or '').strip()
    sort_key = (request.GET.get('sort') or WORKSTATION_DEFAULT_SORT_KEY).lower()
    if sort_key not in WORKSTATION_SORT_FIELD_MAP:
        sort_key = WORKSTATION_DEFAULT_SORT_KEY
    direction_param = (request.GET.get('dir') or DEFAULT_SORT_DIR).lower()
    sort_dir = 'ASC' if direction_param == 'asc' else 'DESC'
    sort_field = WORKSTATION_SORT_FIELD_MAP[sort_key]

    portal_base = _deskera_portal_base()
    workstations, meta_summary, error, raw_payload = _fetch_all_workstations(
        client,
        portal_base=portal_base,
        search=search,
        status=status,
        sort_field=sort_field,
        sort_dir=sort_dir,
    )

    sort_state = _build_sort_state(
        request,
        current_sort=sort_key,
        current_dir=direction_param,
        columns=('name', 'work_center', 'resource', 'status', 'capacity', 'shift'),
    )

    context = {
        'workstations': workstations,
        'search': search,
        'status': status,
        'meta': meta_summary,
        'error': error,
        'portal_base': portal_base,
        'sort_state': sort_state,
        'raw_response': (raw_payload if not error else None),
        'chat_endpoint': reverse('deskera_workstations_chat'),
    }
    return render(request, 'magicwand/deskera_workstations.html', context)


@login_required
@require_service_access("deskera")
def deskera_jobcards(request):
    snapshot_client = _build_deskera_client()
    ui_client = _build_deskera_client()
    try:
        ui_timeout = int(getattr(settings, "DESKERA_MRP_UI_TIMEOUT", 15))
    except (TypeError, ValueError):
        ui_timeout = 15
    ui_client.timeout = max(5, min(ui_timeout, 60))

    search = (request.GET.get('search') or '').strip()
    status = (request.GET.get('status') or '').strip()
    sort_key = (request.GET.get('sort') or JOBCARD_DEFAULT_SORT_KEY).lower()
    if sort_key not in JOBCARD_SORT_FIELD_MAP:
        sort_key = JOBCARD_DEFAULT_SORT_KEY
    direction_param = (request.GET.get('dir') or DEFAULT_SORT_DIR).lower()
    sort_dir = 'ASC' if direction_param == 'asc' else 'DESC'
    sort_field = JOBCARD_SORT_FIELD_MAP[sort_key]

    try:
        page_value = request.GET.get('page')
        if page_value in (None, ''):
            page = 0
        else:
            page = max(0, int(page_value))
    except (TypeError, ValueError):
        page = 0

    ui_page_size = getattr(settings, 'DESKERA_JOBCARD_UI_PAGE_SIZE', 50)
    try:
        ui_limit = max(1, min(int(ui_page_size), 100))
    except (TypeError, ValueError):
        ui_limit = 50

    portal_base = _deskera_portal_base()
    warning: Optional[str] = None
    jobcards: List[Dict[str, Any]] = []
    meta_summary: Dict[str, Any] = {"page": page, "limit": ui_limit, "total": 0, "has_more": False}
    error: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None

    snapshot_key = _jobcard_snapshot_cache_key(snapshot_client)
    snapshot_error_key = _jobcard_snapshot_error_key(snapshot_client)
    snapshot = _deskera_cache_get(snapshot_key)
    last_snapshot_error = _deskera_cache_get(snapshot_error_key)
    last_snapshot_error_message = _deskera_snapshot_error_message(last_snapshot_error)
    if _deskera_snapshot_error_is_recent(last_snapshot_error) and _deskera_snapshot_has_records(snapshot):
        jobcards, meta_summary = _deskera_snapshot_page(
            snapshot,
            search=search,
            status=status,
            page=page,
            limit=ui_limit,
        )
        for record in jobcards:
            if portal_base and not record.get("portal_url"):
                record["portal_url"] = f"{portal_base}/mrp/job-card"
        warning = _deskera_user_facing_warning(last_snapshot_error_message or "Deskera MRP is temporarily unavailable.")
        raw_payload = None
    else:
        jobcards, meta_summary, error, raw_payload = _fetch_normalized_jobcards(
            ui_client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=page,
            limit=ui_limit,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )

    all_columns: List[Dict[str, str]] = []
    if error:
        _ensure_jobcard_snapshot_refresh(snapshot_client, portal_base=portal_base)
        snapshot = _deskera_cache_get(snapshot_key)
        if _deskera_snapshot_has_records(snapshot):
            paged_records, snapshot_meta = _deskera_snapshot_page(
                snapshot,
                search=search,
                status=status,
                page=page,
                limit=ui_limit,
            )

            for record in paged_records:
                if portal_base and not record.get("portal_url"):
                    record["portal_url"] = f"{portal_base}/mrp/job-card"

            jobcards = paged_records
            meta_summary = snapshot_meta
            warning = _deskera_user_facing_warning(error, has_cached_snapshot=True)
            error = None
            raw_payload = None
        else:
            error = _deskera_user_facing_warning(error, has_cached_snapshot=False)

    if not error and jobcards:
        column_keys = _deskera_jobcard_columns(jobcards)
        if not column_keys:
            column_keys = [
                "identifier",
                "work_order",
                "sales_order",
                "product",
                "station",
                "status",
                "quantity",
                "due_date",
                "operator",
                "attachments",
            ]
        all_columns = [{"key": key, "label": _humanize_deskera_column(key)} for key in column_keys]
        for card in jobcards:
            raw = card.get("raw")
            if not isinstance(raw, dict):
                raw = {}
            card["all_values"] = [
                _deskera_format_column_value(ui_client, key, raw.get(key) if key in raw else card.get(key))
                for key in column_keys
            ]

    sort_state = _build_sort_state(
        request,
        current_sort=sort_key,
        current_dir=direction_param,
        columns=('job_card', 'work_order', 'sales_order', 'product', 'station', 'status', 'due_date'),
    )

    context = {
        'jobcards': jobcards,
        'search': search,
        'status': status,
        'meta': meta_summary,
        'warning': warning,
        'error': error,
        'portal_base': portal_base,
        'sort_state': sort_state,
        'raw_response': (raw_payload if not error else None),
        'copilot_status_endpoint': reverse('deskera_jobcards_copilot_status'),
        'summary_endpoint': reverse('deskera_jobcards_summary'),
        'chat_endpoint': reverse('deskera_jobcards_chat'),
        'all_columns': all_columns,
    }

    params = request.GET.copy()
    params['page'] = str(max(page - 1, 0))
    prev_query = params.urlencode()
    prev_url = f"{request.path}?{prev_query}" if page > 0 and prev_query else (request.path if page > 0 else None)

    params = request.GET.copy()
    params['page'] = str(page + 1)
    next_query = params.urlencode()
    next_url = f"{request.path}?{next_query}" if meta_summary.get('has_more') and next_query else (request.path if meta_summary.get('has_more') else None)

    total_records = meta_summary.get('total')
    total_pages = None
    if total_records is not None:
        try:
            total_pages = max(1, (int(total_records) + ui_limit - 1) // ui_limit)
        except (TypeError, ValueError):
            total_pages = None

    copilot_status = _ensure_jobcard_snapshot_refresh(snapshot_client, portal_base=portal_base)
    snapshot = _deskera_cache_get(snapshot_key)
    context['pagination'] = {
        'page': page,
        'page_size': ui_limit,
        'prev_url': prev_url,
        'next_url': next_url,
        'total_pages': total_pages,
    }
    context['copilot'] = {
        'status': copilot_status,
        'fetched_at_ts': (snapshot.get('fetched_at_ts') if isinstance(snapshot, dict) else None),
        'records': (snapshot.get('meta', {}).get('records') if isinstance(snapshot, dict) else None),
    }

    return render(request, 'magicwand/deskera_jobcards.html', context)


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera", deny="json")
def deskera_jobcards_copilot_status(request):
    """
    Lightweight status endpoint for the job card snapshot used by Deskera Copilot.
    """
    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    if not client.is_configured:
        return JsonResponse(
            {
                "status": "error",
                "error": (
                    "Deskera MRP is not configured. Set DESKERA_MRP_BASE_URL (and optionally DESKERA_MRP_API_KEY)."
                ),
            },
            status=502,
        )

    snapshot, snapshot_error, snapshot_status = _get_or_build_jobcard_snapshot(client, portal_base=portal_base)
    lock_present = _deskera_refresh_lock_present(
        _jobcard_snapshot_lock_key(client),
        max_age_seconds=_jobcard_snapshot_lock_seconds(),
    )
    last_error = _deskera_cache_get(_jobcard_snapshot_error_key(client))

    records_count: Optional[int] = None
    fetched_at_ts: Optional[float] = None
    meta: Dict[str, Any] = {}
    if isinstance(snapshot, dict):
        fetched_at_ts_value = snapshot.get("fetched_at_ts")
        if isinstance(fetched_at_ts_value, (int, float)):
            fetched_at_ts = float(fetched_at_ts_value)
        meta = dict(snapshot.get("meta") or {})
        records_value = meta.get("records")
        if isinstance(records_value, int):
            records_count = records_value
        else:
            records_count = len(snapshot.get("records") or [])

    snapshot_usable = _deskera_snapshot_has_records(snapshot)
    error_message = None
    for candidate in (snapshot_error, last_error):
        candidate_message = _deskera_snapshot_error_message(candidate)
        if candidate_message:
            error_message = candidate_message
            break

    status_value = snapshot_status
    warning_message = None
    if error_message and snapshot_usable:
        status_value = "ready"
        warning_message = error_message
    elif error_message and not lock_present:
        status_value = "error"
        error_message = _deskera_user_facing_warning(error_message, has_cached_snapshot=False)

    return JsonResponse(
        {
            "status": status_value,
            "lock_present": lock_present,
            "cached": bool(snapshot_usable and warning_message),
            "records": records_count,
            "fetched_at_ts": fetched_at_ts,
            "meta": {
                "building": meta.get("building"),
                "partial": meta.get("partial"),
                "has_more": meta.get("has_more"),
                "fetched_pages": meta.get("fetched_pages"),
                "total": meta.get("total"),
            }
            if meta
            else None,
            "error": (error_message if status_value == "error" else None),
            "warning": warning_message,
        }
    )


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera", deny="json")
def deskera_jobcards_summary(request):
    """
    Auto-generated stakeholder summary for Deskera job cards once the full snapshot is ready.
    """
    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    if not client.is_configured:
        return JsonResponse(
            {
                "status": "error",
                "error": (
                    "Deskera MRP is not configured. Set DESKERA_MRP_BASE_URL (and optionally DESKERA_MRP_API_KEY)."
                ),
            },
            status=502,
        )

    snapshot, snapshot_error, snapshot_status = _get_or_build_jobcard_snapshot(client, portal_base=portal_base)
    if snapshot_error and snapshot_status == "error":
        return JsonResponse({"status": "error", "error": snapshot_error}, status=502)

    fetched_at_ts: Optional[float] = None
    records_count: Optional[int] = None
    meta: Dict[str, Any] = {}
    jobcards: List[Dict[str, Any]] = []
    if snapshot and isinstance(snapshot, dict):
        fetched_at_value = snapshot.get("fetched_at_ts")
        if isinstance(fetched_at_value, (int, float)):
            fetched_at_ts = float(fetched_at_value)
        meta = dict(snapshot.get("meta") or {})
        jobcards = list(snapshot.get("records") or [])
        records_value = meta.get("records")
        if isinstance(records_value, int):
            records_count = records_value
        else:
            records_count = len(jobcards)

    snapshot_payload = {
        "status": snapshot_status,
        "records": records_count,
        "fetched_at_ts": fetched_at_ts,
        "meta": {
            "building": meta.get("building"),
            "partial": meta.get("partial"),
            "has_more": meta.get("has_more"),
            "fetched_pages": meta.get("fetched_pages"),
            "total": meta.get("total"),
        }
        if meta
        else None,
        "error": snapshot_error,
    }

    if snapshot_status not in {"ready", "stale", "refreshing"} or not jobcards:
        return JsonResponse({"status": "syncing", "snapshot": snapshot_payload})

    cache_key = _jobcard_stakeholder_summary_cache_key(client, snapshot_fetched_at_ts=fetched_at_ts)
    lock_key = _jobcard_stakeholder_summary_lock_key(client, snapshot_fetched_at_ts=fetched_at_ts)
    error_key = _jobcard_stakeholder_summary_error_key(client, snapshot_fetched_at_ts=fetched_at_ts)

    force_param = (request.GET.get("force") or request.GET.get("refresh") or "").strip().lower()
    force = force_param in {"1", "true", "yes", "on"}
    if force:
        cache.delete(cache_key)
        cache.delete(error_key)

    cached = cache.get(cache_key)
    if cached and isinstance(cached, dict):
        return JsonResponse(
            {
                "status": "ready",
                "snapshot": snapshot_payload,
                "generated_at_ts": cached.get("generated_at_ts"),
                "summary": cached.get("summary"),
            }
        )

    cached_error = cache.get(error_key)
    cached_error_message = _deskera_snapshot_error_message(cached_error)
    if cached_error_message:
        return JsonResponse(
            {
                "status": "error",
                "snapshot": snapshot_payload,
                "error": cached_error_message,
            },
            status=502,
        )

    lock_present = bool(cache.get(lock_key))
    if force and lock_present:
        return JsonResponse({"status": "generating", "snapshot": snapshot_payload})

    if not lock_present and cache.add(lock_key, True, timeout=_jobcard_stakeholder_summary_timeout_seconds()):
        worker_meta = {**meta, "fetched_at_ts": fetched_at_ts, "snapshot_status": snapshot_status}
        Thread(
            target=_refresh_jobcard_stakeholder_summary_worker,
            kwargs={
                "client": client,
                "cache_key": cache_key,
                "lock_key": lock_key,
                "error_key": error_key,
                "snapshot_fetched_at_ts": fetched_at_ts,
                "jobcards": jobcards,
                "meta": worker_meta,
            },
            daemon=True,
        ).start()

    return JsonResponse({"status": "generating", "snapshot": snapshot_payload})


@login_required
@require_http_methods(["GET"])
@require_service_access("deskera", deny="json")
def deskera_workorders_summary(request):
    """
    Auto-generated stakeholder summary for Deskera work orders once the full snapshot is ready.
    """
    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    if not client.is_configured:
        return JsonResponse(
            {
                "status": "error",
                "error": (
                    "Deskera MRP is not configured. Set DESKERA_MRP_BASE_URL (and optionally DESKERA_MRP_API_KEY)."
                ),
            },
            status=502,
        )

    snapshot, snapshot_error, snapshot_status = _get_or_build_workorder_snapshot(client, portal_base=portal_base)
    if snapshot_error and snapshot_status == "error":
        return JsonResponse({"status": "error", "error": snapshot_error}, status=502)

    fetched_at_ts: Optional[float] = None
    records_count: Optional[int] = None
    meta: Dict[str, Any] = {}
    workorders: List[Dict[str, Any]] = []
    if snapshot and isinstance(snapshot, dict):
        fetched_at_value = snapshot.get("fetched_at_ts")
        if isinstance(fetched_at_value, (int, float)):
            fetched_at_ts = float(fetched_at_value)
        meta = dict(snapshot.get("meta") or {})
        workorders = list(snapshot.get("records") or [])
        records_value = meta.get("records")
        if isinstance(records_value, int):
            records_count = records_value
        else:
            records_count = len(workorders)

    snapshot_payload = {
        "status": snapshot_status,
        "records": records_count,
        "fetched_at_ts": fetched_at_ts,
        "meta": {
            "building": meta.get("building"),
            "partial": meta.get("partial"),
            "has_more": meta.get("has_more"),
            "fetched_pages": meta.get("fetched_pages"),
            "total": meta.get("total"),
        }
        if meta
        else None,
        "error": snapshot_error,
    }

    if snapshot_status not in {"ready", "stale", "refreshing"} or not workorders:
        return JsonResponse({"status": "syncing", "snapshot": snapshot_payload})

    cache_key = _workorder_stakeholder_summary_cache_key(client, snapshot_fetched_at_ts=fetched_at_ts)
    lock_key = _workorder_stakeholder_summary_lock_key(client, snapshot_fetched_at_ts=fetched_at_ts)
    error_key = _workorder_stakeholder_summary_error_key(client, snapshot_fetched_at_ts=fetched_at_ts)

    force_param = (request.GET.get("force") or request.GET.get("refresh") or "").strip().lower()
    force = force_param in {"1", "true", "yes", "on"}
    if force:
        cache.delete(cache_key)
        cache.delete(error_key)

    cached = cache.get(cache_key)
    if cached and isinstance(cached, dict):
        return JsonResponse(
            {
                "status": "ready",
                "snapshot": snapshot_payload,
                "generated_at_ts": cached.get("generated_at_ts"),
                "summary": cached.get("summary"),
            }
        )

    cached_error = cache.get(error_key)
    cached_error_message = _deskera_snapshot_error_message(cached_error)
    if cached_error_message:
        return JsonResponse(
            {
                "status": "error",
                "snapshot": snapshot_payload,
                "error": cached_error_message,
            },
            status=502,
        )

    lock_present = bool(cache.get(lock_key))
    if force and lock_present:
        return JsonResponse({"status": "generating", "snapshot": snapshot_payload})

    if not lock_present and cache.add(lock_key, True, timeout=_workorder_stakeholder_summary_timeout_seconds()):
        worker_meta = {**meta, "fetched_at_ts": fetched_at_ts, "snapshot_status": snapshot_status}
        Thread(
            target=_refresh_workorder_stakeholder_summary_worker,
            kwargs={
                "client": client,
                "cache_key": cache_key,
                "lock_key": lock_key,
                "error_key": error_key,
                "snapshot_fetched_at_ts": fetched_at_ts,
                "workorders": workorders,
                "meta": worker_meta,
            },
            daemon=True,
        ).start()

    return JsonResponse({"status": "generating", "snapshot": snapshot_payload})


@login_required
@require_http_methods(["POST"])
@require_service_access("deskera", deny="json")
def deskera_workorders_chat(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    question = (payload.get('question') or '').strip()
    if not question:
        return JsonResponse({'error': 'Please enter a question for the chatbot.'}, status=400)

    search = (payload.get('search') or '').strip()
    status = (payload.get('status') or '').strip()

    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    snapshot, snapshot_error, snapshot_status = _get_or_build_workorder_snapshot(client, portal_base=portal_base)

    workorders: List[Dict[str, Any]]
    meta: Dict[str, Any]

    if snapshot and isinstance(snapshot, dict):
        workorders = list(snapshot.get("records") or [])
        meta = dict(snapshot.get("meta") or {})
        meta["fetched_at_ts"] = snapshot.get("fetched_at_ts")
        meta["snapshot_status"] = snapshot_status
    else:
        ui_page_size = getattr(settings, 'DESKERA_WORKORDER_UI_PAGE_SIZE', 50)
        try:
            ui_limit = max(1, min(int(ui_page_size), 100))
        except (TypeError, ValueError):
            ui_limit = 50

        sort_field = SORT_FIELD_MAP.get(DEFAULT_SORT_KEY, "createdOn")
        recent_records, recent_meta, page_error, _ = _fetch_normalized_workorders(
            client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=0,
            limit=ui_limit,
            sort_field=sort_field,
            sort_dir="DESC",
            fetch_attachment_details=False,
        )
        if page_error:
            return JsonResponse({'error': snapshot_error or page_error}, status=502)

        workorders = [_workorder_snapshot_record(order) for order in recent_records]
        meta = {
            **(recent_meta or {}),
            "records": len(workorders),
            "building": True,
            "partial": True,
            "source": "recent_page",
            "snapshot_status": snapshot_status,
        }
        if snapshot_error:
            meta["snapshot_error"] = snapshot_error

    status_normalized = status.strip().lower()
    if status_normalized and status_normalized not in {"all", "any"}:
        workorders = [
            order
            for order in workorders
            if str(order.get("status") or "").strip().lower() == status_normalized
        ]
        meta["filtered_by_status"] = status

    if search:
        search_lower = search.lower()
        workorders = [
            order
            for order in workorders
            if search_lower in str(order.get("_search") or "").lower()
        ]
        meta["filtered_by_search"] = search

    try:
        answer = _generate_deskera_answer(question, workorders, meta)
    except AIServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    if meta.get("building") or meta.get("partial"):
        record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(workorders)
        answer = f"(Copilot dataset is syncing; answer based on {record_count} work orders loaded so far.)\n\n{answer}"

    return JsonResponse({'answer': answer})


@login_required
@require_http_methods(["POST"])
@require_service_access("deskera", deny="json")
def deskera_jobcards_chat(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    question = (payload.get('question') or '').strip()
    if not question:
        return JsonResponse({'error': 'Please enter a question for the chatbot.'}, status=400)

    search = (payload.get('search') or '').strip()
    status = (payload.get('status') or '').strip()

    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    snapshot, snapshot_error, snapshot_status = _get_or_build_jobcard_snapshot(client, portal_base=portal_base)

    jobcards: List[Dict[str, Any]]
    meta: Dict[str, Any]

    if snapshot and isinstance(snapshot, dict):
        jobcards = list(snapshot.get("records") or [])
        meta = dict(snapshot.get("meta") or {})
        meta["fetched_at_ts"] = snapshot.get("fetched_at_ts")
        meta["snapshot_status"] = snapshot_status
    else:
        ui_page_size = getattr(settings, 'DESKERA_JOBCARD_UI_PAGE_SIZE', 50)
        try:
            ui_limit = max(1, min(int(ui_page_size), 100))
        except (TypeError, ValueError):
            ui_limit = 50

        sort_field = JOBCARD_SORT_FIELD_MAP.get(JOBCARD_DEFAULT_SORT_KEY, "jobCardCode")
        recent_records, recent_meta, page_error, _ = _fetch_normalized_jobcards(
            client,
            portal_base=portal_base,
            search=search,
            status=status,
            page=0,
            limit=ui_limit,
            sort_field=sort_field,
            sort_dir="DESC",
        )
        if page_error:
            return JsonResponse({'error': snapshot_error or page_error}, status=502)

        jobcards = [_jobcard_snapshot_record(card) for card in recent_records]
        meta = {
            **(recent_meta or {}),
            "records": len(jobcards),
            "building": True,
            "partial": True,
            "source": "recent_page",
            "snapshot_status": snapshot_status,
        }
        if snapshot_error:
            meta["snapshot_error"] = snapshot_error

    status_normalized = status.strip().lower()
    if status_normalized and status_normalized not in {"all", "any"}:
        jobcards = [card for card in jobcards if str(card.get("status") or "").strip().lower() == status_normalized]
        meta["filtered_by_status"] = status

    if search:
        search_lower = search.lower()
        jobcards = [card for card in jobcards if search_lower in str(card.get("_search") or "").lower()]
        meta["filtered_by_search"] = search

    try:
        answer = _generate_jobcard_answer(question, jobcards, meta)
    except AIServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    if meta.get("building") or meta.get("partial"):
        record_count = meta.get("records") if isinstance(meta.get("records"), int) else len(jobcards)
        answer = f"(Copilot dataset is syncing; answer based on {record_count} job cards loaded so far.)\n\n{answer}"

    return JsonResponse({'answer': answer})


@login_required
@require_http_methods(["POST"])
@require_service_access("deskera", deny="json")
def deskera_workstations_chat(request):
    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON payload.'}, status=400)

    question = (payload.get('question') or '').strip()
    if not question:
        return JsonResponse({'error': 'Please enter a question for the chatbot.'}, status=400)

    search = (payload.get('search') or '').strip()
    status = (payload.get('status') or '').strip()

    sort_key = (payload.get('sort') or WORKSTATION_DEFAULT_SORT_KEY).lower()
    if sort_key not in WORKSTATION_SORT_FIELD_MAP:
        sort_key = WORKSTATION_DEFAULT_SORT_KEY
    direction_param = (payload.get('dir') or DEFAULT_SORT_DIR).lower()
    sort_dir = 'ASC' if direction_param == 'asc' else 'DESC'
    sort_field = WORKSTATION_SORT_FIELD_MAP[sort_key]

    try:
        limit_override = payload.get('limit')
        if limit_override is None:
            limit_override = None
        else:
            limit_override = max(1, min(int(limit_override), 200))
    except (TypeError, ValueError):
        limit_override = None

    try:
        max_pages_override = payload.get('max_pages')
        if max_pages_override is None:
            max_pages_override = None
        else:
            max_pages_override = max(1, min(int(max_pages_override), 25))
    except (TypeError, ValueError):
        max_pages_override = None

    client = _build_deskera_client()
    portal_base = _deskera_portal_base()
    workstations, meta, error, _ = _fetch_all_workstations(
        client,
        portal_base=portal_base,
        search=search,
        status=status,
        sort_field=sort_field,
        sort_dir=sort_dir,
        limit=limit_override,
        max_pages=max_pages_override,
    )

    if error:
        return JsonResponse({'error': error}, status=502)

    try:
        answer = _generate_workstation_answer(question, workstations, meta)
    except AIServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    return JsonResponse({'answer': answer})


def _workorder_identifier_from_raw(record: Dict[str, Any]) -> str:
    for key in (
        "documentSequenceCode",
        "workOrderCode",
        "workOrderNo",
        "documentNo",
        "code",
        "id",
        "reference",
    ):
        value = record.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _fetch_workorder_record_by_identifier(client: DeskeraMRPClient, identifier: str) -> Optional[Dict[str, Any]]:
    normalized_identifier = str(identifier or "").strip()
    if not normalized_identifier:
        return None

    filter_keys = (
        "workOrderCode",
        "documentSequenceCode",
        "workOrderNo",
        "documentNo",
        "code",
        "id",
        "reference",
    )

    for key in filter_keys:
        try:
            result = client.fetch_workorders(
                page=0,
                limit=5,
                extra_filters={key: normalized_identifier},
                fetch_attachment_details=False,
            )
        except DeskeraMRPError:
            continue

        records = result.get("records") or []
        for record in records:
            if _workorder_identifier_from_raw(record) == normalized_identifier:
                return record

        if len(records) == 1:
            return records[0]

    # Fallback: use search if query filters are not supported by the tenant.
    result = client.fetch_workorders(
        search=normalized_identifier,
        page=0,
        limit=25,
        fetch_attachment_details=False,
    )
    for record in result.get("records") or []:
        if _workorder_identifier_from_raw(record) == normalized_identifier:
            return record

    return None


def _normalize_workorder_record(order: Dict[str, Any], *, client: DeskeraMRPClient, portal_base: str) -> Dict[str, Any]:
    def pick(keys: Sequence[str]) -> Any:
        for key in keys:
            value = order.get(key)
            if value not in (None, "", []):
                return value
        return ""

    def as_identifier(value: Any) -> str:
        if value in (None, "", []):
            return ""
        if isinstance(value, (int, float, bool)):
            return str(value).strip()
        if isinstance(value, str):
            return value.strip()
        return ""

    identifier = pick((
        "documentSequenceCode",
        "workOrderCode",
        "workOrderNo",
        "documentNo",
        "code",
        "id",
        "reference",
    )) or ""
    description = pick((
        "description",
        "workOrderDescription",
        "itemDescription",
        "productDescription",
        "remarks",
        "note",
    ))
    if description in (None, "", [], "-", "—"):
        description = ""
    description = description or _extract_named_custom_field_text(order, "description", "desc")
    product = pick(("productName", "itemName", "name", "product"))
    quantity = pick(("quantity", "qty", "plannedQty", "orderQty", "manufactureQuantity", "actualQuantity"))
    status = pick(("status", "state", "stage")) or "Unknown"
    job_status = _extract_job_status_from_deskera_workorder(order)
    customer = pick(("customerName", "accountName", "customer", "contactName"))
    due_raw = pick(("promiseDate", "expectedCompletionDate", "deliveryDate", "dueDate", "plannedEndDate"))
    created_raw = pick(("created_at", "createdAt", "createdDate", "createdOn", "updatedOn"))

    due_date = client._format_date(due_raw) if due_raw else ""
    created_date = client._format_date(created_raw) if created_raw else ""

    portal_url = ""
    if portal_base and identifier:
        portal_url = f"{portal_base}/mrp/workorder/{identifier}"

    priority = pick(("priority", "priorityCode"))
    compound_code = _extract_compound_code_from_deskera_workorder(order)
    cut_length = _extract_cut_length_from_deskera_workorder(order)
    weight = _extract_weight_from_deskera_workorder(order)
    purchase_order = _extract_purchase_order_from_deskera_workorder(order)
    parent_work_order_code = as_identifier(
        pick(("parentWorkOrderCode", "parentWorkorderCode", "parentWorkOrderNo", "parentWorkOrder"))
    )
    parent_work_order_seq_code = as_identifier(
        pick(("parentWorkOrderSeqCode", "parentWorkOrderSequenceCode", "parentWorkOrderSeq"))
    )
    document_sequence_code = as_identifier(
        pick(("documentSequenceCode", "documentNo", "document_number", "documentNumber")) or identifier
    )
    work_order_code = as_identifier(pick(("workOrderCode", "workOrderNo", "workOrder")) or "")

    return {
        "identifier": identifier,
        "documentSequenceCode": document_sequence_code,
        "workOrderCode": work_order_code,
        "description": description or "—",
        "product": product or "Unassigned",
        "compoundCode": compound_code,
        "cutLength": cut_length,
        "weight": weight,
        "purchaseOrder": purchase_order,
        "parentWorkOrderSeqCode": parent_work_order_seq_code,
        "parentWorkOrderCode": parent_work_order_code,
        "quantity": quantity,
        "status": status,
        "jobStatus": job_status,
        "customer": customer,
        "due_date": due_date,
        "created_date": created_date,
        "priority": priority,
        "portal_url": portal_url,
        "raw": order,
    }


def _normalize_workstation_record(record: Dict[str, Any], *, client: DeskeraMRPClient, portal_base: str) -> Dict[str, Any]:
    def pick(keys: Sequence[str]) -> Any:
        for key in keys:
            value = record.get(key)
            if value not in (None, "", []):
                return value
        return ""

    identifier = pick((
        "workStationCode",
        "workstationCode",
        "code",
        "id",
    )) or ""
    name = pick(("workStationName", "workstationName", "name", "description"))
    center = pick(("workCenter", "workCentre", "workCenterName", "productionLine"))
    resource = pick(("resource", "resourceName", "machineName", "operatorName"))
    status = pick(("status", "state", "isActive"))
    capacity = pick(("capacity", "capacityPerDay", "availableCapacity"))
    shift = pick(("shift", "shiftName", "shiftCode"))
    portal_url = f"{portal_base}/mrp/work-stations"

    return {
        "identifier": identifier,
        "name": name or identifier or "—",
        "work_center": center or "—",
        "resource": resource or "—",
        "status": status or "—",
        "capacity": capacity or "—",
        "shift": shift or "—",
        "portal_url": portal_url,
        "raw": record,
    }


def _normalize_jobcard_record(record: Dict[str, Any], *, client: DeskeraMRPClient, portal_base: str) -> Dict[str, Any]:
    def pick(keys: Sequence[str]) -> Any:
        for key in keys:
            value = record.get(key)
            if value not in (None, "", []):
                return value
        return ""

    identifier = pick(("documentSequenceCode", "jobCardCode", "jobCardNo", "jobCard", "id")) or ""
    work_order = pick(("workOrderSequenceCode", "workOrderSeqCode", "workOrderNo", "workOrderCode", "workOrder"))
    sales_order = pick(("salesOrderSequenceCode", "salesOrderCustomerOrderNumber", "salesOrderCode", "salesOrderNo"))
    product = pick(("workOrderItemName", "productName", "itemName"))
    station = pick(("workStationName", "workStation", "station"))
    if not station:
        ws_details = record.get("workstationDetails")
        if isinstance(ws_details, dict):
            station = ws_details.get("workstationName") or station
    status = pick(("status", "state"))
    qty = pick(("quantity", "qty", "plannedQty"))
    due_raw = pick(("dueDate", "expectedCompletionDate"))
    due_date = client._format_date(due_raw) if due_raw else ""
    operator = ""
    op_details = record.get("operatorDetails")
    if isinstance(op_details, dict):
        operator = op_details.get("operatorName") or ""

    attachments_label = ""
    attachments_value = record.get("attachmentDetails") or record.get("attachments") or []
    attachment_names: List[str] = []
    if isinstance(attachments_value, list):
        count = len(attachments_value)
        if count == 1:
            attachments_label = "1 file"
        elif count > 1:
            attachments_label = f"{count} files"
        for item in attachments_value:
            if isinstance(item, dict):
                name = (
                    item.get("attachmentFileName")
                    or item.get("fileName")
                    or item.get("name")
                )
                if name:
                    attachment_names.append(str(name))
    elif attachments_value:
        attachments_label = "1 file"

    portal_url = f"{portal_base}/mrp/job-card"

    return {
        "identifier": identifier,
        "work_order": work_order or "—",
        "sales_order": sales_order or "—",
        "product": product or "—",
        "station": station or "—",
        "status": status or "—",
        "quantity": qty or "—",
        "due_date": due_date or "—",
        "operator": operator or "—",
        "attachments": attachments_label or "—",
        "attachment_names": attachment_names,
        "portal_url": portal_url,
        "raw": record,
    }


def _build_sort_state(request, *, current_sort: str, current_dir: str, columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    sortable_columns = ('product', 'customer', 'quantity', 'status', 'due_date', 'created')
    if columns:
        sortable_columns = tuple(columns)
    params = request.GET.copy()
    sort_links: Dict[str, Dict[str, Any]] = {}
    for column in sortable_columns:
        params_copy = params.copy()
        params_copy['page'] = '0'
        params_copy['sort'] = column
        is_active = column == current_sort
        next_dir = 'desc' if (is_active and current_dir == 'asc') else 'asc'
        params_copy['dir'] = next_dir
        query = params_copy.urlencode()
        sort_links[column] = {
            'url': f"{request.path}?{query}" if query else request.path,
            'is_active': is_active,
            'current_dir': current_dir if is_active else 'asc',
            'next_dir': next_dir,
        }
    return {
        'current': current_sort,
        'direction': current_dir,
        'columns': sort_links,
    }

@require_http_methods(["GET"])
def health_check(request):
    """
    Health check endpoint for load balancers and monitoring.
    """
    return JsonResponse({
        'status': 'healthy',
        'service': 'magicwand',
        'version': '1.0.0',
        'components': {
            'database': 'connected',
            'authentication': 'active',
            'microservices': 'integrated'
        }
    })

def logout_view(request):
    """Logout view that clears session and redirects to the frontend shell."""
    logout(request)

    frontend_base = resolve_frontend_base(request)
    logout_target = getattr(settings, 'LOGOUT_REDIRECT_URL', None)
    if logout_target:
        target_host = urlsplit(logout_target).netloc
        base_host = urlsplit(frontend_base).netloc
        if target_host and base_host and target_host.lower() != base_host.lower():
            logout_target = None
    if not logout_target:
        logout_target = f"{frontend_base}/?logout=1"

    response = HttpResponseRedirect(logout_target)

    session_cookie_name = getattr(settings, 'SESSION_COOKIE_NAME', 'sessionid')
    csrf_cookie_name = getattr(settings, 'CSRF_COOKIE_NAME', 'csrftoken')

    response.delete_cookie(session_cookie_name)
    response.delete_cookie(csrf_cookie_name)
    response.delete_cookie('allauth_session')

    domain_options = [None]
    frontend_host = urlsplit(frontend_base or logout_target).hostname
    if frontend_host:
        domain_options.append(frontend_host)
        if not frontend_host.startswith('.'):
            domain_options.append(f'.{frontend_host}')

    for domain in domain_options:
        for path in ['/', '/accounts/']:
            response.delete_cookie(session_cookie_name, domain=domain, path=path)
            response.delete_cookie(csrf_cookie_name, domain=domain, path=path)

    return response


def _cad_upload_catalogue_file(
    drawing_file: Any,
    *,
    gasket_id: str,
    gasket_type: str,
    display_name: str = "",
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    content = drawing_file.read()
    files = {
        "file": (drawing_file.name, content, drawing_file.content_type or "application/octet-stream")
    }
    data = {
        "gasket_id": gasket_id,
        "name": display_name,
        "gasket_type": gasket_type,
    }
    resp = requests.post(
        _cad_api("/catalogue/upload"),
        data=data,
        files=files,
        timeout=timeout or getattr(settings, "CAD_API_TIMEOUT", 20),
    )
    resp.raise_for_status()
    return resp.json()


def _cad_match_uploaded_file(
    match_file: Any,
    *,
    top_k: str = "5",
    gasket_type: str = "",
    match_gasket_id: str = "",
    match_gasket_label: str = "",
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    request_timeout = timeout or getattr(settings, "CAD_API_TIMEOUT", 20)
    content = match_file.read()
    query_values = [
        match_file.name,
        match_gasket_id,
        match_gasket_label,
        *_cad_extract_pdf_query_values(content),
    ]
    files = {
        "file": (match_file.name, content, match_file.content_type or "application/octet-stream")
    }
    params = {"top_k": top_k}
    data = {}
    suffix = (Path(match_file.name).suffix or "").lower()
    is_pdf = suffix == ".pdf"
    if gasket_type:
        data["gasket_type"] = gasket_type
    if match_gasket_id:
        data["gasket_id"] = match_gasket_id
    if match_gasket_label:
        data["gasket_label"] = match_gasket_label

    def _post_match(endpoint: str):
        return requests.post(
            _cad_api(endpoint),
            params=params,
            data=data if data else None,
            files=files,
            timeout=request_timeout,
        )

    resp = _post_match("/cad/match")
    if not is_pdf and resp.status_code == 202:
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        status_url = payload.get("status_url") if isinstance(payload, dict) else None
        if status_url:
            import time as _time

            max_wait = int(getattr(settings, "CAD_MATCH_WARMUP_WAIT_SECONDS", 20))
            interval = int(getattr(settings, "CAD_MATCH_WARMUP_POLL_INTERVAL", 2))
            deadline = _time.time() + max(1, max_wait)
            ready = False
            while _time.time() < deadline:
                try:
                    status_response = requests.get(_cad_api(status_url), timeout=request_timeout)
                    if status_response.ok:
                        status_payload = status_response.json()
                        if status_payload.get("has_metrics"):
                            ready = True
                            break
                    _time.sleep(max(1, interval))
                except RequestException:
                    _time.sleep(max(1, interval))
            if ready:
                resp = _post_match("/cad/match")

    resp.raise_for_status()
    match_results: Dict[str, Any] = resp.json()

    if isinstance(match_results, dict):
        if isinstance(match_results.get("results"), list):
            try:
                drawings_resp = requests.get(_cad_api("/cad/drawings"), timeout=request_timeout)
                drawings_resp.raise_for_status()
                drawings = (
                    drawings_resp.json()
                    if drawings_resp.headers.get("content-type", "").startswith("application/json")
                    else []
                )
            except RequestException:
                drawings = []
            drawing_map = {d.get("id"): d for d in (drawings or []) if isinstance(d, dict)}
            stem_map: Dict[str, Any] = {}
            for drawing in drawings or []:
                try:
                    stem_map[Path(drawing.get("storage_path", "")).stem.lower()] = drawing
                except Exception:
                    continue
            flattened = []
            for idx, item in enumerate(match_results.get("results", [])):
                if not isinstance(item, dict):
                    continue
                score = float(item.get("score", 0.0) or 0.0)
                matched_pdf = (item.get("match_pdf") or "").strip()
                stem = matched_pdf.lower()
                drawing_source = stem_map.get(stem) or drawing_map.get(matched_pdf)
                drawing = dict(drawing_source) if drawing_source else {
                    "id": matched_pdf or "unknown",
                    "title": matched_pdf or "Unknown",
                    "project": "",
                    "discipline": "",
                    "storage_path": "",
                }
                match_bbox = item.get("match_bbox")
                if drawing.get("preview_url") and isinstance(match_bbox, (list, tuple)) and len(match_bbox) == 4:
                    try:
                        coords = ",".join(str(int(coord)) for coord in match_bbox)
                        drawing["preview_url"] = f"{drawing['preview_url']}?bbox={coords}"
                    except Exception:
                        pass
                shape_id = f"{matched_pdf}#p{item.get('match_page')}" if matched_pdf else item.get("match_page")
                verdict = "MATCH" if score >= 0.92 else ("CLOSE" if score >= 0.85 else "NEW")
                flattened.append(
                    {
                        "drawing": drawing,
                        "similarity": score,
                        "similarity_percent": score * 100.0,
                        "embedding_method": item.get("source") or "pdf",
                        "rank": idx,
                        "shape_id": shape_id,
                        "shape_label": f"Page {item.get('match_page')}" if item.get("match_page") else None,
                        "shape_type": None,
                        "bounds": None,
                        "shape_viewer_url": None,
                        "download_url": f"/cad/download/{drawing.get('id')}" if drawing.get("id") else None,
                        "urn": None,
                        "derivatives": [],
                        "forge_status": None,
                        "forge_error": None,
                        "verdict": verdict,
                    }
                )
            match_results = {"matches": flattened}
        elif isinstance(match_results.get("results"), dict):
            flattened = []
            for _gid, items in match_results.get("results", {}).items():
                if not isinstance(items, list):
                    continue
                for idx, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    parent_id = item.get("drawing_id") or (
                        item.get("shape_id", "").split("#", 1)[0]
                        if isinstance(item.get("shape_id"), str)
                        else None
                    )
                    drawing = {
                        "id": parent_id or item.get("drawing_id") or "unknown",
                        "title": item.get("label") or parent_id or "Unknown",
                        "project": "",
                        "discipline": "",
                        "storage_path": item.get("storage_path") or "",
                        "preview_url": item.get("preview_url"),
                    }
                    flattened.append(
                        {
                            "drawing": drawing,
                            "similarity": float(item.get("score", 0.0) or 0.0),
                            "embedding_method": "metrics",
                            "rank": idx,
                            "shape_id": item.get("shape_id"),
                            "shape_label": item.get("label"),
                            "shape_type": None,
                            "bounds": item.get("bounds"),
                            "shape_viewer_url": None,
                            "download_url": item.get("download_url") or (f"/cad/download/{parent_id}" if parent_id else None),
                            "urn": item.get("urn"),
                            "derivatives": [],
                            "forge_status": None,
                            "forge_error": None,
                            "verdict": item.get("verdict"),
                        }
                    )
            match_results = {"matches": flattened}

    match_results = _promote_exact_cad_matches(match_results, query_values=query_values)
    if isinstance(match_results, dict) and not list(match_results.get("matches") or []):
        try:
            drawings_resp = requests.get(_cad_api("/cad/drawings"), timeout=request_timeout)
            drawings_resp.raise_for_status()
            drawings = (
                drawings_resp.json()
                if drawings_resp.headers.get("content-type", "").startswith("application/json")
                else []
            )
        except RequestException:
            drawings = []
        if isinstance(drawings, list):
            fallback_matches = _cad_exact_match_fallback(
                drawings,
                query_values=query_values,
                gasket_type=gasket_type,
            )
            if fallback_matches.get("matches"):
                match_results = fallback_matches
    if isinstance(match_results, dict):
        for item in match_results.get("matches", []):
            try:
                item["similarity_percent"] = round(float(item.get("similarity", 0.0)) * 100, 2)
            except (TypeError, ValueError):
                item["similarity_percent"] = None
            if not item.get("verdict"):
                try:
                    score = float(item.get("similarity", 0.0) or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                item["verdict"] = "MATCH" if score >= 0.92 else ("CLOSE" if score >= 0.85 else "NEW")
    return match_results


@login_required
@require_service_access("design")
def cad_library(request):
    timeout = getattr(settings, 'CAD_API_TIMEOUT', 20)
    match_results = None
    match_top_k = '3'
    match_gasket_type = ''

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'upload':
            drawing_file = request.FILES.get('drawing')
            gasket_id = request.POST.get('gasket_id') or ''
            display_name = request.POST.get('display_name') or ''
            gasket_type = (request.POST.get('gasket_type') or '').strip().upper()
            if not drawing_file:
                messages.error(request, 'Please choose a DWG, DXF, or PDF file to upload.')
            elif not gasket_id or not gasket_type:
                messages.error(request, 'Please provide both Die Number and Category for catalogue upload.')
            else:
                try:
                    response_json = _cad_upload_catalogue_file(
                        drawing_file,
                        gasket_id=gasket_id,
                        gasket_type=gasket_type,
                        display_name=display_name,
                        timeout=timeout,
                    )
                    descriptor_name = response_json.get('name') or drawing_file.name
                    messages.success(
                        request,
                        f"Uploaded '{descriptor_name}' to the CAD catalogue.",
                    )
                except RequestException as exc:
                    messages.error(request, f'CAD upload failed: {exc}')
        elif action == 'upload_folder':
            drawing_files = request.FILES.getlist('drawings')
            if not drawing_files:
                messages.error(request, 'Please choose a folder containing PDF drawings to upload.')
            else:
                uploaded = 0
                skipped = 0
                failures = []

                for drawing_file in drawing_files:
                    suffix = (Path(drawing_file.name).suffix or '').lower()
                    if suffix != '.pdf':
                        skipped += 1
                        continue

                    gasket_id = (Path(drawing_file.name).stem or '').strip().upper()
                    if not gasket_id:
                        failures.append(f"{drawing_file.name}: unable to determine Die Number from filename.")
                        continue

                    match = re.match(r'([A-Z]{3})', gasket_id)
                    gasket_type = match.group(1) if match else ''
                    if not gasket_type:
                        failures.append(f"{drawing_file.name}: unable to determine Category from filename.")
                        continue

                    try:
                        content = drawing_file.read()
                        files = {
                            'file': (
                                Path(drawing_file.name).name,
                                content,
                                drawing_file.content_type or 'application/octet-stream',
                            ),
                        }
                        data = {
                            'gasket_id': gasket_id,
                            'gasket_type': gasket_type,
                        }
                        resp = requests.post(
                            _cad_api('/catalogue/upload'),
                            data=data,
                            files=files,
                            timeout=timeout,
                        )
                        resp.raise_for_status()
                        uploaded += 1
                    except RequestException as exc:
                        failures.append(f"{drawing_file.name}: {exc}")

                if uploaded:
                    messages.success(request, f'Uploaded {uploaded} drawing(s) to the CAD catalogue.')
                if skipped:
                    messages.info(request, f'Skipped {skipped} non-PDF file(s).')
                if failures:
                    preview = ', '.join(failures[:3])
                    remaining = len(failures) - 3
                    more = f' (and {remaining} more)' if remaining > 0 else ''
                    messages.error(
                        request,
                        f"{len(failures)} drawing(s) failed to upload: {preview}{more}. "
                        "Ensure files are named like AGR-1234.pdf so Category can be inferred, or upload individually.",
                    )
        elif action == 'match':
            match_file = request.FILES.get('match_file')
            top_k = (request.POST.get('top_k') or '3').strip()
            gasket_type = (request.POST.get('match_gasket_type') or '').strip().upper()
            match_top_k = top_k
            match_gasket_type = gasket_type
            match_gasket_id = (request.POST.get('match_gasket_id') or '').strip()
            match_gasket_label = (request.POST.get('match_gasket_label') or '').strip()
            if not match_file:
                messages.error(request, 'Please choose a DWG, DXF, or PDF file to match.')
            else:
                try:
                    match_results = _cad_match_uploaded_file(
                        match_file,
                        top_k=top_k,
                        gasket_type=gasket_type,
                        match_gasket_id=match_gasket_id,
                        match_gasket_label=match_gasket_label,
                        timeout=timeout,
                    )
                    messages.success(request, 'Match analysis completed.')
                except RequestException as exc:
                    messages.error(request, f'CAD match failed: {exc}')

    default_page_size = getattr(settings, 'CAD_LIBRARY_PAGE_SIZE', 100)
    page_size_options = getattr(
        settings,
        'CAD_LIBRARY_PAGE_SIZE_OPTIONS',
        [50, 100, 200, 500],
    )

    context = {
        'match_results': match_results,
        'match_top_k': match_top_k,
        'match_gasket_type': match_gasket_type,
        'cad_api_root': settings.CAD_API_ROOT.rstrip('/'),
        'cad_page_size': default_page_size,
        'cad_page_size_options': page_size_options,
        'cad_bucket_name': getattr(settings, 'CAD_GCS_BUCKET', ''),
    }
    return render(request, 'magicwand/cad_library.html', context)


def _format_bytes(value: int) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "0 B"
    if size < 1024:
        return f"{size} B"
    units = ["KB", "MB", "GB", "TB"]
    current = float(size)
    for unit in units:
        current /= 1024.0
        if current < 1024.0:
            return f"{current:.1f} {unit}"
    return f"{current:.1f} PB"


def _sanitize_path_segment(segment: str, *, fallback: str) -> str:
    raw = (segment or "").strip().replace("\x00", "")
    if not raw or raw in {".", ".."}:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip()
    if cleaned in {".", ".."} or not cleaned:
        return fallback
    return cleaned


@login_required
@require_service_access("design")
@require_http_methods(["POST"])
def design_library_prepare_upload(request):
    """Return GCS resumable upload session URLs for a folder upload batch."""
    bucket_name = getattr(settings, "DESIGN_LIBRARY_GCS_BUCKET", "") or ""
    if not bucket_name:
        return JsonResponse({"error": "Design Library bucket is not configured."}, status=400)

    prefix_root = getattr(settings, "DESIGN_LIBRARY_GCS_PREFIX", "design-library") or "design-library"
    prefix_root = prefix_root.strip().strip("/") or "design-library"

    upload_limit_default = 250 * 1024 * 1024
    try:
        upload_limit_bytes = int(
            getattr(settings, "DESIGN_LIBRARY_FOLDER_UPLOAD_MAX_BYTES", upload_limit_default) or upload_limit_default
        )
    except (TypeError, ValueError):
        upload_limit_bytes = upload_limit_default
    if upload_limit_bytes <= 0:
        upload_limit_bytes = upload_limit_default

    try:
        payload = json.loads((request.body or b"{}").decode("utf-8"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    descriptors = payload.get("files")
    if not isinstance(descriptors, list) or not descriptors:
        return JsonResponse({"error": "No files were provided."}, status=400)

    parsed: List[Dict[str, Any]] = []
    roots: set[str] = set()
    total_upload_bytes = 0

    for idx, item in enumerate(descriptors):
        if not isinstance(item, dict):
            continue
        raw_name = (str(item.get("name") or "").replace("\\", "/")).lstrip("/")
        if not raw_name:
            continue

        raw_parts = [part for part in raw_name.split("/") if part and part != "."]
        if any(part == ".." for part in raw_parts):
            continue

        safe_parts: List[str] = []
        for part_idx, part in enumerate(raw_parts):
            fallback = "file" if part_idx == len(raw_parts) - 1 else "folder"
            safe_parts.append(_sanitize_path_segment(part, fallback=fallback))

        if len(safe_parts) > 1:
            roots.add(safe_parts[0])

        try:
            size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            size = 0

        total_upload_bytes += size
        content_type = str(item.get("content_type") or "").strip() or "application/octet-stream"
        parsed.append(
            {
                "index": idx,
                "raw_name": raw_name,
                "safe_parts": safe_parts,
                "size": size,
                "content_type": content_type,
            }
        )

    if not parsed:
        return JsonResponse({"error": "No valid files detected in the selected folder."}, status=400)

    if total_upload_bytes > upload_limit_bytes:
        return JsonResponse(
            {
                "error": (
                    f"Selected folder size is {_format_bytes(total_upload_bytes)}. "
                    f"Maximum allowed is {_format_bytes(upload_limit_bytes)}."
                )
            },
            status=400,
        )

    if len(roots) == 1:
        folder_name = next(iter(roots))
    else:
        folder_name = f"upload-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    try:
        from google.cloud import storage  # type: ignore

        storage_client = storage.Client()
        gcs_bucket = storage_client.bucket(bucket_name)
    except Exception as exc:
        return JsonResponse({"error": f"Could not initialize storage client: {exc}"}, status=500)

    request_origin = request.headers.get("Origin", "").strip() or None
    folder_prefix = f"{prefix_root}/{folder_name}/"
    uploads: List[Dict[str, Any]] = []

    try:
        for item in parsed:
            safe_parts = item["safe_parts"]
            if len(safe_parts) > 1 and safe_parts[0] in roots:
                relative_parts = safe_parts[1:]
            else:
                relative_parts = safe_parts[-1:]
            if not relative_parts:
                continue

            object_name = folder_prefix + "/".join(relative_parts)
            blob = gcs_bucket.blob(object_name)
            upload_url = blob.create_resumable_upload_session(
                content_type=item["content_type"],
                size=item["size"] if item["size"] > 0 else None,
                origin=request_origin,
            )
            uploads.append(
                {
                    "index": item["index"],
                    "upload_url": upload_url,
                    "content_type": item["content_type"],
                    "relative_path": "/".join(relative_parts),
                }
            )
    except Exception as exc:
        return JsonResponse({"error": f"Could not create upload sessions: {exc}"}, status=500)

    if not uploads:
        return JsonResponse({"error": "No valid files to upload."}, status=400)

    return JsonResponse(
        {
            "folder": folder_name,
            "upload_limit_bytes": upload_limit_bytes,
            "uploads": uploads,
        }
    )


@login_required
@require_service_access("design")
def design_library_folder(request):
    """Upload a folder of drawings and list/download stored files.

    When DESIGN_LIBRARY_GCS_BUCKET is set, files are stored in GCS under:
      DESIGN_LIBRARY_GCS_PREFIX/<folder>/...
    Otherwise they are stored locally under:
      MEDIA_ROOT/design-library/<folder>/...
    """
    bucket_name = getattr(settings, "DESIGN_LIBRARY_GCS_BUCKET", "") or ""
    prefix_root = getattr(settings, "DESIGN_LIBRARY_GCS_PREFIX", "design-library") or "design-library"
    prefix_root = prefix_root.strip().strip("/") or "design-library"
    upload_limit_default = 250 * 1024 * 1024
    try:
        upload_limit_bytes = int(
            getattr(settings, "DESIGN_LIBRARY_FOLDER_UPLOAD_MAX_BYTES", upload_limit_default) or upload_limit_default
        )
    except (TypeError, ValueError):
        upload_limit_bytes = upload_limit_default
    if upload_limit_bytes <= 0:
        upload_limit_bytes = upload_limit_default
    upload_limit_label = _format_bytes(upload_limit_bytes)

    use_gcs = bool(bucket_name)
    storage_client = None
    gcs_bucket = None
    gcs_error: Optional[str] = None
    if use_gcs:
        try:
            from google.cloud import storage  # type: ignore

            storage_client = storage.Client()
            gcs_bucket = storage_client.bucket(bucket_name)
        except Exception as exc:
            gcs_error = str(exc)

    library_root = None
    if not use_gcs:
        library_root = Path(settings.MEDIA_ROOT) / "design-library"
        library_root.mkdir(parents=True, exist_ok=True)

    selected_folder = (request.GET.get("folder") or "").strip()
    if "/" in selected_folder or "\\" in selected_folder or ".." in selected_folder:
        selected_folder = ""
    if selected_folder:
        selected_folder = _sanitize_path_segment(selected_folder, fallback="upload")
    search_query = (request.GET.get("q") or "").strip()
    if len(search_query) > 200:
        search_query = search_query[:200]
    search_query_lower = search_query.lower()
    search_all_folders = bool(search_query_lower)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload_folder":
            drawing_files = request.FILES.getlist("drawings")
            if not drawing_files:
                messages.error(request, "Please choose a folder to upload.")
                return redirect("design_library_folder")
            total_upload_bytes = sum(int(getattr(item, "size", 0) or 0) for item in drawing_files)
            if total_upload_bytes > upload_limit_bytes:
                messages.error(
                    request,
                    (
                        f"Selected folder size is {_format_bytes(total_upload_bytes)}. "
                        f"Maximum allowed is {upload_limit_label}."
                    ),
                )
                return redirect("design_library_folder")

            parsed: list[tuple[object, list[str]]] = []
            roots: set[str] = set()

            for drawing_file in drawing_files:
                raw_name = (getattr(drawing_file, "name", "") or "").replace("\\", "/").lstrip("/")
                if not raw_name:
                    continue
                raw_parts = [part for part in raw_name.split("/") if part and part != "."]
                if any(part == ".." for part in raw_parts):
                    continue

                safe_parts: list[str] = []
                for idx, part in enumerate(raw_parts):
                    fallback = "file" if idx == len(raw_parts) - 1 else "folder"
                    safe_parts.append(_sanitize_path_segment(part, fallback=fallback))

                if len(safe_parts) > 1:
                    roots.add(safe_parts[0])
                parsed.append((drawing_file, safe_parts))

            if not parsed:
                messages.error(request, "No files detected in the selected folder.")
                return redirect("design_library_folder")

            if len(roots) == 1:
                folder_name = next(iter(roots))
            else:
                folder_name = f"upload-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

            uploaded = 0
            failures = []
            prefix_base = f"{prefix_root}/"

            if use_gcs:
                if gcs_error or not storage_client or not gcs_bucket:
                    messages.error(request, "Design Library storage is not available. Please try again later.")
                    return redirect("design_library_folder")

                folder_prefix = f"{prefix_base}{folder_name}/"
                for drawing_file, safe_parts in parsed:
                    if len(safe_parts) > 1 and safe_parts[0] in roots:
                        relative_parts = safe_parts[1:]
                    else:
                        relative_parts = safe_parts[-1:]

                    if not relative_parts:
                        continue

                    object_name = folder_prefix + "/".join(relative_parts)
                    try:
                        blob = gcs_bucket.blob(object_name)
                        blob.upload_from_file(
                            drawing_file,
                            rewind=True,
                            content_type=getattr(drawing_file, "content_type", None) or "application/octet-stream",
                        )
                        uploaded += 1
                    except Exception as exc:
                        failures.append(f"{drawing_file.name}: {exc}")
            else:
                assert library_root is not None
                folder_path = library_root / folder_name
                folder_path.mkdir(parents=True, exist_ok=True)

                for drawing_file, safe_parts in parsed:
                    if len(safe_parts) > 1 and safe_parts[0] in roots:
                        relative_parts = safe_parts[1:]
                    else:
                        relative_parts = safe_parts[-1:]

                    if not relative_parts:
                        continue

                    destination = folder_path.joinpath(*relative_parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with destination.open("wb") as handle:
                            for chunk in drawing_file.chunks():
                                handle.write(chunk)
                        uploaded += 1
                    except OSError as exc:
                        failures.append(f"{drawing_file.name}: {exc}")

            if uploaded:
                messages.success(request, "Folder uploaded.")
            if failures:
                preview = ", ".join(failures[:3])
                remaining = len(failures) - 3
                more = f" (and {remaining} more)" if remaining > 0 else ""
                messages.error(request, f"{len(failures)} file(s) failed to upload: {preview}{more}.")
            return redirect(f"{reverse('design_library_folder')}?folder={quote(folder_name)}")
        if action == "delete_file":
            raw_path = (request.POST.get("file_path") or "").replace("\\", "/").lstrip("/")
            if not raw_path or ".." in raw_path.split("/"):
                messages.error(request, "Invalid file path.")
                return redirect("design_library_folder")

            selected_folder_post = (request.POST.get("selected_folder") or "").strip()
            if "/" in selected_folder_post or "\\" in selected_folder_post or ".." in selected_folder_post:
                selected_folder_post = ""
            if selected_folder_post:
                selected_folder_post = _sanitize_path_segment(selected_folder_post, fallback="upload")

            search_query_post = (request.POST.get("search_query") or "").strip()
            if len(search_query_post) > 200:
                search_query_post = search_query_post[:200]

            deleted = False
            if use_gcs:
                if gcs_error or not storage_client or not gcs_bucket:
                    messages.error(request, "Design Library storage is not available. Please try again later.")
                else:
                    prefix_base = f"{prefix_root}/"
                    object_name = f"{prefix_base}{raw_path}"
                    try:
                        blob = gcs_bucket.blob(object_name)
                        if not blob.exists(storage_client):
                            messages.error(request, "File not found.")
                        else:
                            blob.delete()
                            deleted = True
                    except Exception as exc:
                        messages.error(request, f"Could not delete file: {exc}")
            else:
                assert library_root is not None
                candidate = (library_root / raw_path).resolve()
                try:
                    candidate.relative_to(library_root.resolve())
                except ValueError:
                    messages.error(request, "Invalid file path.")
                else:
                    if not candidate.exists() or not candidate.is_file():
                        messages.error(request, "File not found.")
                    else:
                        try:
                            candidate.unlink()
                            deleted = True
                            parent = candidate.parent
                            library_base = library_root.resolve()
                            while parent != library_base:
                                try:
                                    parent.rmdir()
                                except OSError:
                                    break
                                parent = parent.parent
                        except OSError as exc:
                            messages.error(request, f"Could not delete file: {exc}")

            if deleted:
                messages.success(request, f"Deleted {Path(raw_path).name}.")

            redirect_url = reverse("design_library_folder")
            params: Dict[str, str] = {}
            if selected_folder_post:
                params["folder"] = selected_folder_post
            if search_query_post:
                params["q"] = search_query_post
            if params:
                return redirect(f"{redirect_url}?{urlencode(params)}")
            return redirect(redirect_url)

    files: list[dict[str, str]] = []
    total_files = 0
    folders: list[str] = []

    if use_gcs:
        if gcs_error or not storage_client or not gcs_bucket:
            messages.error(request, "Design Library storage is not available. Please contact support.")
            context = {
                "folders": [],
                "selected_folder": selected_folder or "",
                "files": [],
                "total_files": 0,
            }
            return render(request, "magicwand/design_library_folder.html", context)

        prefix_base = f"{prefix_root}/"
        try:
            iterator = storage_client.list_blobs(bucket_name, prefix=prefix_base, delimiter="/")
            folder_set: set[str] = set()
            for page in iterator.pages:
                for prefix in getattr(page, "prefixes", []):
                    name = prefix[len(prefix_base) :].rstrip("/")
                    if name:
                        folder_set.add(name)
            folders = sorted(folder_set)
        except Exception:
            folders = []

        total_files = 0
        try:
            for blob in storage_client.list_blobs(bucket_name, prefix=prefix_base):
                if blob.name and not blob.name.endswith("/"):
                    total_files += 1
        except Exception:
            total_files = 0

        base_prefix = prefix_base
        if selected_folder and not search_all_folders:
            base_prefix = f"{prefix_base}{selected_folder}/"

        try:
            for blob in storage_client.list_blobs(bucket_name, prefix=base_prefix):
                if not blob.name or blob.name.endswith("/"):
                    continue
                rel_from_root = blob.name[len(prefix_base) :]
                if search_query_lower and search_query_lower not in rel_from_root.lower():
                    continue
                rel_display = (
                    blob.name[len(base_prefix) :]
                    if selected_folder and not search_all_folders
                    else rel_from_root
                )
                size_label = _format_bytes(getattr(blob, "size", 0) or 0)
                files.append(
                    {
                        "relative_path": rel_display,
                        "file_path": rel_from_root,
                        "display_name": Path(blob.name).name,
                        "size_label": size_label,
                        "download_url": reverse(
                            "design_library_download",
                            kwargs={"file_path": rel_from_root},
                        ),
                    }
                )
        except Exception:
            files = []
    else:
        assert library_root is not None
        folders = sorted([p.name for p in library_root.iterdir() if p.is_dir()])
        base_path = library_root
        if selected_folder and not search_all_folders and (library_root / selected_folder).is_dir():
            base_path = library_root / selected_folder

        for path in library_root.rglob("*"):
            if path.is_file():
                total_files += 1

        for path in sorted([p for p in base_path.rglob("*") if p.is_file()]):
            rel_from_root = path.relative_to(library_root).as_posix()
            if search_query_lower and search_query_lower not in rel_from_root.lower():
                continue
            rel_display = (
                path.relative_to(base_path).as_posix()
                if selected_folder and not search_all_folders
                else rel_from_root
            )
            try:
                size_label = _format_bytes(path.stat().st_size)
            except OSError:
                size_label = "—"
            files.append(
                {
                    "relative_path": rel_display,
                    "file_path": rel_from_root,
                    "display_name": path.name,
                    "size_label": size_label,
                    "download_url": reverse("design_library_download", kwargs={"file_path": rel_from_root}),
                }
            )

    context = {
        "folders": folders,
        "selected_folder": selected_folder or "",
        "files": files,
        "total_files": total_files,
        "search_query": search_query,
        "upload_limit_bytes": upload_limit_bytes,
        "upload_limit_label": upload_limit_label,
        "searching_all_folders": search_all_folders,
        "direct_upload_enabled": use_gcs and not gcs_error,
        "prepare_upload_url": reverse("design_library_prepare_upload"),
    }
    return render(request, "magicwand/design_library_folder.html", context)


@login_required
@require_service_access("design")
def design_library_download(request, file_path: str):
    bucket_name = getattr(settings, "DESIGN_LIBRARY_GCS_BUCKET", "") or ""
    prefix_root = getattr(settings, "DESIGN_LIBRARY_GCS_PREFIX", "design-library") or "design-library"
    prefix_root = prefix_root.strip().strip("/") or "design-library"

    raw = (file_path or "").replace("\\", "/").lstrip("/")
    if not raw or ".." in raw.split("/"):
        raise Http404("File not found")

    if bucket_name:
        prefix_base = f"{prefix_root}/"
        object_name = f"{prefix_base}{raw}"
        try:
            from google.cloud import storage  # type: ignore

            storage_client = storage.Client()
            bucket = storage_client.bucket(bucket_name)
            blob = bucket.blob(object_name)
            if not blob.exists(storage_client):
                raise Http404("File not found")
            content_type = getattr(blob, "content_type", None) or None
            handle = blob.open("rb")
            return FileResponse(
                handle,
                as_attachment=True,
                filename=Path(raw).name,
                content_type=content_type,
            )
        except Http404:
            raise
        except Exception:
            raise Http404("File not found") from None

    library_root = (Path(settings.MEDIA_ROOT) / "design-library").resolve()
    candidate = (library_root / raw).resolve()
    try:
        candidate.relative_to(library_root)
    except ValueError:
        raise Http404("File not found") from None

    if not candidate.exists() or not candidate.is_file():
        raise Http404("File not found")

    return FileResponse(candidate.open("rb"), as_attachment=True, filename=candidate.name)


@login_required
@require_service_access("accounts")
def accounts_portal(request):
    """Wrapper page to keep the main header while loading Accounts via proxy.

    Builds a same-origin iframe URL `/accounts-service/<path>` from
    `ACCOUNTS_PORTAL_URL` and adds `embedded=1` to hint the upstream to hide
    its own header bar.
    """
    raw = (getattr(settings, 'ACCOUNTS_PORTAL_URL', '') or '').strip()
    if not raw:
        return render(
            request,
            'magicwand/service_missing.html',
            {'service_label': 'Accounts'},
            status=502,
        )

    parsed = urlsplit(raw)
    default_path = parsed.path or '/'
    base_qs = parsed.query or ''
    qs = base_qs
    if 'embedded=' not in (base_qs or ''):
        qs = (qs + ('&' if qs else '') + 'embedded=1')
    proxy_url = f"/accounts-service{default_path}{('?' + qs) if qs else ''}"

    context = {
        'proxy_url': proxy_url,
        'portal_url': proxy_url,
        'service_label': 'Accounts',
    }
    return render(request, 'magicwand/accounts_portal.html', context)


@csrf_exempt
@login_required
@require_service_access("accounts", deny="forbidden")
@xframe_options_sameorigin
def accounts_service_proxy(request, subpath: str = ""):
    """Lightweight reverse proxy for the Accounts service under /accounts-service/*.

    Uses settings.ACCOUNTS_PORTAL_URL to resolve the target origin. If the
    configured URL contains a path (e.g. /ui/transfers), requests to the root
    (/accounts-service/) proxy to that path; subpaths are proxied verbatim.
    """
    target_base = (getattr(settings, 'ACCOUNTS_PORTAL_URL', '') or '').strip()
    if not target_base:
        return render(
            request,
            'magicwand/service_missing.html',
            {'service_label': 'Accounts'},
            status=502,
        )

    parsed = urlsplit(target_base)
    if not parsed.scheme or not parsed.netloc:
        # Support relative URLs by resolving against current request
        target_base = request.build_absolute_uri(target_base)
        parsed = urlsplit(target_base)

    base_origin = f"{parsed.scheme}://{parsed.netloc}"
    default_path = parsed.path or '/'

    # Determine the upstream path
    upstream_path = '/' + subpath.lstrip('/') if subpath else default_path
    # Compose final URL, preserving query string
    upstream_url = urljoin(base_origin.rstrip('/') + '/', upstream_path.lstrip('/'))
    incoming_qs = request.META.get('QUERY_STRING', '')
    base_qs = parsed.query if not subpath else ''
    if base_qs and incoming_qs:
        qs = f"{base_qs}&{incoming_qs}"
    else:
        qs = base_qs or incoming_qs
    if qs:
        upstream_url = f"{upstream_url}?{qs}"

    # Build headers for upstream
    hop_by_hop = {
        'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
        'te', 'trailers', 'transfer-encoding', 'upgrade', 'content-length', 'host'
    }
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop_by_hop}
    headers['X-Forwarded-For'] = request.META.get('REMOTE_ADDR', '')
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.get_host()
    headers['X-MagicWand-Embedded'] = '1'

    method = request.method.upper()
    data = request.body if method in {'POST', 'PUT', 'PATCH', 'DELETE'} else None

    try:
        resp = requests.request(
            method,
            upstream_url,
            headers=headers,
            data=data,
            allow_redirects=False,
            timeout=getattr(settings, 'ACCOUNTS_PROXY_TIMEOUT', 30),
        )
    except RequestException as exc:
        return render(
            request,
            'magicwand/service_missing.html',
            {'service_label': f'Accounts (upstream error: {exc})'},
            status=502,
        )

    # Build Django response
    from django.http import HttpResponse

    proxy_response = HttpResponse(resp.content, status=resp.status_code)

    # Selected headers to copy back
    copy_headers = [
        'Content-Type', 'Content-Encoding', 'Cache-Control', 'Content-Disposition', 'ETag',
        'Expires', 'Last-Modified', 'Vary', 'Pragma', 'WWW-Authenticate'
    ]
    for h in copy_headers:
        if h in resp.headers:
            proxy_response[h] = resp.headers[h]

    # Rewrite Location on redirects to keep navigation under the proxy path
    location = resp.headers.get('Location')
    if location:
        try:
            loc_parsed = urlsplit(location)
            if loc_parsed.scheme and loc_parsed.netloc:
                # Absolute URL: if it points to the upstream origin, rewrite to local proxy path
                if f"{loc_parsed.scheme}://{loc_parsed.netloc}".lower() == base_origin.lower():
                    new_path = loc_parsed.path or '/'
                    new_qs = f"?{loc_parsed.query}" if loc_parsed.query else ''
                    proxy_response['Location'] = f"/accounts-service{new_path}{new_qs}"
                else:
                    # Different host; pass through unchanged
                    proxy_response['Location'] = location
            else:
                # Relative location
                rel = location if location.startswith('/') else f"/{location}"
                proxy_response['Location'] = f"/accounts-service{rel}"
        except Exception:
            proxy_response['Location'] = location

    return proxy_response


@require_http_methods(["GET"])
def auth_check(request):
    """
    Simple authentication check endpoint for the frontend.
    Returns JSON response indicating authentication status.
    """
    try:
        # Debug: Print session info
        print(f"Auth check - User: {request.user}, Authenticated: {request.user.is_authenticated}")
        print(f"Session key: {request.session.session_key}")
        print(f"Session data: {dict(request.session)}")
        
        if request.user.is_authenticated:
            return JsonResponse({
                'authenticated': True,
                'user': {
                    'email': request.user.email,
                    'name': request.user.get_full_name() or request.user.email
                }
            }, status=200)
        else:
            return JsonResponse({
                'authenticated': False
            }, status=200)
    except Exception as e:
        print(f"Auth check error: {e}")
        return JsonResponse({
            'authenticated': False,
            'error': str(e)
        }, status=200)

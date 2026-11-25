from __future__ import annotations

import json
import logging
import re
import math
import csv
import uuid
from urllib.parse import quote_plus
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError
from django.db import models
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from dashboard.models import next_certificate_serial
from magicwand.access import (
    require_any_service_access,
    require_service_access,
    user_has_service_access,
)
from .models import (
    CompoundingCRIN,
    CompoundingCRINRequest,
    CompoundingDailyScrap,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingRecordValue,
    PendingMixingLine,
    CompoundingMBPlan,
    CompoundingIoTRun,
    CompoundingStockEntry,
    IST_TIMEZONE,
    compounding_business_date,
    next_compounding_cjc_number,
)
from .records import (
    RecordImportError,
    RecordSaveError,
    apply_record_changes,
    available_months,
    build_compound_summary,
    build_month_report,
    import_workbook,
    month_bounds,
    month_workbook,
    parse_month,
)
from .stock import (
    FBIssueError,
    crin_fb_totals,
    normalize_compound_code as _normalize_compound_code,
    record_fb_issue,
    record_line_actuals,
    record_uploaded_stock,
    stock_balances,
)

try:
    from openpyxl import load_workbook
except Exception:  # pragma: no cover - handled at runtime via user-facing message
    load_workbook = None


logger = logging.getLogger(__name__)

_PRODUCTION_SERVICES = ["compounding_production", "compounding_planning"]
_MAX_CRIN_ITEMS = 50
_MAX_CRIN_QUANTITY = 2147483647


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _iot_dashboard_available() -> bool:
    return bool(getattr(settings, "DEBUG", False) or (getattr(settings, "IOT_PORTAL_URL", "") or "").strip())


def _iot_dashboard_machine_id() -> str:
    return (
        (getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        or (getattr(settings, "IOT_MACHINE_ID_SOURCE", "") or "").strip()
    )


def _parse_datetime_local(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, IST_TIMEZONE)
    return parsed.astimezone(IST_TIMEZONE)


def _iot_run_compound_options(plan: CompoundingPlan) -> List[Dict[str, str]]:
    codes = list(
        plan.mb_plans.exclude(compound_code="")
        .order_by("compound_code")
        .values_list("compound_code", flat=True)
    )
    if not codes:
        codes = _available_master_compounds(plan)
    seen: set[str] = set()
    options: List[Dict[str, str]] = []
    for code in codes:
        normalized = (code or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        options.append({"value": normalized, "label": normalized})
    return options


def _open_iot_batch(machine_id: str) -> Optional[CompoundingIoTRun]:
    """The batch started with "Start batch" that has not been ended yet on this machine."""
    return (
        CompoundingIoTRun.objects.filter(machine_id=machine_id, ended_at__isnull=True)
        .select_related("crin")
        .order_by("-started_at", "-id")
        .first()
    )


def _iot_batch_crin_options() -> List[Dict[str, str]]:
    """Open CRINs an operator can tie a batch to."""
    crins = CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN).order_by("crin_number")
    return [
        {
            "value": str(crin.pk),
            "label": f"{crin.display_number} · {crin.compound_code}" if crin.compound_code else crin.display_number,
            "compound_code": crin.compound_code or "",
        }
        for crin in crins
    ]


def _parse_batch_weight(value: str) -> tuple[Optional[Decimal], bool]:
    """Returns (weight, ok). A blank value is fine; a bad or non-positive one is not."""
    raw = (value or "").strip()
    if not raw:
        return None, True
    try:
        weight = Decimal(raw)
    except InvalidOperation:
        return None, False
    if not weight.is_finite() or weight <= 0 or weight >= Decimal("10000000"):
        return None, False
    return weight.quantize(Decimal("0.001")), True


def _iot_run_duration_text(started_at, ended_at) -> str:
    """How long a compound took on the machine, e.g. "1 h 30 min"."""
    if not started_at or not ended_at or ended_at <= started_at:
        return "-"
    total_minutes = int((ended_at - started_at).total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"


def _get_or_create_plan(plan_date: date, *, user) -> CompoundingPlan:
    plan = CompoundingPlan.objects.filter(plan_date=plan_date).first()
    if plan:
        return plan
    year = plan_date.year
    cjc_number = next_compounding_cjc_number(year=year)
    plan = CompoundingPlan.objects.create(plan_date=plan_date, cjc_number=cjc_number, created_by=user)
    return plan


def _remaining_shift_values(
    shift1: Optional[int],
    shift2: Optional[int],
    shift3: Optional[int],
    actual_total: int,
    returned_total: int,
) -> tuple[int, int, int]:
    remaining_actual = max(int(actual_total or 0), 0)
    remaining_returned = max(int(returned_total or 0), 0)
    results: list[int] = []

    for raw in (shift1, shift2, shift3):
        planned = max(int(raw or 0), 0)
        used_here = min(planned, remaining_actual)
        planned -= used_here
        remaining_actual -= used_here

        returned_here = min(planned, remaining_returned)
        planned -= returned_here
        remaining_returned -= returned_here

        results.append(planned)

    return results[0], results[1], results[2]


def _mb_consumed_batches(actual_mb: int, actual_fb: int) -> int:
    return max(int(actual_mb or 0), 0) + max(int(actual_fb or 0), 0)


def _remaining_mb_batches(
    planned_mb: int,
    actual_mb: int,
    actual_fb: int,
    returned_mb: int,
) -> int:
    return max(
        int(planned_mb or 0)
        - _mb_consumed_batches(actual_mb, actual_fb)
        - max(int(returned_mb or 0), 0),
        0,
    )


_REPORT_PERIODS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}


def _report_period(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in _REPORT_PERIODS else "daily"


def _report_period_bounds(period: str, anchor: date) -> tuple[date, date]:
    if period == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if period == "monthly":
        start = anchor.replace(day=1)
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        return start, next_month - timedelta(days=1)
    return anchor, anchor


def _report_pct(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round((int(numerator or 0) / int(denominator or 0)) * 100, 1)


def _report_pct_display(value: Optional[float]) -> str:
    if value is None:
        return "—"
    rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _empty_report_totals() -> Dict[str, Any]:
    return {
        "line_count": 0,
        "saved_line_count": 0,
        "open_line_count": 0,
        "planned_mb": 0,
        "planned_fb": 0,
        "planned_total": 0,
        "actual_mb": 0,
        "actual_fb": 0,
        "actual_total": 0,
        "mb_consumed": 0,
        "returned_mb": 0,
        "returned_fb": 0,
        "remaining_mb": 0,
        "remaining_fb": 0,
        "completion_pct": None,
        "completion_display": "—",
    }


def _finalize_report_totals(totals: Dict[str, Any]) -> Dict[str, Any]:
    totals["planned_total"] = int(totals.get("planned_mb") or 0) + int(totals.get("planned_fb") or 0)
    totals["actual_total"] = int(totals.get("actual_mb") or 0) + int(totals.get("actual_fb") or 0)
    totals["open_line_count"] = int(totals.get("line_count") or 0) - int(totals.get("saved_line_count") or 0)
    totals["completion_pct"] = _report_pct(totals["actual_total"], totals["planned_total"])
    totals["completion_display"] = _report_pct_display(totals["completion_pct"])
    return totals


def _add_report_totals(totals: Dict[str, Any], row: Dict[str, Any]) -> None:
    totals["line_count"] += 1
    totals["saved_line_count"] += 1 if row["actuals_saved"] else 0
    for key in (
        "planned_mb",
        "planned_fb",
        "actual_mb",
        "actual_fb",
        "mb_consumed",
        "returned_mb",
        "returned_fb",
        "remaining_mb",
        "remaining_fb",
    ):
        totals[key] += int(row.get(key) or 0)


def _line_report_row(line: CompoundingLine) -> Dict[str, Any]:
    shift1_mb = int(line.shift1_mb or 0)
    shift1_fb = int(line.shift1_fb or 0)
    shift2_mb = int(line.shift2_mb or 0)
    shift2_fb = int(line.shift2_fb or 0)
    shift3_mb = int(line.shift3_mb or 0)
    shift3_fb = int(line.shift3_fb or 0)
    planned_mb = shift1_mb + shift2_mb + shift3_mb
    planned_fb = shift1_fb + shift2_fb + shift3_fb
    actual_mb = int(getattr(line, "actual_mb", 0) or 0)
    actual_fb = int(getattr(line, "actual_fb", 0) or 0)
    returned_mb = int(getattr(line, "returned_to_planning_mb", 0) or 0)
    returned_fb = int(getattr(line, "returned_to_planning_fb", 0) or 0)
    remaining_mb = _remaining_mb_batches(planned_mb, actual_mb, actual_fb, returned_mb)
    remaining_fb = max(planned_fb - actual_fb - returned_fb, 0)
    planned_total = planned_mb + planned_fb
    actual_total = actual_mb + actual_fb
    completion_pct = _report_pct(actual_total, planned_total)
    plan = line.plan
    compound_code = (line.compound_code or "").strip() or "Unspecified"
    return {
        "id": line.id,
        "plan_date": plan.plan_date,
        "cjc_number": plan.cjc_number,
        "compound_code": compound_code,
        "shift1_mb": shift1_mb,
        "shift1_fb": shift1_fb,
        "shift2_mb": shift2_mb,
        "shift2_fb": shift2_fb,
        "shift3_mb": shift3_mb,
        "shift3_fb": shift3_fb,
        "planned_mb": planned_mb,
        "planned_fb": planned_fb,
        "planned_total": planned_total,
        "actual_mb": actual_mb,
        "actual_fb": actual_fb,
        "actual_total": actual_total,
        "mb_consumed": _mb_consumed_batches(actual_mb, actual_fb),
        "returned_mb": returned_mb,
        "returned_fb": returned_fb,
        "remaining_mb": remaining_mb,
        "remaining_fb": remaining_fb,
        "actuals_saved": bool(getattr(line, "actuals_saved", False)),
        "status_label": "Saved" if bool(getattr(line, "actuals_saved", False)) else "Open",
        "completion_pct": completion_pct,
        "completion_display": _report_pct_display(completion_pct),
    }


def _build_compounding_production_report(*, period: str, anchor: date) -> Dict[str, Any]:
    period = _report_period(period)
    start_date, end_date = _report_period_bounds(period, anchor)
    plans = list(
        CompoundingPlan.objects.filter(plan_date__gte=start_date, plan_date__lte=end_date).order_by("plan_date", "id")
    )
    line_objs = list(
        CompoundingLine.objects.filter(plan__plan_date__gte=start_date, plan__plan_date__lte=end_date)
        .select_related("plan")
        .order_by("plan__plan_date", "position", "id")
    )

    overall = _empty_report_totals()
    overall["plan_count"] = len(plans)
    daily_index: Dict[date, Dict[str, Any]] = {}
    for plan in plans:
        daily_index[plan.plan_date] = {
            "plan_date": plan.plan_date,
            "cjc_numbers": str(plan.cjc_number),
            "_cjc_numbers": {str(plan.cjc_number)},
            **_empty_report_totals(),
        }

    compound_index: Dict[str, Dict[str, Any]] = {}
    shift_totals = [
        {"label": "1st shift", "planned_mb": 0, "planned_fb": 0, "planned_total": 0},
        {"label": "2nd shift", "planned_mb": 0, "planned_fb": 0, "planned_total": 0},
        {"label": "3rd shift", "planned_mb": 0, "planned_fb": 0, "planned_total": 0},
    ]
    rows: List[Dict[str, Any]] = []

    for line in line_objs:
        row = _line_report_row(line)
        rows.append(row)
        _add_report_totals(overall, row)

        daily = daily_index.setdefault(
            row["plan_date"],
            {
                "plan_date": row["plan_date"],
                "cjc_numbers": "",
                "_cjc_numbers": set(),
                **_empty_report_totals(),
            },
        )
        daily["_cjc_numbers"].add(str(row["cjc_number"]))
        _add_report_totals(daily, row)

        compound = compound_index.setdefault(
            row["compound_code"],
            {
                "compound_code": row["compound_code"],
                "active_days": set(),
                **_empty_report_totals(),
            },
        )
        compound["active_days"].add(row["plan_date"])
        _add_report_totals(compound, row)

        shift_totals[0]["planned_mb"] += row["shift1_mb"]
        shift_totals[0]["planned_fb"] += row["shift1_fb"]
        shift_totals[1]["planned_mb"] += row["shift2_mb"]
        shift_totals[1]["planned_fb"] += row["shift2_fb"]
        shift_totals[2]["planned_mb"] += row["shift3_mb"]
        shift_totals[2]["planned_fb"] += row["shift3_fb"]

    _finalize_report_totals(overall)
    for shift in shift_totals:
        shift["planned_total"] = int(shift["planned_mb"] or 0) + int(shift["planned_fb"] or 0)

    daily_rows: List[Dict[str, Any]] = []
    for item in daily_index.values():
        _finalize_report_totals(item)
        cjc_numbers = sorted(item.pop("_cjc_numbers", set()))
        item["cjc_numbers"] = ", ".join(cjc_numbers)
        daily_rows.append(item)
    daily_rows.sort(key=lambda item: item["plan_date"])

    compound_rows: List[Dict[str, Any]] = []
    for item in compound_index.values():
        _finalize_report_totals(item)
        active_days = item.pop("active_days", set())
        item["active_day_count"] = len(active_days)
        compound_rows.append(item)
    compound_rows.sort(key=lambda item: (-int(item.get("actual_total") or 0), item["compound_code"]))

    return {
        "period": period,
        "period_label": _REPORT_PERIODS[period],
        "anchor_date": anchor,
        "start_date": start_date,
        "end_date": end_date,
        "overall": overall,
        "daily_rows": daily_rows,
        "compound_rows": compound_rows,
        "shift_totals": shift_totals,
        "line_rows": rows,
        "has_rows": bool(rows),
    }


def _seed_rollover_lines_for_display(plan: CompoundingPlan) -> None:
    if plan.lines.exists():
        return

    previous_plan = CompoundingPlan.objects.filter(plan_date=plan.plan_date - timedelta(days=1)).first()
    if previous_plan is None:
        return

    next_position = 0
    for previous_line in previous_plan.lines.all().order_by("position", "id"):
        shift1_mb, shift2_mb, shift3_mb = _remaining_shift_values(
            previous_line.shift1_mb,
            previous_line.shift2_mb,
            previous_line.shift3_mb,
            _mb_consumed_batches(
                int(getattr(previous_line, "actual_mb", 0) or 0),
                int(getattr(previous_line, "actual_fb", 0) or 0),
            ),
            int(getattr(previous_line, "returned_to_planning_mb", 0) or 0),
        )
        shift1_fb, shift2_fb, shift3_fb = _remaining_shift_values(
            previous_line.shift1_fb,
            previous_line.shift2_fb,
            previous_line.shift3_fb,
            int(getattr(previous_line, "actual_fb", 0) or 0),
            int(getattr(previous_line, "returned_to_planning_fb", 0) or 0),
        )

        if all(value <= 0 for value in (shift1_mb, shift2_mb, shift3_mb, shift1_fb, shift2_fb, shift3_fb)):
            continue

        if previous_line.crin_request_id and not previous_line.actuals_saved:
            # A CRIN's unfinished line moves to the new day with its CRIN link, so saving
            # it can still send the remaining batches back to that CRIN's planning.
            previous_line.plan = plan
            previous_line.position = next_position
            previous_line.save(update_fields=["plan", "position", "updated_at"])
            next_position += 1
            continue

        CompoundingLine.objects.create(
            plan=plan,
            position=next_position,
            compound_code=previous_line.compound_code,
            shift1_mb=shift1_mb or None,
            shift2_mb=shift2_mb or None,
            shift3_mb=shift3_mb or None,
            shift1_fb=shift1_fb or None,
            shift2_fb=shift2_fb or None,
            shift3_fb=shift3_fb or None,
        )
        next_position += 1


def _display_folder_label(folder_prefix: str) -> str:
    prefix = str(getattr(settings, "MTC_TEMPLATES_PREFIX", "") or "").strip().lstrip("/")
    prefix = f"{prefix}/" if prefix and not prefix.endswith("/") else prefix
    label = (folder_prefix or "").strip()
    if prefix and label.startswith(prefix):
        label = label[len(prefix):]
    return label.rstrip("/") or folder_prefix


_FOLDER_CODE_PREFIX_RE = re.compile(r"^\s*\d+\s*[-:]\s*", re.IGNORECASE)


def _clean_compound_code(value: Any) -> str:
    label = str(value or "").strip()
    label = " ".join(label.split())
    label = re.sub(r"\bAG\s+", "AG-", label, flags=re.IGNORECASE)
    return label


def _compound_from_folder(folder_prefix: str) -> str:
    label = _display_folder_label(folder_prefix)
    label = label.strip().rstrip("/")
    label = _FOLDER_CODE_PREFIX_RE.sub("", label).strip()
    return _clean_compound_code(label)


def _cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_sheet(headers: List[Any], rows: List[Any]) -> tuple[list[str], list[list[str]]]:
    normalized_headers = [_cell_to_text(item) for item in list(headers or [])]

    normalized_rows: list[list[str]] = []
    for raw_row in list(rows or []):
        if isinstance(raw_row, (list, tuple)):
            normalized_rows.append([_cell_to_text(cell) for cell in raw_row])

    max_cols = len(normalized_headers)
    for row in normalized_rows:
        max_cols = max(max_cols, len(row))
    if max_cols <= 0:
        return [], []

    if len(normalized_headers) < max_cols:
        normalized_headers.extend([""] * (max_cols - len(normalized_headers)))
    elif len(normalized_headers) > max_cols:
        normalized_headers = normalized_headers[:max_cols]

    padded_rows: list[list[str]] = []
    for row in normalized_rows:
        padded = row[:max_cols]
        if len(padded) < max_cols:
            padded.extend([""] * (max_cols - len(padded)))
        padded_rows.append(padded)

    used_col_indexes: list[int] = []
    for idx in range(max_cols):
        header_has_value = bool((normalized_headers[idx] or "").strip())
        column_has_value = any((row[idx] or "").strip() for row in padded_rows)
        if header_has_value or column_has_value:
            used_col_indexes.append(idx)

    if not used_col_indexes:
        return [], []

    filtered_headers = [(normalized_headers[idx] or "").strip() for idx in used_col_indexes]
    filtered_rows: list[list[str]] = []
    for row in padded_rows:
        trimmed_row = [row[idx] for idx in used_col_indexes]
        if any((cell or "").strip() for cell in trimmed_row):
            filtered_rows.append(trimmed_row)

    # Ensure data columns without a header still get a stable column name.
    for idx, header in enumerate(filtered_headers, start=1):
        if not header:
            filtered_headers[idx - 1] = f"Column {idx}"

    return filtered_headers, filtered_rows


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _find_header_index(headers: List[str], candidates: List[str]) -> Optional[int]:
    keyed = [_header_key(header) for header in headers]
    for idx, key in enumerate(keyed):
        if key in candidates:
            return idx
    return None


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _parse_shift(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if text in {"1", "2", "3"}:
        return int(text)
    return None


def _decimal_text(value: Decimal, places: int = 3) -> str:
    quant = Decimal("1").scaleb(-places)
    rendered = format(value.quantize(quant), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _normalize_master_sheet_decimal_cells(headers: List[str], rows: List[List[str]], places: int = 3) -> List[List[str]]:
    cols = _master_sheet_columns(headers)
    decimal_indexes = {
        cols.get("mb_weight"),
        cols.get("chemical_weight"),
        cols.get("fb_total_weight"),
        cols.get("mb_stock"),
        cols.get("fb_stock"),
    }
    indexes = sorted(idx for idx in decimal_indexes if isinstance(idx, int))
    if not indexes:
        return [list(row or []) for row in list(rows or [])]

    normalized_rows: List[List[str]] = []
    width = len(headers)
    for raw in list(rows or []):
        row = list(raw or [])[:width]
        if len(row) < width:
            row.extend([""] * (width - len(row)))
        for idx in indexes:
            if idx >= len(row):
                continue
            value = _to_decimal(row[idx])
            if value is None:
                continue
            row[idx] = _decimal_text(value, places=places)
        normalized_rows.append(row)
    return normalized_rows


def _compute_batches(quantity: int, batch_size: Optional[Decimal]) -> int:
    qty = int(quantity or 0)
    if qty <= 0 or batch_size is None:
        return 0
    if batch_size <= 0:
        return 0
    return int(math.ceil(float(Decimal(qty) / batch_size)))


def _master_sheet_columns(headers: List[str]) -> Dict[str, Optional[int]]:
    return {
        "compound_code": _find_header_index(
            headers,
            [
                "compoundcode",
                "compound",
                "compoundid",
            ],
        ),
        "mb_weight": _find_header_index(
            headers,
            [
                "mbweight",
                "mbwt",
            ],
        ),
        "chemical_weight": _find_header_index(
            headers,
            [
                "chemicalsweight",
                "chemicalweight",
                "chemweight",
                "chemicalwt",
            ],
        ),
        "fb_total_weight": _find_header_index(
            headers,
            [
                "fbtotalweight",
                "fbtotalwt",
                "mbfbtotalweight",
                "mbandfbtotalweight",
                "mbfbandtotalweight",
                "mbfbweight",
                "fbbatchweight",
            ],
        ),
        "mb_min_stock": _find_header_index(
            headers,
            [
                "mbminstocklevel",
                "mbminstocklavel",
                "mbminstock",
                "msl",
            ],
        ),
        "mb_stock": _find_header_index(
            headers,
            [
                "mbcorrectstocklevel",
                "mbcorrectstocklevels",
                "mbcorrectstocklev",
                "mbstocklevel",
                "mbstock",
            ],
        ),
        "fb_stock": _find_header_index(
            headers,
            [
                "fbcorrectstocklevel",
                "fbcorrectstocklevels",
                "fbcorrectstocklev",
                "fbstocklevel",
                "fbstock",
            ],
        ),
    }


def _has_core_compounding_headers(row: List[Any]) -> bool:
    keys = [_header_key(value) for value in list(row or []) if str(value or "").strip()]
    if not keys:
        return False
    has_compound = any(key in {"compoundcode", "compound", "compoundid"} for key in keys)
    has_mb_weight = any(key in {"mbweight", "mbwt"} for key in keys)
    has_msl = any(key in {"mbminstocklevel", "mbminstocklavel", "mbminstock", "msl"} for key in keys)
    return has_compound and (has_mb_weight or has_msl)


def _sheet_table_with_detected_headers(headers: List[Any], rows: List[Any]) -> tuple[list[str], list[list[str]], Dict[str, Optional[int]]]:
    base_headers, base_rows = _normalize_sheet(list(headers or []), list(rows or []))
    base_cols = _master_sheet_columns(base_headers)
    if base_cols.get("compound_code") is not None and (
        base_cols.get("mb_weight") is not None or base_cols.get("mb_min_stock") is not None
    ):
        return base_headers, base_rows, base_cols

    # Some files include a title row above the actual header row.
    for idx, row in enumerate(base_rows[:60]):
        if not _has_core_compounding_headers(row):
            continue
        cand_headers, cand_rows = _normalize_sheet(list(row or []), base_rows[idx + 1 :])
        cand_cols = _master_sheet_columns(cand_headers)
        if cand_cols.get("compound_code") is not None and (
            cand_cols.get("mb_weight") is not None or cand_cols.get("mb_min_stock") is not None
        ):
            return cand_headers, cand_rows, cand_cols

    return base_headers, base_rows, base_cols


def _split_header_and_rows(parsed_rows: List[List[str]]) -> tuple[list[str], list[list[str]]]:
    non_empty_rows = [list(row or []) for row in list(parsed_rows or []) if any((cell or "").strip() for cell in row)]
    if not non_empty_rows:
        return [], []

    # Prefer a real table header when title/banner rows exist above it.
    header_index = 0
    for idx, row in enumerate(non_empty_rows[:60]):
        if _has_core_compounding_headers(row):
            header_index = idx
            break

    return list(non_empty_rows[header_index] or []), list(non_empty_rows[header_index + 1 :] or [])


def _master_sheet_for_plan(plan: CompoundingPlan) -> Optional[CompoundingMasterSheet]:
    sheet = CompoundingMasterSheet.objects.filter(plan=plan).first()
    if sheet:
        return sheet

    # Fallback for new plan dates when user has not re-uploaded master yet.
    return (
        CompoundingMasterSheet.objects.filter(plan__plan_date__lt=plan.plan_date)
        .select_related("plan")
        .order_by("-plan__plan_date", "-updated_at")
        .first()
    )


def _available_master_compounds(plan: CompoundingPlan) -> List[str]:
    return _compounds_from_sheet(_master_sheet_for_plan(plan))


def _compounds_from_sheet(sheet: Optional[CompoundingMasterSheet]) -> List[str]:
    if not sheet:
        return []
    headers, rows, cols = _sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
    code_idx = cols.get("compound_code")
    if not headers or code_idx is None:
        return []

    seen: set[str] = set()
    compounds: list[str] = []
    for idx, _raw in enumerate(rows):
        row = _get_sheet_row(rows, idx, len(headers))
        code = _clean_compound_code(row[code_idx])
        if not code:
            continue
        key = _normalize_compound_code(code)
        if not key or key in seen:
            continue
        seen.add(key)
        compounds.append(code)
    return compounds


def _get_sheet_row(rows: List[List[str]], idx: int, size: int) -> List[str]:
    row = list(rows[idx] or [])[:size]
    if len(row) < size:
        row.extend([""] * (size - len(row)))
    return row


def _sheet_spec_for_compound(plan: CompoundingPlan, compound_code: str) -> Dict[str, Any]:
    sheet = _master_sheet_for_plan(plan)
    if not sheet:
        return {}
    headers, rows, cols = _sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
    if not headers:
        return {}
    code_idx = cols.get("compound_code")
    if code_idx is None:
        return {}

    target_code = _normalize_compound_code(compound_code)
    for idx, raw in enumerate(rows):
        row = _get_sheet_row(rows, idx, len(headers))
        if _normalize_compound_code(row[code_idx]) != target_code:
            continue
        mb_weight = _to_decimal(row[cols["mb_weight"]]) if cols.get("mb_weight") is not None else None
        chemical_weight = _to_decimal(row[cols["chemical_weight"]]) if cols.get("chemical_weight") is not None else None
        fb_total_weight = _to_decimal(row[cols["fb_total_weight"]]) if cols.get("fb_total_weight") is not None else None
        # FB batch size must come from "MB & FB Total Weight" when available.
        # Only fallback to MB+chemical when that column is missing/blank.
        if fb_total_weight is None and mb_weight is not None and chemical_weight is not None:
            fb_total_weight = mb_weight + chemical_weight
        sheet_mb_stock = _to_decimal(row[cols["mb_stock"]]) if cols.get("mb_stock") is not None else None
        sheet_fb_stock = _to_decimal(row[cols["fb_stock"]]) if cols.get("fb_stock") is not None else None
        # The ledger is the live balance; the sheet is only the starting value.
        ledger = stock_balances([target_code]).get(target_code) or {}
        return {
            "sheet": sheet,
            "headers": headers,
            "rows": rows,
            "columns": cols,
            "row_index": idx,
            "mb_weight": mb_weight,
            "chemical_weight": chemical_weight,
            "fb_total_weight": fb_total_weight,
            "mb_min_stock": _to_decimal(row[cols["mb_min_stock"]]) if cols.get("mb_min_stock") is not None else None,
            "mb_stock": ledger.get(CompoundingStockEntry.STOCK_MB, sheet_mb_stock),
            "fb_stock": ledger.get(CompoundingStockEntry.STOCK_FB, sheet_fb_stock),
        }
    return {}


def _rows_with_ledger_stock(headers: List[str], rows: List[List[str]]) -> List[List[str]]:
    """Show live ledger balances in the stock columns of a master sheet table."""
    columns = _master_sheet_columns(headers)
    code_idx = columns.get("compound_code")
    if code_idx is None:
        return rows
    balances = stock_balances()
    updated: List[List[str]] = []
    for idx, _raw in enumerate(rows):
        row = _get_sheet_row(rows, idx, len(headers))
        ledger = balances.get(_normalize_compound_code(row[code_idx])) or {}
        for stock_type, column in (
            (CompoundingStockEntry.STOCK_MB, columns.get("mb_stock")),
            (CompoundingStockEntry.STOCK_FB, columns.get("fb_stock")),
        ):
            if column is not None and stock_type in ledger:
                row[column] = _decimal_text(ledger[stock_type])
        updated.append(row)
    return updated


def _actual_totals_by_compound(plan: CompoundingPlan) -> Dict[str, Dict[str, int]]:
    totals: Dict[str, Dict[str, int]] = {}
    grouped = (
        plan.lines.values("compound_code")
        .annotate(total_actual_mb=Sum("actual_mb"), total_actual_fb=Sum("actual_fb"))
        .order_by()
    )
    for item in grouped:
        key = _normalize_compound_code(item.get("compound_code"))
        if not key:
            continue
        totals[key] = {
            "actual_mb": int(item.get("total_actual_mb") or 0),
            "actual_fb": int(item.get("total_actual_fb") or 0),
        }
    return totals


def _row_batch_weights(row: List[str], columns: Dict[str, Optional[int]]) -> tuple[Decimal, Decimal]:
    mb_weight = _to_decimal(row[columns["mb_weight"]]) if columns.get("mb_weight") is not None else None
    chemical_weight = _to_decimal(row[columns["chemical_weight"]]) if columns.get("chemical_weight") is not None else None
    fb_total_weight = _to_decimal(row[columns["fb_total_weight"]]) if columns.get("fb_total_weight") is not None else None
    if fb_total_weight is None and mb_weight is not None and chemical_weight is not None:
        fb_total_weight = mb_weight + chemical_weight
    if mb_weight is None:
        mb_weight = Decimal("0")
    if fb_total_weight is None:
        fb_total_weight = Decimal("0")
    return mb_weight, fb_total_weight


def _derive_base_rows(
    *,
    headers: List[str],
    rows: List[List[str]],
    columns: Dict[str, Optional[int]],
    actual_totals: Dict[str, Dict[str, int]],
) -> List[List[str]]:
    size = len(headers)
    code_idx = columns.get("compound_code")
    mb_stock_idx = columns.get("mb_stock")
    fb_stock_idx = columns.get("fb_stock")

    base_rows: List[List[str]] = []
    for idx, _ in enumerate(rows):
        row = _get_sheet_row(rows, idx, size)
        if code_idx is None:
            base_rows.append(row)
            continue
        code_key = _normalize_compound_code(row[code_idx])
        totals = actual_totals.get(code_key) or {}
        actual_mb = int(totals.get("actual_mb") or 0)
        actual_fb = int(totals.get("actual_fb") or 0)
        if actual_mb == 0 and actual_fb == 0:
            base_rows.append(row)
            continue

        mb_weight, fb_weight = _row_batch_weights(row, columns)
        if mb_stock_idx is not None:
            current_mb = _to_decimal(row[mb_stock_idx])
            current_mb = current_mb if current_mb is not None else Decimal("0")
            base_mb = current_mb - (Decimal(actual_mb) * mb_weight) + (Decimal(actual_fb) * mb_weight)
            row[mb_stock_idx] = _decimal_text(base_mb, places=3)
        if fb_stock_idx is not None:
            current_fb = _to_decimal(row[fb_stock_idx])
            current_fb = current_fb if current_fb is not None else Decimal("0")
            base_fb = current_fb - (Decimal(actual_fb) * fb_weight)
            row[fb_stock_idx] = _decimal_text(base_fb, places=3)
        base_rows.append(row)
    return base_rows


def _recompute_rows_from_base(
    *,
    headers: List[str],
    base_rows: List[List[str]],
    columns: Dict[str, Optional[int]],
    actual_totals: Dict[str, Dict[str, int]],
) -> List[List[str]]:
    size = len(headers)
    code_idx = columns.get("compound_code")
    mb_stock_idx = columns.get("mb_stock")
    fb_stock_idx = columns.get("fb_stock")

    rows: List[List[str]] = []
    for idx, _ in enumerate(base_rows):
        row = _get_sheet_row(base_rows, idx, size)
        if code_idx is None:
            rows.append(row)
            continue
        code_key = _normalize_compound_code(row[code_idx])
        totals = actual_totals.get(code_key) or {}
        actual_mb = int(totals.get("actual_mb") or 0)
        actual_fb = int(totals.get("actual_fb") or 0)
        if actual_mb == 0 and actual_fb == 0:
            rows.append(row)
            continue

        mb_weight, fb_weight = _row_batch_weights(row, columns)
        mb_stock = _to_decimal(row[mb_stock_idx]) if mb_stock_idx is not None else None
        fb_stock = _to_decimal(row[fb_stock_idx]) if fb_stock_idx is not None else None
        mb_stock = mb_stock if mb_stock is not None else Decimal("0")
        fb_stock = fb_stock if fb_stock is not None else Decimal("0")

        mb_stock = mb_stock + (Decimal(actual_mb) * mb_weight)
        mb_stock = mb_stock - (Decimal(actual_fb) * mb_weight)
        fb_stock = fb_stock + (Decimal(actual_fb) * fb_weight)

        if mb_stock_idx is not None:
            row[mb_stock_idx] = _decimal_text(mb_stock, places=3)
        if fb_stock_idx is not None:
            row[fb_stock_idx] = _decimal_text(fb_stock, places=3)
        rows.append(row)
    return rows


def _header_signature(headers: List[str]) -> tuple[str, ...]:
    return tuple(_header_key(item) for item in list(headers or []))


def _estimate_remaining_quantity(
    *,
    plan: CompoundingPlan,
    compound_code: str,
    remaining_mb: int,
    remaining_fb: int,
) -> tuple[int, Optional[Decimal]]:
    spec = _sheet_spec_for_compound(plan, compound_code)
    mb_weight = spec.get("mb_weight")
    fb_weight = spec.get("fb_total_weight")

    candidates: List[Decimal] = []
    if remaining_mb > 0 and mb_weight is not None and mb_weight > 0:
        candidates.append(Decimal(int(remaining_mb)) * mb_weight)
    if remaining_fb > 0 and fb_weight is not None and fb_weight > 0:
        candidates.append(Decimal(int(remaining_fb)) * fb_weight)

    if candidates:
        qty = max(candidates)
    else:
        qty = Decimal(max(int(remaining_mb), int(remaining_fb), 1))
    qty_int = int(qty.to_integral_value(rounding=ROUND_CEILING))
    if qty_int <= 0:
        qty_int = 1
    return qty_int, fb_weight


def _min_quantity_for_mb_batches(*, batches: int, batch_size: Optional[Decimal]) -> int:
    batch_count = int(batches or 0)
    if batch_count <= 0 or batch_size is None or batch_size <= 0:
        return 0
    lower = Decimal(batch_count - 1) * batch_size
    return int(lower.to_integral_value(rounding=ROUND_FLOOR)) + 1


def _fb_required_mb_stock(*, fb_batches: int, mb_weight: Optional[Decimal]) -> Optional[Decimal]:
    batch_count = int(fb_batches or 0)
    if batch_count <= 0:
        return Decimal("0")
    if mb_weight is None or mb_weight <= 0:
        return None
    return Decimal(batch_count) * mb_weight


def _available_mb_batches(*, mb_stock: Optional[Decimal], mb_weight: Optional[Decimal]) -> Optional[int]:
    if mb_stock is None or mb_weight is None or mb_weight <= 0:
        return None
    available = mb_stock / mb_weight
    return max(int(available.to_integral_value(rounding=ROUND_FLOOR)), 0)


def _mb_demand_by_compound(plan: CompoundingPlan) -> Dict[str, Dict[str, int]]:
    """
    Outstanding batches per compound across every date, because MB stock is shared.

    - on_board_fb: FB batches on unsaved board lines (these reserve MB stock)
    - incoming_mb: MB batches on unsaved board lines (MB that is yet to be made)
    - waiting_fb: FB batches on open CRINs that have not been sent to the board
    """
    demand: Dict[str, Dict[str, int]] = {}

    def bucket(key: str) -> Dict[str, int]:
        return demand.setdefault(key, {"on_board_fb": 0, "incoming_mb": 0, "waiting_fb": 0})

    on_board_crin_ids: set[int] = set()
    for line in CompoundingLine.objects.filter(actuals_saved=False):
        if line.crin_request_id:
            on_board_crin_ids.add(line.crin_request_id)
        key = _normalize_compound_code(line.compound_code)
        if not key:
            continue
        planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
        planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
        entry = bucket(key)
        entry["on_board_fb"] += max(planned_fb - int(line.actual_fb or 0) - int(line.returned_to_planning_fb or 0), 0)
        entry["incoming_mb"] += max(planned_mb - int(line.actual_mb or 0) - int(line.returned_to_planning_mb or 0), 0)

    # A CRIN stays open until issued, but a saved linked line means its batches are made.
    made_crin_ids = set(
        CompoundingLine.objects.filter(actuals_saved=True, crin_request__isnull=False).values_list(
            "crin_request_id", flat=True
        )
    )
    fb_weights: Dict[str, Optional[Decimal]] = {}
    for crin in CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN):
        if crin.id in on_board_crin_ids or crin.id in made_crin_ids:
            continue
        key = _normalize_compound_code(crin.compound_code)
        if not key:
            continue
        if crin.fb_planning is not None:
            batches = int(crin.fb_planning)
        else:
            if key not in fb_weights:
                fb_weights[key] = _sheet_spec_for_compound(plan, crin.compound_code).get("fb_total_weight")
            batches = _compute_batches(crin.quantity, fb_weights[key])
        bucket(key)["waiting_fb"] += batches
    return demand


def _suggested_mb_plan(shortage: Decimal, mb_weight: Decimal) -> tuple[int, int]:
    """Return (quantity, batches) that cover a shortage, keeping the two values consistent."""
    if shortage <= 0 or mb_weight <= 0:
        return 0, 0
    batches = int((shortage / mb_weight).to_integral_value(rounding=ROUND_CEILING))
    rounded_up = int(shortage.to_integral_value(rounding=ROUND_CEILING))
    full_batches = int((Decimal(batches) * mb_weight).to_integral_value(rounding=ROUND_FLOOR))
    return max(min(rounded_up, full_batches), 1), batches


def _mb_availability(
    plan: CompoundingPlan,
    compound_code: str,
    *,
    spec: Optional[Dict[str, Any]] = None,
    demand: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """
    MB position for a compound.

    MB required comes from FB batches x MB weight (not the CRIN quantity, which
    also includes chemicals). Stock and MB already on the board are subtracted
    before suggesting new MB. FB on the board reserves stock until actuals are saved.
    """
    spec = spec if spec is not None else _sheet_spec_for_compound(plan, compound_code)
    demand = demand if demand is not None else _mb_demand_by_compound(plan)
    entry = demand.get(_normalize_compound_code(compound_code)) or {}
    on_board_fb = int(entry.get("on_board_fb") or 0)
    incoming_mb = int(entry.get("incoming_mb") or 0)
    waiting_fb = int(entry.get("waiting_fb") or 0)
    mb_weight = spec.get("mb_weight")
    mb_stock = spec.get("mb_stock")

    result: Dict[str, Any] = {
        "known": False,
        "on_board_fb": on_board_fb,
        "waiting_fb": waiting_fb,
        "open_fb": on_board_fb + waiting_fb,
        "incoming_mb": incoming_mb,
        "mb_stock": mb_stock,
        "required": None,
        "reserved": None,
        "free": None,
        "incoming": None,
        "shortage": None,
        "suggested_quantity": 0,
        "suggested_batches": 0,
    }
    if mb_weight is None or mb_weight <= 0 or mb_stock is None:
        return result

    required = Decimal(on_board_fb + waiting_fb) * mb_weight
    reserved = Decimal(on_board_fb) * mb_weight
    incoming = Decimal(incoming_mb) * mb_weight
    shortage = max(required - mb_stock - incoming, Decimal("0"))
    suggested_quantity, suggested_batches = _suggested_mb_plan(shortage, mb_weight)
    result.update(
        {
            "known": True,
            "required": required,
            "reserved": reserved,
            "free": max(mb_stock - reserved, Decimal("0")),
            "incoming": incoming,
            "shortage": shortage,
            "suggested_quantity": suggested_quantity,
            "suggested_batches": suggested_batches,
        }
    )
    return result


def _mb_availability_display(availability: Dict[str, Any]) -> Dict[str, Any]:
    def text(value: Optional[Decimal]) -> str:
        return _decimal_text(value) if value is not None else "—"

    return {
        "known": availability["known"],
        "open_fb": availability["open_fb"],
        "required": text(availability["required"]),
        "stock": text(availability["mb_stock"]),
        "reserved": text(availability["reserved"]),
        "free": text(availability["free"]),
        "incoming": text(availability["incoming"]),
        "shortage": text(availability["shortage"]),
        "has_shortage": bool(availability["shortage"] and availability["shortage"] > 0),
        "suggested_quantity": availability["suggested_quantity"],
        "suggested_batches": availability["suggested_batches"],
    }


def _board_section_rows(lines: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    """
    Rows for the MB or FB section of the board.

    A board line saves MB and FB together, so a line holding both appears in each
    section and shares one form, which the MB section renders.
    """
    rows: List[Dict[str, Any]] = []
    for line in lines:
        has_mb = bool(line["planned_mb"] or line["actual_mb"])
        has_fb = bool(line["planned_fb"] or line["actual_fb"])
        if kind == "fb":
            if not has_fb:
                continue
            owns_form = not has_mb
        else:
            if has_fb and not has_mb:
                continue
            owns_form = True
        rows.append(
            {
                "id": line["id"],
                "compound_code": line["compound_code"],
                "crin_number": line["crin_number"],
                "shift1": line[f"shift1_{kind}"],
                "shift2": line[f"shift2_{kind}"],
                "shift3": line[f"shift3_{kind}"],
                "planned": line[f"planned_{kind}"],
                "planned_weight": line[f"planned_{kind}_weight"],
                "actual": line[f"actual_{kind}"],
                "actual_weight": line[f"actual_{kind}_weight"],
                "remaining": line[f"remaining_{kind}"],
                "remaining_weight": line[f"remaining_{kind}_weight"],
                "actual_field": f"actual_{kind}",
                "actuals_saved": line["actuals_saved"],
                "is_mixed": has_mb and has_fb,
                "owns_form": owns_form,
            }
        )
    return rows


def _negative_mb_stock(plan: CompoundingPlan) -> Dict[str, tuple[str, Decimal]]:
    """Return {compound_key: (code, stock)} for every compound with MB stock below zero."""
    balances = stock_balances()
    negative: Dict[str, tuple[str, Decimal]] = {}
    seen: set[str] = set()
    sheet = _master_sheet_for_plan(plan)
    if sheet:
        headers, rows, cols = _sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
        code_idx = cols.get("compound_code")
        stock_idx = cols.get("mb_stock")
        if code_idx is not None:
            for idx, _raw in enumerate(rows):
                row = _get_sheet_row(rows, idx, len(headers))
                code = _clean_compound_code(row[code_idx])
                key = _normalize_compound_code(code)
                if not key or key in seen:
                    continue
                seen.add(key)
                sheet_stock = _to_decimal(row[stock_idx]) if stock_idx is not None else None
                stock = (balances.get(key) or {}).get(CompoundingStockEntry.STOCK_MB, sheet_stock)
                if stock is not None and stock < 0:
                    negative[key] = (code, stock)
    for key, ledger in balances.items():
        stock = ledger.get(CompoundingStockEntry.STOCK_MB)
        if key in seen or stock is None or stock >= 0:
            continue
        code = (
            CompoundingStockEntry.objects.filter(compound_key=key)
            .order_by("-id")
            .values_list("compound_code", flat=True)
            .first()
        )
        negative[key] = (code or key, stock)
    return negative


def _mb_shortage_alerts(
    plan: CompoundingPlan,
    *,
    demand: Optional[Dict[str, Dict[str, int]]] = None,
) -> List[Dict[str, Any]]:
    """Compounds whose MB stock is negative, or too low for their open FB."""
    demand = demand if demand is not None else _mb_demand_by_compound(plan)
    negative_stock = _negative_mb_stock(plan)
    codes: Dict[str, str] = {}
    for code in CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN).values_list("compound_code", flat=True):
        codes.setdefault(_normalize_compound_code(code), code)
    for code in CompoundingLine.objects.filter(actuals_saved=False).values_list("compound_code", flat=True):
        codes.setdefault(_normalize_compound_code(code), code)

    alerts: List[Dict[str, Any]] = []
    for key, code in codes.items():
        if not key:
            continue
        availability = _mb_availability(plan, code, demand=demand)
        if availability["open_fb"] <= 0:
            continue
        if not availability["known"]:
            alerts.append({"compound_code": code, "known": False, "message": f"MB stock or weight is missing for {code}."})
            continue
        required = availability["required"]
        stock = availability["mb_stock"]
        if stock >= required:
            continue
        # A negative balance must be made up as well, so it adds to the shortage.
        short = required - stock
        is_negative = stock < 0
        lead = f"MB stock is negative for {code}" if is_negative else f"MB stock is not available for {code}"
        alerts.append(
            {
                "compound_code": code,
                "known": True,
                "negative": is_negative,
                "required": _decimal_text(required),
                "stock": _decimal_text(stock),
                "short": _decimal_text(short),
                "incoming": _decimal_text(availability["incoming"]) if availability["incoming"] > 0 else "",
                "message": (
                    f"{lead}. Needed {_decimal_text(required)}, in stock {_decimal_text(stock)}, "
                    f"short by {_decimal_text(short)}. Plan MB first."
                ),
            }
        )

    alerted = {_normalize_compound_code(item["compound_code"]) for item in alerts}
    for key, (code, stock) in negative_stock.items():
        if key in alerted:
            continue
        alerts.append(
            {
                "compound_code": code,
                "known": True,
                "negative": True,
                "required": "",
                "stock": _decimal_text(stock),
                "short": _decimal_text(-stock),
                "incoming": "",
                "message": (
                    f"MB stock is negative for {code} ({_decimal_text(stock)}). "
                    "Check the entries or upload a stock count."
                ),
            }
        )
    alerts.sort(key=lambda item: item["compound_code"])
    return alerts


def _recompute_master_sheet_stock_for_plan(
    plan: CompoundingPlan,
    *,
    focus_compound: str = "",
    base_from_current_when_missing: bool = False,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "updated": False,
        "reason": "",
        "mb_stock": None,
        "fb_stock": None,
    }
    sheet = CompoundingMasterSheet.objects.filter(plan=plan).first()
    if not sheet:
        result["reason"] = "master_sheet_missing"
        return result

    headers, rows, columns = _sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
    if not headers:
        result["reason"] = "sheet_empty"
        return result
    if columns.get("compound_code") is None:
        result["reason"] = "compound_column_missing"
        return result
    if columns.get("mb_stock") is None and columns.get("fb_stock") is None:
        result["reason"] = "stock_columns_missing"
        return result

    actual_totals = _actual_totals_by_compound(plan)

    base_headers, base_rows, _ = _sheet_table_with_detected_headers(
        list(sheet.base_headers or []),
        list(sheet.base_rows or []),
    )
    current_sig = _header_signature(headers)
    base_sig = _header_signature(base_headers)
    base_needs_rebuild = (not base_headers) or (base_sig != current_sig) or (len(base_rows) != len(rows))
    if base_needs_rebuild:
        base_headers = list(headers)
        if base_from_current_when_missing and not base_sig:
            base_rows = [_get_sheet_row(rows, idx, len(headers)) for idx, _ in enumerate(rows)]
        else:
            base_rows = _derive_base_rows(headers=headers, rows=rows, columns=columns, actual_totals=actual_totals)

    recomputed_rows = _recompute_rows_from_base(
        headers=headers,
        base_rows=base_rows,
        columns=columns,
        actual_totals=actual_totals,
    )

    update_fields: List[str] = ["headers", "rows", "updated_at"]
    sheet.headers = headers
    sheet.rows = recomputed_rows
    if base_needs_rebuild:
        sheet.base_headers = base_headers
        sheet.base_rows = base_rows
        update_fields.extend(["base_headers", "base_rows"])
    sheet.save(update_fields=update_fields)
    result["updated"] = True

    focus_key = _normalize_compound_code(focus_compound) if focus_compound else ""
    if focus_key and columns.get("compound_code") is not None:
        code_idx = int(columns["compound_code"])
        mb_idx = columns.get("mb_stock")
        fb_idx = columns.get("fb_stock")
        for idx, _ in enumerate(recomputed_rows):
            row = _get_sheet_row(recomputed_rows, idx, len(headers))
            if _normalize_compound_code(row[code_idx]) != focus_key:
                continue
            if mb_idx is not None and mb_idx < len(row):
                result["mb_stock"] = row[mb_idx]
            if fb_idx is not None and fb_idx < len(row):
                result["fb_stock"] = row[fb_idx]
            break
    return result


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def board(request):
    return render(
        request,
        "magicwand/compounding_home.html",
        {
            "can_planning": user_has_service_access(request.user, "compounding_planning"),
            "can_board": user_has_service_access(request.user, "compounding_board"),
            "can_raise_crin": any(user_has_service_access(request.user, slug) for slug in _PRODUCTION_SERVICES),
            "show_iot_dashboard": _iot_dashboard_available(),
        },
    )


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def production_report(request):
    period = _report_period(request.GET.get("period") or "")
    anchor = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    report = _build_compounding_production_report(period=period, anchor=anchor)

    return render(
        request,
        "magicwand/compounding_production_report.html",
        {
            "report": report,
            "period": report["period"],
            "period_options": [{"value": value, "label": label} for value, label in _REPORT_PERIODS.items()],
            "anchor_date": anchor,
            "start_date": report["start_date"],
            "end_date": report["end_date"],
            "can_planning": user_has_service_access(request.user, "compounding_planning"),
            "can_board": user_has_service_access(request.user, "compounding_board"),
            "show_iot_dashboard": _iot_dashboard_available(),
        },
    )


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def production_report_csv(request):
    period = _report_period(request.GET.get("period") or "")
    anchor = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    report = _build_compounding_production_report(period=period, anchor=anchor)
    filename = (
        f"compounding-production-{report['period']}-"
        f"{report['start_date'].isoformat()}-to-{report['end_date'].isoformat()}.csv"
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)

    overall = report["overall"]
    writer.writerow(["Compounding Production Report"])
    writer.writerow(["Period", report["period_label"]])
    writer.writerow(["From", report["start_date"].isoformat()])
    writer.writerow(["To", report["end_date"].isoformat()])
    writer.writerow(["Plans", overall.get("plan_count", 0)])
    writer.writerow(["Board lines", overall["line_count"]])
    writer.writerow(["Saved lines", overall["saved_line_count"]])
    writer.writerow(["Planned MB", overall["planned_mb"]])
    writer.writerow(["Planned FB", overall["planned_fb"]])
    writer.writerow(["Actual MB", overall["actual_mb"]])
    writer.writerow(["Actual FB", overall["actual_fb"]])
    writer.writerow(["MB consumed", overall["mb_consumed"]])
    writer.writerow(["Returned MB", overall["returned_mb"]])
    writer.writerow(["Returned FB", overall["returned_fb"]])
    writer.writerow(["Remaining MB", overall["remaining_mb"]])
    writer.writerow(["Remaining FB", overall["remaining_fb"]])
    writer.writerow(["Completion", overall["completion_display"]])
    writer.writerow([])

    writer.writerow(["Daily totals"])
    writer.writerow([
        "Date",
        "CJC",
        "Lines",
        "Saved",
        "Planned MB",
        "Planned FB",
        "Actual MB",
        "Actual FB",
        "MB consumed",
        "Returned MB",
        "Returned FB",
        "Remaining MB",
        "Remaining FB",
        "Completion",
    ])
    for item in report["daily_rows"]:
        writer.writerow([
            item["plan_date"].isoformat(),
            item["cjc_numbers"],
            item["line_count"],
            item["saved_line_count"],
            item["planned_mb"],
            item["planned_fb"],
            item["actual_mb"],
            item["actual_fb"],
            item["mb_consumed"],
            item["returned_mb"],
            item["returned_fb"],
            item["remaining_mb"],
            item["remaining_fb"],
            item["completion_display"],
        ])
    writer.writerow([])

    writer.writerow(["Compound totals"])
    writer.writerow([
        "Compound",
        "Active days",
        "Lines",
        "Saved",
        "Planned MB",
        "Planned FB",
        "Actual MB",
        "Actual FB",
        "MB consumed",
        "Returned MB",
        "Returned FB",
        "Remaining MB",
        "Remaining FB",
        "Completion",
    ])
    for item in report["compound_rows"]:
        writer.writerow([
            item["compound_code"],
            item["active_day_count"],
            item["line_count"],
            item["saved_line_count"],
            item["planned_mb"],
            item["planned_fb"],
            item["actual_mb"],
            item["actual_fb"],
            item["mb_consumed"],
            item["returned_mb"],
            item["returned_fb"],
            item["remaining_mb"],
            item["remaining_fb"],
            item["completion_display"],
        ])
    writer.writerow([])

    writer.writerow(["Board line detail"])
    writer.writerow([
        "Date",
        "CJC",
        "Compound",
        "Status",
        "Shift 1 MB",
        "Shift 1 FB",
        "Shift 2 MB",
        "Shift 2 FB",
        "Shift 3 MB",
        "Shift 3 FB",
        "Planned MB",
        "Planned FB",
        "Actual MB",
        "Actual FB",
        "MB consumed",
        "Returned MB",
        "Returned FB",
        "Remaining MB",
        "Remaining FB",
        "Completion",
    ])
    for row in report["line_rows"]:
        writer.writerow([
            row["plan_date"].isoformat(),
            row["cjc_number"],
            row["compound_code"],
            row["status_label"],
            row["shift1_mb"],
            row["shift1_fb"],
            row["shift2_mb"],
            row["shift2_fb"],
            row["shift3_mb"],
            row["shift3_fb"],
            row["planned_mb"],
            row["planned_fb"],
            row["actual_mb"],
            row["actual_fb"],
            row["mb_consumed"],
            row["returned_mb"],
            row["returned_fb"],
            row["remaining_mb"],
            row["remaining_fb"],
            row["completion_display"],
        ])
    return response


_MAX_RECORD_UPLOAD_BYTES = 10 * 1024 * 1024


def _record_url(month: date) -> str:
    return f"{reverse('compounding:production_record')}?month={month:%Y-%m}"


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def production_record(request):
    month = parse_month(request.GET.get("month") or "") or compounding_business_date().replace(day=1)
    # Same as the Excel sheet: every compound is listed unless the user narrows it.
    show_all = request.GET.get("show") != "active"
    return render(
        request,
        "magicwand/compounding_record.html",
        {
            "report": build_month_report(month, show_all=show_all),
            "month_value": f"{month:%Y-%m}",
            "month_options": [{"value": f"{item:%Y-%m}", "label": f"{item:%B %Y}"} for item in available_months()],
            "show_all": show_all,
            "can_import": user_has_service_access(request.user, "compounding_planning"),
            "can_edit_all": user_has_service_access(request.user, "compounding_planning"),
        },
    )


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"], deny="json")
@require_http_methods(["POST"])
def save_record_grid(request):
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid data. Reload the page."}, status=400)
    month = parse_month(str(payload.get("month") or "")) if isinstance(payload, dict) else None
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if month is None or not isinstance(changes, list):
        return JsonResponse({"error": "Invalid data. Reload the page."}, status=400)
    try:
        saved = apply_record_changes(
            month,
            changes,
            user=request.user,
            can_edit_all=user_has_service_access(request.user, "compounding_planning"),
        )
    except RecordSaveError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "saved": saved})


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def download_production_record(request):
    month = parse_month(request.GET.get("month") or "") or compounding_business_date().replace(day=1)
    workbook = month_workbook(build_month_report(month, show_all=True))
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="compounding-production-record-{month:%Y-%m}.xlsx"'
    workbook.save(response)
    return response


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["POST"])
def save_record_scrap(request):
    month = parse_month(request.POST.get("month") or "")
    if month is None:
        messages.error(request, "Select a valid month.")
        return redirect("compounding:production_record")
    _start, end = month_bounds(month)
    entries: Dict[date, Optional[Decimal]] = {}
    for day in range(1, end.day + 1):
        text = (request.POST.get(f"scrap_{day}") or "").strip()
        if not text:
            entries[month.replace(day=day)] = None
            continue
        try:
            quantity = Decimal(text)
        except InvalidOperation:
            quantity = Decimal("-1")
        if (
            not quantity.is_finite()
            or quantity < 0
            or quantity > Decimal("9999999")
            or quantity != quantity.quantize(Decimal("0.001"))
        ):
            messages.error(request, f"Day {day}: enter scrap as a number with up to 3 decimal places.")
            return redirect(_record_url(month))
        entries[month.replace(day=day)] = quantity if quantity > 0 else None

    with transaction.atomic():
        for entry_date, quantity in entries.items():
            if quantity is None:
                CompoundingDailyScrap.objects.filter(date=entry_date).delete()
            else:
                CompoundingDailyScrap.objects.update_or_create(
                    date=entry_date,
                    defaults={"quantity": quantity, "updated_by": request.user},
                )
    messages.success(request, f"Saved scrap for {month:%B %Y}.")
    return redirect(_record_url(month))


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def compound_record_summary(request):
    months = available_months()
    period = (request.GET.get("period") or "all").strip()
    selected = parse_month(period)
    chosen = [selected] if selected in months else months
    return render(
        request,
        "magicwand/compounding_record_summary.html",
        {
            "summary": build_compound_summary(chosen),
            "period": f"{selected:%Y-%m}" if selected in months else "all",
            "month_options": [{"value": f"{item:%Y-%m}", "label": f"{item:%B %Y}"} for item in months],
        },
    )


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def import_production_record(request):
    upload = request.FILES.get("record_file")
    back = request.POST.get("month") or ""
    redirect_url = reverse("compounding:production_record") + (f"?month={back}" if parse_month(back) else "")
    if not upload:
        messages.error(request, "Select the Excel production record to import.")
        return redirect(redirect_url)
    filename = (upload.name or "").strip()
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        messages.error(request, "Only .xlsx or .xlsm files are supported.")
        return redirect(redirect_url)
    if upload.size > _MAX_RECORD_UPLOAD_BYTES:
        messages.error(request, "The Excel file must be 10 MB or smaller.")
        return redirect(redirect_url)
    try:
        results = import_workbook(upload, user=request.user, filename=filename)
    except RecordImportError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_url)
    names = ", ".join(f"{item['month']:%B %Y}" for item in results)
    messages.success(request, f"Imported {names}. Live stock was not changed.")
    return redirect(_record_url(results[-1]["month"]))


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def stock_ledger(request):
    balances = stock_balances()
    summary: Dict[str, Dict[str, Any]] = {}

    def add_row(key: str, code: str, sheet_mb: Optional[Decimal] = None, sheet_fb: Optional[Decimal] = None) -> None:
        if not key or key in summary:
            return
        ledger = balances.get(key) or {}
        mb_stock = ledger.get(CompoundingStockEntry.STOCK_MB, sheet_mb)
        fb_stock = ledger.get(CompoundingStockEntry.STOCK_FB, sheet_fb)
        summary[key] = {
            "compound_code": code,
            "mb_stock": _decimal_text(mb_stock) if mb_stock is not None else "—",
            "fb_stock": _decimal_text(fb_stock) if fb_stock is not None else "—",
            "mb_negative": bool(mb_stock is not None and mb_stock < 0),
            "fb_negative": bool(fb_stock is not None and fb_stock < 0),
            "from_ledger": bool(ledger),
        }

    sheet = CompoundingMasterSheet.objects.order_by("-plan__plan_date", "-updated_at").first()
    if sheet:
        headers, rows, cols = _sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
        code_idx = cols.get("compound_code")
        if code_idx is not None:
            for idx, _raw in enumerate(rows):
                row = _get_sheet_row(rows, idx, len(headers))
                code = _clean_compound_code(row[code_idx])
                add_row(
                    _normalize_compound_code(code),
                    code,
                    _to_decimal(row[cols["mb_stock"]]) if cols.get("mb_stock") is not None else None,
                    _to_decimal(row[cols["fb_stock"]]) if cols.get("fb_stock") is not None else None,
                )
    known_codes = (
        CompoundingStockEntry.objects.order_by("compound_key", "-id").values_list("compound_key", "compound_code")
    )
    for key, code in known_codes:
        add_row(key, code)

    selected_key = _normalize_compound_code(request.GET.get("compound") or "")
    selected_row = summary.get(selected_key)
    entries: List[Dict[str, Any]] = []
    if selected_row:
        running = {CompoundingStockEntry.STOCK_MB: Decimal("0"), CompoundingStockEntry.STOCK_FB: Decimal("0")}
        history = (
            CompoundingStockEntry.objects.filter(compound_key=selected_key)
            .select_related("plan", "created_by")
            .order_by("created_at", "id")
        )
        for entry in history:
            running[entry.stock_type] += entry.quantity
            entries.append(
                {
                    "created_at": entry.created_at.astimezone(IST_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                    "stock_type": entry.get_stock_type_display(),
                    "entry_type": entry.get_entry_type_display(),
                    "batches": entry.batches,
                    "quantity": _decimal_text(entry.quantity),
                    "is_increase": entry.quantity > 0,
                    "balance": _decimal_text(running[entry.stock_type]),
                    "plan_date": entry.plan.plan_date if entry.plan else None,
                    "cjc_number": entry.plan.cjc_number if entry.plan else None,
                    "user": entry.created_by.get_username() if entry.created_by else "",
                    "note": entry.note,
                }
            )
        entries.reverse()

    return render(
        request,
        "magicwand/compounding_stock.html",
        {
            "rows": list(summary.values()),
            "negative_rows": [row for row in summary.values() if row["mb_negative"] or row["fb_negative"]],
            "selected_compound": selected_row["compound_code"] if selected_row else "",
            "selected_row": selected_row,
            "entries": entries[:200],
            "entries_total": len(entries),
        },
    )


def _crin_stage(
    *,
    crin: CompoundingCRIN,
    issued: Decimal,
    made: Decimal,
    on_board: bool,
    batches_done: bool,
    mb_short: bool,
) -> tuple[str, str]:
    """Return (label, Bootstrap colour) for where a CRIN is in the flow."""
    if crin.status == CompoundingCRIN.STATUS_CLOSED:
        return "Closed", "secondary"
    if issued > 0:
        return "Part Issued", "info"
    if on_board:
        return "In Production", "primary"
    if batches_done:
        return "FB Ready", "success"
    if mb_short:
        return "MB Pending", "danger"
    if crin.fb_planning is not None or made > 0:
        return "FB Planned", "warning"
    return "Raised", "light"


def _safe_next(request) -> str:
    """Return a posted Compounding page to come back to, or "" if it is not a safe local path."""
    value = (request.POST.get("next") or "").strip()
    if value.startswith("/compounding/") and "//" not in value and "\\" not in value:
        return value
    return ""


WORK_STAGES = [
    ("mb", "Waiting for MB", "Plan and make MB first.", "danger"),
    ("fb", "Ready for FB", "MB is available. Send FB to the board.", "primary"),
    ("board", "On the board", "Save actuals on the Compounding Board when made.", "warning"),
    ("issue", "Ready to issue", "FB is made. Issue it to Production.", "success"),
]


WORK_RANGES = {
    "today": "Today",
    "week": "Last 7 days",
    "all": "All open",
}


def _work_since(range_key: str) -> Optional[date]:
    today = compounding_business_date()
    if range_key == "week":
        return today - timedelta(days=6)
    if range_key == "all":
        return None
    return today


def _work_queue(since: Optional[date] = None) -> Dict[str, Any]:
    """
    Open CRINs in the step each needs next, oldest first.

    MB is shared out across every open CRIN so older ones are served first, then only
    the CRINs from `since` onward are shown. Older ones are counted, not lost.
    """
    crins = list(
        CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN)
        .select_related("plan")
        .order_by("plan__plan_date", "created_at", "id")
    )
    status_rows = {row["id"]: row for row in _crin_status_rows(crins)}
    board_lines: Dict[int, List[CompoundingLine]] = {}
    for line in CompoundingLine.objects.filter(crin_request__in=crins, actuals_saved=False).select_related("plan"):
        board_lines.setdefault(line.crin_request_id, []).append(line)

    # MB on the board and FB reserving MB, across every date: MB sent here goes on today's board.
    board_fb: Dict[str, int] = {}
    board_mb: Dict[str, int] = {}
    for line in CompoundingLine.objects.filter(actuals_saved=False):
        key = _normalize_compound_code(line.compound_code)
        planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
        planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
        board_fb[key] = board_fb.get(key, 0) + max(planned_fb - int(line.actual_fb or 0) - int(line.returned_to_planning_fb or 0), 0)
        board_mb[key] = board_mb.get(key, 0) + max(planned_mb - int(line.actual_mb or 0) - int(line.returned_to_planning_mb or 0), 0)
    free_left: Dict[str, Decimal] = {}
    incoming_left: Dict[str, Decimal] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {key: [] for key, *_rest in WORK_STAGES}

    for crin in crins:
        status = status_rows[crin.id]
        item = dict(status)
        item["plan_date"] = crin.plan.plan_date
        if crin.id in board_lines:
            shifts = []
            for line in board_lines[crin.id]:
                for number in (1, 2, 3):
                    fb = getattr(line, f"shift{number}_fb")
                    mb = getattr(line, f"shift{number}_mb")
                    if fb or mb:
                        parts = [f"{fb} FB" if fb else "", f"{mb} MB" if mb else ""]
                        shifts.append(f"Shift {number}: " + ", ".join(part for part in parts if part))
                item["board_date"] = board_lines[crin.id][0].plan.plan_date
            item["detail"] = "; ".join(shifts) or "On the board"
            groups["board"].append(item)
            continue
        if status["stage_label"] in ("FB Ready", "Part Issued"):
            groups["issue"].append(item)
            continue

        spec = _sheet_spec_for_compound(crin.plan, crin.compound_code)
        fb_weight = spec.get("fb_total_weight")
        mb_weight = spec.get("mb_weight")
        batches = int(crin.fb_planning) if crin.fb_planning is not None else _compute_batches(crin.quantity, fb_weight)
        item["fb_batches"] = batches
        if batches <= 0:
            groups["fb"].append(item)
            continue
        needed = _fb_required_mb_stock(fb_batches=batches, mb_weight=mb_weight)
        key = _normalize_compound_code(crin.compound_code)
        stock = spec.get("mb_stock")
        if needed is None or stock is None or not mb_weight:
            item["problem"] = "MB weight or stock is missing in the master sheet."
            groups["mb"].append(item)
            continue
        if key not in free_left:
            free_left[key] = max(stock - Decimal(board_fb.get(key, 0)) * mb_weight, Decimal("0"))
            incoming_left[key] = Decimal(board_mb.get(key, 0)) * mb_weight
        left = free_left[key]
        item["mb_needed"] = _decimal_text(needed)
        if left >= needed:
            free_left[key] = left - needed
            groups["fb"].append(item)
            continue
        short = needed - left
        free_left[key] = Decimal("0")
        coming = incoming_left[key]
        item["mb_available"] = _decimal_text(left)
        item["mb_short"] = _decimal_text(short)
        if coming >= short:
            incoming_left[key] = coming - short
            item["mb_on_board"] = True
        else:
            incoming_left[key] = Decimal("0")
            remaining = short - coming
            item["mb_on_board_part"] = _decimal_text(coming) if coming > 0 else ""
            item["suggested_batches"] = int((remaining / mb_weight).to_integral_value(rounding=ROUND_CEILING)) if mb_weight else 0
        groups["mb"].append(item)

    def is_recent(item: Dict[str, Any]) -> bool:
        if since is None:
            return True
        return item["plan_date"] >= since or bool(item.get("board_date") and item["board_date"] >= since)

    all_count = sum(len(items) for items in groups.values())
    if since is not None:
        groups = {key: [item for item in items if is_recent(item)] for key, items in groups.items()}
    stages = [
        {"key": key, "title": title, "hint": hint, "color": color, "items": groups[key]}
        for key, title, hint, color in WORK_STAGES
    ]
    shown = sum(len(items) for items in groups.values())
    return {"stages": stages, "total": shown, "hidden": all_count - shown}


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def work_queue(request):
    range_key = (request.GET.get("range") or "today").strip().lower()
    if range_key not in WORK_RANGES:
        range_key = "today"
    return render(
        request,
        "magicwand/compounding_work.html",
        {
            "queue": _work_queue(_work_since(range_key)),
            "can_planning": user_has_service_access(request.user, "compounding_planning"),
            "today": compounding_business_date(),
            "range_key": range_key,
            "range_options": [{"key": key, "label": label} for key, label in WORK_RANGES.items()],
            "back_url": f"{reverse('compounding:work_queue')}?range={range_key}",
        },
    )


def _crin_status_rows(crin_objs: List[CompoundingCRIN]) -> List[Dict[str, Any]]:
    crin_ids = [crin.id for crin in crin_objs]
    totals = crin_fb_totals(crin_ids)
    on_board_ids: set[int] = set()
    done_ids: set[int] = set()
    for crin_id, saved in CompoundingLine.objects.filter(crin_request_id__in=crin_ids).values_list(
        "crin_request_id", "actuals_saved"
    ):
        (done_ids if saved else on_board_ids).add(crin_id)

    demand_by_plan: Dict[int, Dict[str, Dict[str, int]]] = {}
    rows: List[Dict[str, Any]] = []
    for crin in crin_objs:
        crin_totals = totals.get(crin.id) or {}
        issued = crin_totals.get("issued", Decimal("0"))
        made = crin_totals.get("made", Decimal("0"))
        ordered = Decimal(crin.order_quantity)
        to_issue = max(ordered - issued, Decimal("0"))
        spec = _sheet_spec_for_compound(crin.plan, crin.compound_code)
        fb_stock = spec.get("fb_stock")

        mb_short = False
        if crin.status == CompoundingCRIN.STATUS_OPEN and crin.id not in on_board_ids and crin.id not in done_ids:
            fb_weight = spec.get("fb_total_weight")
            batches = (
                int(crin.fb_planning)
                if crin.fb_planning is not None
                else _compute_batches(crin.quantity, fb_weight)
            )
            needed = _fb_required_mb_stock(fb_batches=batches, mb_weight=spec.get("mb_weight"))
            if crin.plan_id not in demand_by_plan:
                demand_by_plan[crin.plan_id] = _mb_demand_by_compound(crin.plan)
            free = _mb_availability(crin.plan, crin.compound_code, spec=spec, demand=demand_by_plan[crin.plan_id])["free"]
            mb_short = batches > 0 and (needed is None or free is None or free < needed)

        stage_label, stage_color = _crin_stage(
            crin=crin,
            issued=issued,
            made=made,
            on_board=crin.id in on_board_ids,
            batches_done=crin.id in done_ids,
            mb_short=mb_short,
        )
        max_issue = max(min(to_issue, fb_stock), Decimal("0")) if fb_stock is not None else Decimal("0")
        rows.append(
            {
                "id": crin.id,
                "crin_number": crin.display_number,
                "full_number": crin.crin_number,
                "compound_code": crin.compound_code,
                "plan_date": crin.plan.plan_date,
                "stage_label": stage_label,
                "stage_color": stage_color,
                "ordered": _decimal_text(ordered),
                "made": _decimal_text(made),
                "issued": _decimal_text(issued),
                "issued_raw": str(issued),
                "to_issue": _decimal_text(to_issue),
                "fb_stock": _decimal_text(fb_stock) if fb_stock is not None else "—",
                "max_issue": _decimal_text(max_issue),
                "can_issue": crin.status == CompoundingCRIN.STATUS_OPEN and max_issue > 0,
                "is_open": crin.status == CompoundingCRIN.STATUS_OPEN,
            }
        )
    return rows


def _production_compounds(plan_date: date) -> List[str]:
    # Reading the list must not create a plan, because that would use up a CJC number.
    plan = CompoundingPlan.objects.filter(plan_date=plan_date).first()
    if plan:
        return _available_master_compounds(plan)
    sheet = (
        CompoundingMasterSheet.objects.filter(plan__plan_date__lte=plan_date)
        .order_by("-plan__plan_date", "-updated_at")
        .first()
    )
    return _compounds_from_sheet(sheet)


def _production_request_groups() -> List[Dict[str, Any]]:
    crins = CompoundingCRIN.objects.select_related("plan", "request", "request__created_by", "created_by")
    open_crins = list(crins.filter(status=CompoundingCRIN.STATUS_OPEN))
    closed_crins = list(crins.filter(status=CompoundingCRIN.STATUS_CLOSED).order_by("-created_at", "-id")[:50])
    crin_objs = sorted(open_crins + closed_crins, key=lambda crin: (crin.created_at, crin.id), reverse=True)
    rows_by_id = {row["id"]: row for row in _crin_status_rows(crin_objs)}

    groups: Dict[str, Dict[str, Any]] = {}
    for crin in crin_objs:
        header = crin.request
        key = f"request-{header.id}" if header else f"crin-{crin.id}"
        if key not in groups:
            raised_at = header.created_at if header else crin.created_at
            raised_by = header.created_by if header else crin.created_by
            groups[key] = {
                "number": header.request_number if header else crin.display_number,
                "raised_at": raised_at.astimezone(IST_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                "raised_by": raised_by.get_username() if raised_by else "—",
                "note": header.note if header else "",
                "items": [],
            }
        groups[key]["items"].append(rows_by_id[crin.id])

    result = list(groups.values())
    for group in result:
        group["items"].sort(key=lambda row: row["full_number"])
        group["item_count"] = len(group["items"])
        group["closed_count"] = sum(1 for row in group["items"] if not row["is_open"])
        group["is_complete"] = group["closed_count"] == group["item_count"]
    return result


def _render_production(
    request,
    *,
    draft: Optional[List[Dict[str, str]]] = None,
    note: str = "",
    submission_id: str = "",
):
    compounds = _production_compounds(compounding_business_date())
    draft_rows = list(draft or [])
    while len(draft_rows) < 4:
        draft_rows.append({"compound_code": "", "quantity": ""})
    return render(
        request,
        "magicwand/compounding_production.html",
        {
            "compound_options": compounds,
            "draft_rows": draft_rows,
            "note": note,
            "submission_id": submission_id or str(uuid.uuid4()),
            "groups": _production_request_groups(),
        },
    )


@login_required
@require_any_service_access(_PRODUCTION_SERVICES)
@require_http_methods(["GET"])
def production_portal(request):
    return _render_production(request)


@login_required
@require_any_service_access(_PRODUCTION_SERVICES)
@require_http_methods(["POST"])
def raise_crin_request(request):
    codes = request.POST.getlist("compound_code")
    quantities = request.POST.getlist("quantity")
    note = (request.POST.get("note") or "").strip()[:255]
    submission_text = (request.POST.get("submission_id") or "").strip()
    draft = [
        {"compound_code": (code or "").strip(), "quantity": (quantity or "").strip()}
        for code, quantity in zip(codes, quantities)
    ]

    def reject(message: str):
        messages.error(request, message)
        return _render_production(request, draft=draft, note=note, submission_id=submission_text)

    try:
        submission_id = uuid.UUID(submission_text)
    except ValueError:
        submission_text = ""
        return reject("This form has expired. Check the entries and submit again.")

    existing = CompoundingCRINRequest.objects.filter(submission_id=submission_id).first()
    if existing:
        messages.info(request, f"{existing.request_number} was already raised.")
        return redirect("compounding:production")

    available = {_normalize_compound_code(code): code for code in _production_compounds(compounding_business_date())}
    if not available:
        return reject("The master sheet is not uploaded yet. Ask Compounding to upload it.")

    items: List[tuple[str, int]] = []
    seen: set[str] = set()
    for row in draft:
        code = row["compound_code"]
        quantity_text = row["quantity"]
        if not code and not quantity_text:
            continue
        key = _normalize_compound_code(code)
        if key not in available:
            return reject("Select a compound for every row that has a quantity.")
        compound = available[key]
        if key in seen:
            return reject(f"{compound} is entered twice. Combine it into one row.")
        seen.add(key)
        if not re.fullmatch(r"[0-9]+", quantity_text) or not 0 < int(quantity_text) <= _MAX_CRIN_QUANTITY:
            return reject(f"Enter a whole-number quantity greater than 0 for {compound}.")
        items.append((compound, int(quantity_text)))

    if not items:
        return reject("Add at least one compound and quantity.")
    if len(items) > _MAX_CRIN_ITEMS:
        return reject(f"A CRIN can have up to {_MAX_CRIN_ITEMS} compounds.")

    try:
        with transaction.atomic():
            plan = _get_or_create_plan(compounding_business_date(), user=request.user)
            year = plan.plan_date.year
            request_number = f"CRIN-{year}-{next_certificate_serial('crin', year=year)}"
            header = CompoundingCRINRequest.objects.create(
                plan=plan,
                request_number=request_number,
                submission_id=submission_id,
                note=note,
                created_by=request.user,
            )
            for position, (compound, quantity) in enumerate(items, start=1):
                CompoundingCRIN.objects.create(
                    plan=plan,
                    request=header,
                    crin_number=f"{request_number}-{position}",
                    compound_folder=compound,
                    compound_code=compound,
                    quantity=quantity,
                    created_by=request.user,
                )
    except IntegrityError:
        existing = CompoundingCRINRequest.objects.filter(submission_id=submission_id).first()
        if existing is None:
            raise
        messages.info(request, f"{existing.request_number} was already raised.")
        return redirect("compounding:production")

    messages.success(request, f"Raised {request_number} with {len(items)} compound{'s' if len(items) != 1 else ''}.")
    return redirect("compounding:production")


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def fb_issue_page(request):
    show_closed = (request.GET.get("show") or "").strip().lower() == "closed"
    crins = CompoundingCRIN.objects.select_related("plan")
    if show_closed:
        crin_objs = list(crins.filter(status=CompoundingCRIN.STATUS_CLOSED).order_by("-updated_at", "-id")[:100])
    else:
        crin_objs = list(crins.filter(status=CompoundingCRIN.STATUS_OPEN).order_by("plan__plan_date", "id"))
    rows = _crin_status_rows(crin_objs)

    recent = (
        CompoundingStockEntry.objects.filter(entry_type=CompoundingStockEntry.ENTRY_FB_ISSUED)
        .select_related("crin", "created_by")
        .order_by("-created_at", "-id")[:30]
    )
    recent_issues = [
        {
            "created_at": entry.created_at.astimezone(IST_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
            "crin_number": entry.crin.display_number if entry.crin else "—",
            "compound_code": entry.compound_code,
            "quantity": _decimal_text(-entry.quantity),
            "user": entry.created_by.get_username() if entry.created_by else "—",
            "note": entry.note,
        }
        for entry in recent
    ]
    return render(
        request,
        "magicwand/compounding_fb_issue.html",
        {"rows": rows, "show_closed": show_closed, "recent_issues": recent_issues},
    )


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["POST"])
@transaction.atomic
def issue_fb(request, crin_id: int):
    redirect_url = _safe_next(request) or reverse("compounding:fb_issue")
    crin = get_object_or_404(CompoundingCRIN.objects.select_for_update().select_related("plan"), id=crin_id)
    if crin.status != CompoundingCRIN.STATUS_OPEN:
        messages.info(request, f"{crin.display_number} is already closed.")
        return redirect(redirect_url)

    try:
        quantity = Decimal((request.POST.get("quantity") or "").strip())
    except InvalidOperation:
        quantity = Decimal("0")
    if not quantity.is_finite() or quantity <= 0 or quantity != quantity.quantize(Decimal("0.001")):
        messages.error(request, "Enter an issue quantity greater than 0, with up to 3 decimal places.")
        return redirect(redirect_url)

    # Reject a repeated or out-of-date form so one issue cannot be recorded twice.
    issued_now = (crin_fb_totals([crin.id]).get(crin.id) or {}).get("issued", Decimal("0.000"))
    try:
        expected_issued = Decimal((request.POST.get("expected_issued") or "").strip())
    except InvalidOperation:
        expected_issued = None
    if expected_issued is None or not expected_issued.is_finite() or expected_issued != issued_now:
        messages.warning(request, f"{crin.display_number} was just updated. Check the balance and issue again if needed.")
        return redirect(redirect_url)

    spec = _sheet_spec_for_compound(crin.plan, crin.compound_code)
    try:
        result = record_fb_issue(
            crin=crin,
            quantity=quantity,
            user=request.user,
            sheet_fb_stock=spec.get("fb_stock"),
            note=(request.POST.get("note") or "").strip(),
        )
    except FBIssueError as exc:
        messages.error(request, str(exc))
        return redirect(redirect_url)

    if result["closed"]:
        messages.success(
            request,
            f"Issued {_decimal_text(quantity)} FB on {crin.display_number}. The CRIN is now closed.",
        )
    else:
        messages.success(
            request,
            f"Issued {_decimal_text(quantity)} FB on {crin.display_number}. "
            f"{_decimal_text(result['remaining'])} is left to issue.",
        )
    return redirect(redirect_url)


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["GET"])
def iot_dashboard(request):
    if not _iot_dashboard_available():
        raise Http404("Not found")

    is_local = bool(getattr(settings, "DEBUG", False))
    plan_date = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    machine_id = _iot_dashboard_machine_id()
    compound_options = _iot_run_compound_options(plan)
    iot_runs = [
        {
            "compound_code": run.compound_code,
            "batch_type": run.get_batch_type_display(),
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "time_taken": _iot_run_duration_text(run.started_at, run.ended_at),
            "stock_quality": run.get_stock_quality_display() if run.ended_at else "Running",
            "rejection_reason": run.get_rejection_reason_display(),
        }
        for run in plan.iot_runs.all().order_by("-started_at", "-id")[:50]
    ]
    open_batch = _open_iot_batch(machine_id)
    return render(
        request,
        "magicwand/iot_dashboard.html",
        {
            "page_kicker": "Local Testing" if is_local else "Production",
            "page_title": "Compounding IOT Dashboard",
            "page_description": (
                "Live local Modbus payloads posted into MagicWand for compounding-side testing."
                if is_local
                else "Live bridge-backed telemetry normalized by Django for the compounding dashboard."
            ),
            "back_url": "/compounding/",
            "back_label": "Back to Compounding",
            "iot_data_url": reverse("iot_dashboard_data"),
            "iot_summary_url": reverse("iot_dashboard_summary"),
            "iot_export_url": reverse("iot_history_export"),
            "iot_chart_max_points": getattr(settings, "IOT_CHART_MAX_POINTS", 5000),
            "plan": plan,
            "plan_date": plan.plan_date.isoformat(),
            "iot_machine_id": machine_id,
            "iot_run_compound_options": compound_options,
            "iot_runs": iot_runs,
            "iot_run_quality_choices": CompoundingIoTRun.QUALITY_CHOICES,
            "iot_batch_type_choices": CompoundingIoTRun.BATCH_TYPE_CHOICES,
            "iot_rejection_reason_choices": CompoundingIoTRun.REJECTION_REASON_CHOICES,
            "iot_batch_crin_options": _iot_batch_crin_options(),
            "iot_open_batch": open_batch,
            "iot_open_batch_weight": (
                _decimal_text(open_batch.batch_weight_kg) if open_batch and open_batch.batch_weight_kg else ""
            ),
            "iot_time_zone": getattr(settings, "IOT_DASHBOARD_TIME_ZONE", "Asia/Kolkata"),
            "iot_time_zone_label": getattr(settings, "IOT_DASHBOARD_TIME_ZONE", "Asia/Kolkata"),
        },
    )


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["POST"])
def save_iot_run(request):
    if not _iot_dashboard_available():
        raise Http404("Not found")

    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    compound_options = {item["value"] for item in _iot_run_compound_options(plan)}

    compound_code = (request.POST.get("compound_code") or "").strip()
    started_at = _parse_datetime_local(request.POST.get("started_at") or "")
    ended_at = _parse_datetime_local(request.POST.get("ended_at") or "")
    stock_quality = (request.POST.get("stock_quality") or "").strip().lower()
    notes = (request.POST.get("notes") or "").strip()
    machine_id = _iot_dashboard_machine_id()

    redirect_url = f"{reverse('compounding:iot_dashboard')}?date={plan.plan_date.isoformat()}"

    if not compound_code:
        messages.error(request, "Select the compound prepared for this run.")
        return redirect(redirect_url)
    if compound_options and compound_code not in compound_options:
        messages.error(request, "Select a valid compound from MB planning.")
        return redirect(redirect_url)
    if started_at is None or ended_at is None:
        messages.error(request, "Enter both start and end time for the run.")
        return redirect(redirect_url)
    if ended_at <= started_at:
        messages.error(request, "End time must be later than start time.")
        return redirect(redirect_url)
    if stock_quality not in {choice[0] for choice in CompoundingIoTRun.QUALITY_CHOICES}:
        messages.error(request, "Select the quality of stock produced.")
        return redirect(redirect_url)

    CompoundingIoTRun.objects.create(
        plan=plan,
        machine_id=machine_id,
        compound_code=compound_code,
        started_at=started_at,
        ended_at=ended_at,
        stock_quality=stock_quality,
        notes=notes[:2000],
        created_by=request.user,
    )
    messages.success(request, f"Saved IoT production context for {compound_code}.")
    return redirect(redirect_url)


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["POST"])
def start_iot_batch(request):
    """Operator taps "Start batch" as the batch goes on the machine; the start time is now."""
    if not _iot_dashboard_available():
        raise Http404("Not found")

    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    redirect_url = f"{reverse('compounding:iot_dashboard')}?date={plan.plan_date.isoformat()}"
    machine_id = _iot_dashboard_machine_id()

    compound_code = (request.POST.get("compound_code") or "").strip()
    batch_type = (request.POST.get("batch_type") or "").strip().lower()
    weight, weight_ok = _parse_batch_weight(request.POST.get("batch_weight_kg") or "")
    crin_id = (request.POST.get("crin_id") or "").strip()

    compound_options = {item["value"] for item in _iot_run_compound_options(plan)}
    if not compound_code:
        messages.error(request, "Select the compound going on the machine.")
        return redirect(redirect_url)
    if compound_options and compound_code not in compound_options:
        messages.error(request, "Select a valid compound from MB planning.")
        return redirect(redirect_url)
    if batch_type not in {choice[0] for choice in CompoundingIoTRun.BATCH_TYPE_CHOICES}:
        messages.error(request, "Select whether this is an MB or FB batch.")
        return redirect(redirect_url)
    if not weight_ok:
        messages.error(request, "Enter the batch weight in kg as a number above 0, or leave it blank.")
        return redirect(redirect_url)

    crin = None
    if crin_id:
        crin = CompoundingCRIN.objects.filter(pk=crin_id, status=CompoundingCRIN.STATUS_OPEN).first() if crin_id.isdigit() else None
        if crin is None:
            messages.error(request, "That CRIN is not open any more. Pick another one or leave it blank.")
            return redirect(redirect_url)

    with transaction.atomic():
        # Lock this machine's runs so two taps at once can't open two batches.
        running = (
            CompoundingIoTRun.objects.select_for_update()
            .filter(machine_id=machine_id, ended_at__isnull=True)
            .order_by("-started_at")
            .first()
        )
        if running is not None:
            messages.error(
                request,
                f"{running.compound_code} is still running on the machine. End that batch before starting a new one.",
            )
            return redirect(redirect_url)
        CompoundingIoTRun.objects.create(
            plan=plan,
            machine_id=machine_id,
            compound_code=compound_code,
            started_at=timezone.now(),
            ended_at=None,
            stock_quality="",
            batch_type=batch_type,
            batch_weight_kg=weight,
            crin=crin,
            created_by=request.user,
        )
    messages.success(request, f"Started a {batch_type.upper()} batch of {compound_code}.")
    return redirect(redirect_url)


@login_required
@require_any_service_access(["compounding_planning", "compounding_board"])
@require_http_methods(["POST"])
def end_iot_batch(request):
    """Operator taps "End batch" when the batch comes off; the end time is now."""
    if not _iot_dashboard_available():
        raise Http404("Not found")

    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    redirect_url = f"{reverse('compounding:iot_dashboard')}?date={plan_date.isoformat()}"
    machine_id = _iot_dashboard_machine_id()

    run_id = (request.POST.get("run_id") or "").strip()
    stock_quality = (request.POST.get("stock_quality") or "").strip().lower()
    rejection_reason = (request.POST.get("rejection_reason") or "").strip().lower()
    notes = (request.POST.get("notes") or "").strip()

    if stock_quality not in {choice[0] for choice in CompoundingIoTRun.QUALITY_CHOICES}:
        messages.error(request, "Select the quality of the batch before ending it.")
        return redirect(redirect_url)
    if stock_quality == CompoundingIoTRun.QUALITY_REJECTED:
        if rejection_reason not in {choice[0] for choice in CompoundingIoTRun.REJECTION_REASON_CHOICES}:
            messages.error(request, "Select why the batch was rejected.")
            return redirect(redirect_url)
    else:
        rejection_reason = ""

    with transaction.atomic():
        run = (
            CompoundingIoTRun.objects.select_for_update()
            .filter(pk=run_id if run_id.isdigit() else None, machine_id=machine_id, ended_at__isnull=True)
            .first()
        )
        if run is None:
            messages.error(request, "That batch has already been ended. Refresh the page to see the current batch.")
            return redirect(redirect_url)
        now = timezone.now()
        run.ended_at = max(now, run.started_at)
        run.stock_quality = stock_quality
        run.rejection_reason = rejection_reason
        if notes:
            run.notes = notes[:2000]
        run.save(update_fields=["ended_at", "stock_quality", "rejection_reason", "notes", "updated_at"])
    messages.success(
        request,
        f"Ended the {run.compound_code} batch after {_iot_run_duration_text(run.started_at, run.ended_at)}.",
    )
    return redirect(redirect_url)


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["GET"])
def planning(request):
    plan_date = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    compound_values = _available_master_compounds(plan)
    compound_options = [{"value": code, "label": code} for code in compound_values]
    compounds_ready = bool(compound_values)

    crins: list[dict[str, Any]] = []
    scope = _recent_crin_scope(request, plan)
    for crin in (
        CompoundingCRIN.objects.filter(scope["query"])
        .select_related("plan")
        .order_by("-created_at", "-id")
    ):
        crins.append(
            {
                "id": crin.id,
                "crin_number": crin.display_number,
                "plan_date": crin.plan.plan_date,
                "is_older": crin.plan_id != plan.id,
                "compound_code": crin.compound_code,
                "quantity": crin.quantity,
                "mb_planning": crin.mb_planning,
                "fb_planning": crin.fb_planning,
                "status": crin.status,
                "has_line": crin.planned_lines.exists(),
                "can_delete": _crin_can_be_deleted(crin),
            }
        )

    master_sheet = _master_sheet_for_plan(plan)
    sheet_headers: list[str] = []
    sheet_rows: list[list[str]] = []
    master_sheet_plan_date = getattr(getattr(master_sheet, "plan", None), "plan_date", None)
    if master_sheet:
        sheet_headers, sheet_rows, _sheet_cols = _sheet_table_with_detected_headers(
            list(getattr(master_sheet, "headers", []) or []),
            list(getattr(master_sheet, "rows", []) or []),
        )
        sheet_rows = _normalize_master_sheet_decimal_cells(sheet_headers, sheet_rows, places=3)
        sheet_rows = _rows_with_ledger_stock(sheet_headers, sheet_rows)
    master_sheet_context = {
        "exists": bool(master_sheet),
        "original_filename": getattr(master_sheet, "original_filename", ""),
        "worksheet_name": getattr(master_sheet, "worksheet_name", ""),
        "headers": sheet_headers,
        "rows": sheet_rows,
        "updated_at": getattr(master_sheet, "updated_at", None),
        "source_plan_date": master_sheet_plan_date,
        "is_carried_forward": bool(master_sheet_plan_date and master_sheet_plan_date != plan.plan_date),
    }

    return render(
        request,
        "magicwand/compounding_board.html",
        {
            "plan": plan,
            "crin_requests": crins,
            "crin_compound_options": compound_options,
            "crin_compounds_ready": compounds_ready,
            "master_sheet": master_sheet_context,
            "mb_alerts": _mb_shortage_alerts(plan),
            "show_all_open": scope["show_all"],
            "hidden_older": scope["hidden_older"],
            "since": scope["since"],
            "show_iot_dashboard": _iot_dashboard_available(),
        },
    )


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def upload_master_sheet(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    upload = request.FILES.get("master_excel")
    if not upload:
        messages.error(request, "Select an Excel file to upload.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    if load_workbook is None:
        messages.error(request, "Excel support is unavailable. Install openpyxl and retry.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    filename = (upload.name or "").strip()
    lowered = filename.lower()
    if not lowered.endswith(".xlsx") and not lowered.endswith(".xlsm"):
        messages.error(request, "Only .xlsx or .xlsm files are supported.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    workbook = None
    try:
        workbook = load_workbook(upload, data_only=True, read_only=True)
        worksheet = workbook.active
        worksheet_name = worksheet.title or "Sheet1"
        parsed_rows = [[_cell_to_text(cell) for cell in row] for row in worksheet.iter_rows(values_only=True)]
    except Exception as exc:
        messages.error(request, f"Could not read Excel file: {exc}")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    raw_headers, raw_rows = _split_header_and_rows(parsed_rows)
    headers, rows = _normalize_sheet(raw_headers, raw_rows)
    rows = _normalize_master_sheet_decimal_cells(headers, rows, places=3)

    max_rows = 2000
    max_cols = 80
    if len(headers) > max_cols:
        headers = headers[:max_cols]
        rows = [row[:max_cols] for row in rows]
        messages.warning(request, f"Excel has too many columns. Truncated to {max_cols} columns.")
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        messages.warning(request, f"Excel has too many rows. Truncated to {max_rows} rows.")

    if not headers:
        messages.error(request, "No readable sheet data found in the uploaded file.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    sheet_cols = _master_sheet_columns(headers)
    actual_totals = _actual_totals_by_compound(plan)
    base_rows = [_get_sheet_row(rows, idx, len(headers)) for idx, _ in enumerate(rows)]
    rows = _recompute_rows_from_base(headers=headers, base_rows=base_rows, columns=sheet_cols, actual_totals=actual_totals)

    with transaction.atomic():
        sheet, _created = CompoundingMasterSheet.objects.select_for_update().get_or_create(
            plan=plan,
            defaults={"created_by": request.user},
        )
        sheet.original_filename = filename
        sheet.worksheet_name = worksheet_name
        sheet.headers = headers
        sheet.rows = rows
        sheet.base_headers = headers
        sheet.base_rows = base_rows
        if not sheet.created_by_id:
            sheet.created_by = request.user
        sheet.save(
            update_fields=[
                "original_filename",
                "worksheet_name",
                "headers",
                "rows",
                "base_headers",
                "base_rows",
                "created_by",
                "updated_at",
            ]
        )

        # An upload for an older date must not overwrite newer live stock.
        newer_sheet_exists = CompoundingMasterSheet.objects.filter(plan__plan_date__gt=plan.plan_date).exists()
        if not newer_sheet_exists and sheet_cols.get("compound_code") is not None:
            code_idx = sheet_cols["compound_code"]
            mb_idx = sheet_cols.get("mb_stock")
            fb_idx = sheet_cols.get("fb_stock")
            record_uploaded_stock(
                plan=plan,
                user=request.user,
                source=filename,
                items=[
                    {
                        "compound_code": _clean_compound_code(row[code_idx]),
                        "mb_stock": _to_decimal(row[mb_idx]) if mb_idx is not None else None,
                        "fb_stock": _to_decimal(row[fb_idx]) if fb_idx is not None else None,
                    }
                    for row in (_get_sheet_row(rows, idx, len(headers)) for idx, _ in enumerate(rows))
                ],
            )

    if newer_sheet_exists:
        messages.warning(request, "A newer master sheet exists, so this file did not change live stock.")
    messages.success(request, f"Loaded {filename} as editable sheet ({len(rows)} rows).")
    return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning", deny="json")
@require_http_methods(["POST"])
def save_master_sheet(request):
    return JsonResponse(
        {"error": "Master sheet is read-only after upload. Upload a new Excel file to update values."},
        status=403,
    )


@login_required
@require_service_access("compounding_board")
@require_http_methods(["GET"])
def display(request):
    plan_date = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    _seed_rollover_lines_for_display(plan)

    line_objs = list(plan.lines.select_related("crin_request").order_by("position", "id"))
    lines: list[dict[str, Any]] = []
    for line in line_objs:
        planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
        planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
        actual_mb = int(getattr(line, "actual_mb", 0) or 0)
        actual_fb = int(getattr(line, "actual_fb", 0) or 0)
        sent_back_mb = int(getattr(line, "returned_to_planning_mb", 0) or 0)
        sent_back_fb = int(getattr(line, "returned_to_planning_fb", 0) or 0)
        remaining_mb_raw = max(planned_mb - _mb_consumed_batches(actual_mb, actual_fb), 0)
        remaining_fb_raw = max(planned_fb - actual_fb, 0)
        remaining_mb = max(remaining_mb_raw - sent_back_mb, 0)
        remaining_fb = max(remaining_fb_raw - sent_back_fb, 0)

        # Keep board focused on active work. Fully saved rows with no remaining
        # (including rows already sent back to planning) are hidden.
        if bool(getattr(line, "actuals_saved", False)) and remaining_mb <= 0 and remaining_fb <= 0:
            continue
        spec = _sheet_spec_for_compound(plan, line.compound_code)
        mb_weight = spec.get("mb_weight")
        fb_weight = spec.get("fb_total_weight")

        def mb_weight_text(batches: int) -> str:
            return _decimal_text(Decimal(batches) * mb_weight) if mb_weight is not None and batches > 0 else ""

        def fb_weight_text(batches: int) -> str:
            return _decimal_text(Decimal(batches) * fb_weight) if fb_weight is not None and batches > 0 else ""

        lines.append(
            {
                "id": line.id,
                "compound_code": line.compound_code,
                "planned_mb_weight": mb_weight_text(planned_mb),
                "actual_mb_weight": mb_weight_text(actual_mb),
                "remaining_mb_weight": mb_weight_text(remaining_mb),
                "planned_fb_weight": fb_weight_text(planned_fb),
                "actual_fb_weight": fb_weight_text(actual_fb),
                "remaining_fb_weight": fb_weight_text(remaining_fb),
                "crin_number": line.crin_request.display_number if line.crin_request else "",
                "planned_mb": planned_mb,
                "planned_fb": planned_fb,
                "actual_mb": actual_mb,
                "actual_fb": actual_fb,
                "actuals_saved": bool(getattr(line, "actuals_saved", False)),
                "remaining_mb": remaining_mb,
                "remaining_fb": remaining_fb,
                "remaining_sent_mb": sent_back_mb,
                "remaining_sent_fb": sent_back_fb,
                "shift1_mb": line.shift1_mb,
                "shift1_fb": line.shift1_fb,
                "shift2_mb": line.shift2_mb,
                "shift2_fb": line.shift2_fb,
                "shift3_mb": line.shift3_mb,
                "shift3_fb": line.shift3_fb,
            }
        )
    pending = list(
        plan.pending.all().order_by("position", "id").values(
            "compound_code",
            "total_batch",
            "crtn",
            "weight_kg",
        )
    )

    return render(
        request,
        "magicwand/compounding_display.html",
        {
            "plan": plan,
            "lines": lines,
            "mb_rows": _board_section_rows(lines, "mb"),
            "fb_rows": _board_section_rows(lines, "fb"),
            "pending": pending,
            "mb_alerts": _mb_shortage_alerts(plan),
            "hide_mb_plan_link": not user_has_service_access(request.user, "compounding_planning"),
            "can_planning": user_has_service_access(request.user, "compounding_planning"),
            "show_iot_dashboard": _iot_dashboard_available(),
        },
    )


@login_required
@require_service_access("compounding_board", deny="json")
@require_http_methods(["POST"])
@transaction.atomic
def set_actuals(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    try:
        line_id = int(request.POST.get("line_id") or 0)
    except (TypeError, ValueError):
        line_id = 0
    # Lock the line so a double click cannot record the same stock movement twice.
    line = get_object_or_404(CompoundingLine.objects.select_for_update(), id=line_id, plan=plan)

    if bool(getattr(line, "actuals_saved", False)):
        messages.info(request, f"Actuals are already saved for {line.compound_code or 'compound'} and cannot be edited.")
        return redirect(f"/compounding/board/?date={plan.plan_date.isoformat()}")

    def to_nonneg_int(value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = 0
        return parsed if parsed >= 0 else 0

    actual_mb = to_nonneg_int(request.POST.get("actual_mb"))
    actual_fb = to_nonneg_int(request.POST.get("actual_fb"))
    totals_before = plan.lines.aggregate(total_mb=Sum("actual_mb"), total_fb=Sum("actual_fb"))
    had_actuals_before_save = bool(int(totals_before.get("total_mb") or 0) or int(totals_before.get("total_fb") or 0))

    line.actual_mb = actual_mb
    line.actual_fb = actual_fb
    line.actuals_saved = True
    line.save(update_fields=["actual_mb", "actual_fb", "actuals_saved", "updated_at"])

    stock_spec = _sheet_spec_for_compound(plan, line.compound_code)
    ledger_result = record_line_actuals(
        line=line,
        user=request.user,
        mb_weight=stock_spec.get("mb_weight"),
        fb_weight=stock_spec.get("fb_total_weight"),
        sheet_mb_stock=stock_spec.get("mb_stock"),
        sheet_fb_stock=stock_spec.get("fb_stock"),
    )

    planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
    planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
    sent_back_mb = int(getattr(line, "returned_to_planning_mb", 0) or 0)
    sent_back_fb = int(getattr(line, "returned_to_planning_fb", 0) or 0)
    remaining_mb = _remaining_mb_batches(planned_mb, actual_mb, actual_fb, sent_back_mb)
    remaining_fb = max(planned_fb - actual_fb - sent_back_fb, 0)

    def _update_mb_plan_remaining(*, batches: int) -> bool:
        if int(batches or 0) <= 0:
            return False
        spec = _sheet_spec_for_compound(plan, line.compound_code)
        batch_size = spec.get("mb_weight")
        if batch_size is None or Decimal(str(batch_size)) <= 0:
            return False

        mb_plan, _created = CompoundingMBPlan.objects.get_or_create(
            plan=plan,
            compound_code=(line.compound_code or "").strip(),
            defaults={"created_by": request.user},
        )
        stock_level = spec.get("mb_stock")
        if stock_level is not None:
            mb_plan.msl = max(int(stock_level), 0)
        mb_plan.batch_size = batch_size
        remaining_qty = _min_quantity_for_mb_batches(batches=int(batches), batch_size=batch_size)
        mb_plan.total_quantity = remaining_qty
        mb_plan.mb_quantity = remaining_qty
        mb_plan.mb_batches = int(batches)
        mb_plan.created_by = mb_plan.created_by or request.user
        mb_plan.save(
            update_fields=["msl", "batch_size", "total_quantity", "mb_quantity", "mb_batches", "created_by", "updated_at"]
        )
        return True

    # Save should return remaining batches back to FB planning on the same CRIN
    # (no new CRIN number), then detach this board line from that CRIN.
    returned_to_same_crin = False
    returned_to_mb_plan = False
    returned_mb = 0
    returned_fb = 0
    same_crin_number = ""
    crin = getattr(line, "crin_request", None)
    if crin is not None:
        if remaining_mb > 0 or remaining_fb > 0:
            quantity, fb_batch_size = _estimate_remaining_quantity(
                plan=plan,
                compound_code=line.compound_code,
                remaining_mb=remaining_mb,
                remaining_fb=remaining_fb,
            )
            crin.quantity = quantity
            crin.mb_planning = (remaining_mb if remaining_mb > 0 else None)
            # Keep 0 (not None) so finished FB is not recalculated from the quantity.
            crin.fb_planning = remaining_fb
            crin.fb_batch_size = fb_batch_size
            crin.status = CompoundingCRIN.STATUS_OPEN
            crin.save(update_fields=["quantity", "mb_planning", "fb_planning", "fb_batch_size", "status", "updated_at"])

            if remaining_mb > 0:
                returned_to_mb_plan = _update_mb_plan_remaining(batches=remaining_mb) or returned_to_mb_plan

            line.returned_to_planning_mb = sent_back_mb + remaining_mb
            line.returned_to_planning_fb = sent_back_fb + remaining_fb
            line.crin_request = None
            line.save(update_fields=["returned_to_planning_mb", "returned_to_planning_fb", "crin_request", "updated_at"])

            returned_to_same_crin = True
            returned_mb = remaining_mb
            returned_fb = remaining_fb
            same_crin_number = crin.display_number
        elif planned_fb <= 0 and not crin.stock_entries.filter(
            entry_type=CompoundingStockEntry.ENTRY_FB_MADE
        ).exists():
            # MB-only CRIN: there is no FB to issue, so making the batches completes it.
            crin.status = CompoundingCRIN.STATUS_CLOSED
            crin.save(update_fields=["status", "updated_at"])
    elif remaining_mb > 0:
        if _update_mb_plan_remaining(batches=remaining_mb):
            line.returned_to_planning_mb = sent_back_mb + remaining_mb
            line.save(update_fields=["returned_to_planning_mb", "updated_at"])

            returned_to_mb_plan = True
            returned_mb = remaining_mb

    # Keep this date's sheet snapshot in step; live stock now comes from the ledger.
    _recompute_master_sheet_stock_for_plan(
        plan,
        focus_compound=line.compound_code,
        base_from_current_when_missing=not had_actuals_before_save,
    )

    compound_label = line.compound_code or "compound"
    stock_key = _normalize_compound_code(line.compound_code)
    balance = stock_balances([stock_key]).get(stock_key) or {}
    mb_balance = balance.get(CompoundingStockEntry.STOCK_MB)
    fb_balance = balance.get(CompoundingStockEntry.STOCK_FB)
    mb_text = _decimal_text(mb_balance) if mb_balance is not None else "—"
    fb_text = _decimal_text(fb_balance) if fb_balance is not None else "—"
    if ledger_result["skipped"]:
        skipped_text = ", ".join(ledger_result["skipped"])
        messages.warning(
            request,
            f"Saved actuals for {compound_label}, but stock was not updated for: {skipped_text}. "
            "Check the weight and stock in the master sheet.",
        )
    else:
        messages.success(
            request,
            f"Saved actuals for {compound_label} and updated stock (MB stock: {mb_text}, FB stock: {fb_text}).",
        )
    typed_value = CompoundingRecordValue.objects.filter(
        month=plan.plan_date.replace(day=1), compound_key=stock_key
    ).first()
    typed_batches = (typed_value.daily_batches or {}).get(str(plan.plan_date.day)) if typed_value else None
    if typed_batches and int(line.actual_fb or 0) > 0:
        messages.warning(
            request,
            f"{compound_label} already has {typed_batches} batches typed in the Production Record for this day. "
            "Both are now counted. Clear the typed value if this is the same production.",
        )
    if mb_balance is not None and mb_balance < 0:
        messages.warning(
            request,
            f"MB stock for {compound_label} is now negative ({mb_text}). Check the entry or upload a stock count.",
        )
    if returned_to_same_crin:
        messages.info(
            request,
            f"Returned remaining batches to FB planning on {same_crin_number} (MB {returned_mb}, FB {returned_fb}).",
        )
    elif returned_to_mb_plan:
        messages.info(
            request,
            f"Returned remaining MB batches ({returned_mb}) to MB planning for {line.compound_code or 'compound'}.",
        )
    return redirect(f"/compounding/board/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def send_remaining_to_planning(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    try:
        line_id = int(request.POST.get("line_id") or 0)
    except (TypeError, ValueError):
        line_id = 0
    line = get_object_or_404(CompoundingLine, id=line_id, plan=plan)

    planned_mb = sum(int(v or 0) for v in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
    planned_fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
    actual_mb = int(getattr(line, "actual_mb", 0) or 0)
    actual_fb = int(getattr(line, "actual_fb", 0) or 0)
    sent_back_mb = int(getattr(line, "returned_to_planning_mb", 0) or 0)
    sent_back_fb = int(getattr(line, "returned_to_planning_fb", 0) or 0)

    remaining_mb = _remaining_mb_batches(planned_mb, actual_mb, actual_fb, sent_back_mb)
    remaining_fb = max(planned_fb - actual_fb - sent_back_fb, 0)
    if remaining_mb <= 0 and remaining_fb <= 0:
        messages.info(request, f"No remaining batches to send for {line.compound_code or 'compound'}.")
        return redirect(f"/compounding/board/?date={plan.plan_date.isoformat()}")

    quantity, fb_batch_size = _estimate_remaining_quantity(
        plan=plan,
        compound_code=line.compound_code,
        remaining_mb=remaining_mb,
        remaining_fb=remaining_fb,
    )

    serial = next_certificate_serial("crin", year=plan.plan_date.year)
    crin_number = f"CRIN-{plan.plan_date.year}-{serial}"
    crin = CompoundingCRIN.objects.create(
        plan=plan,
        crin_number=crin_number,
        compound_folder=(line.compound_code or "").strip(),
        compound_code=(line.compound_code or "").strip(),
        quantity=quantity,
        fb_batch_size=fb_batch_size,
        mb_planning=(remaining_mb if remaining_mb > 0 else None),
        fb_planning=remaining_fb,
        created_by=request.user,
    )

    line.returned_to_planning_mb = sent_back_mb + remaining_mb
    line.returned_to_planning_fb = sent_back_fb + remaining_fb
    line.save(update_fields=["returned_to_planning_mb", "returned_to_planning_fb", "updated_at"])

    messages.success(
        request,
        f"Sent remaining batches for {line.compound_code} to planning as {crin.display_number} (MB {remaining_mb}, FB {remaining_fb}).",
    )
    return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def create_crin(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    selected_compound = _clean_compound_code(request.POST.get("compound_code") or "")
    folder = (request.POST.get("compound_folder") or "").strip()  # legacy form compatibility
    legacy_code = _compound_from_folder(folder) if folder else ""
    compound_code = selected_compound or legacy_code
    quantity_raw = (request.POST.get("quantity") or "").strip()
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        quantity = 0

    def parse_optional_int(value: str) -> Optional[int]:
        raw = (value or "").strip()
        if not raw:
            return None
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    mb_planning = parse_optional_int(request.POST.get("mb_planning") or "")
    fb_planning = parse_optional_int(request.POST.get("fb_planning") or "")

    available_codes = _available_master_compounds(plan)
    if not available_codes:
        messages.error(request, "Upload the master sheet first so CRIN compounds can be selected from Compound Code.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    normalized_code_map = {_normalize_compound_code(code): code for code in available_codes}
    selected_key = _normalize_compound_code(compound_code)
    if not selected_key or selected_key not in normalized_code_map:
        messages.error(request, "Select a valid compound code from the master sheet.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")
    compound_code = normalized_code_map[selected_key]
    if not folder:
        folder = compound_code

    if quantity <= 0:
        messages.error(request, "Enter a quantity greater than 0.")
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    year = plan.plan_date.year
    serial = next_certificate_serial("crin", year=year)
    crin_number = f"CRIN-{year}-{serial}"

    CompoundingCRIN.objects.create(
        plan=plan,
        crin_number=crin_number,
        compound_folder=folder,
        compound_code=compound_code,
        quantity=quantity,
        mb_planning=mb_planning,
        fb_planning=fb_planning,
        created_by=request.user,
    )

    messages.success(request, f"Raised {crin_number} for {compound_code}.")
    raised_key = _normalize_compound_code(compound_code)
    needs_mb_first = False
    for alert in _mb_shortage_alerts(plan):
        if _normalize_compound_code(alert["compound_code"]) == raised_key:
            messages.warning(request, alert["message"])
            needs_mb_first = needs_mb_first or bool(alert.get("required")) or not alert["known"]
    if needs_mb_first:
        # MB comes first: a short CRIN goes to MB planning, not FB planning.
        return redirect(
            f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}&compound={quote_plus(compound_code)}"
        )
    return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")


def _recent_crin_scope(request, plan: CompoundingPlan) -> Dict[str, Any]:
    """
    Which CRINs a planning screen lists: this day's, plus open ones raised the day before
    (so yesterday evening's CRINs are still there). Older open CRINs are counted, not listed,
    unless "All open" is chosen.
    """
    show_all = (request.GET.get("range") or "").strip().lower() == "all"
    since = plan.plan_date - timedelta(days=1)
    if show_all:
        query = models.Q(plan=plan) | models.Q(status=CompoundingCRIN.STATUS_OPEN)
        hidden = 0
    else:
        query = models.Q(plan=plan) | (models.Q(status=CompoundingCRIN.STATUS_OPEN) & models.Q(plan__plan_date__gte=since))
        hidden = (
            CompoundingCRIN.objects.filter(status=CompoundingCRIN.STATUS_OPEN, plan__plan_date__lt=since)
            .exclude(plan=plan)
            .count()
        )
    return {"query": query, "show_all": show_all, "hidden_older": hidden, "since": since}


def _crin_can_be_deleted(crin: CompoundingCRIN) -> bool:
    """A CRIN can be deleted until production is recorded, so no stock history is lost."""
    if (
        crin.status != CompoundingCRIN.STATUS_OPEN
        or crin.quantity != crin.order_quantity
        or crin.stock_entries.exists()
    ):
        return False
    return not any(
        line.actuals_saved
        or int(line.actual_mb or 0)
        or int(line.actual_fb or 0)
        or int(line.returned_to_planning_mb or 0)
        or int(line.returned_to_planning_fb or 0)
        for line in crin.planned_lines.all()
    )


def _remove_crin_from_line(line: CompoundingLine, crin: CompoundingCRIN) -> None:
    """Take a CRIN's batches off a board line, keeping any MB that was sent separately."""
    line.shift1_fb = line.shift2_fb = line.shift3_fb = None
    if crin.mb_planning is not None:
        for field in ("shift1_mb", "shift2_mb", "shift3_mb"):
            if getattr(line, field) == crin.mb_planning:
                setattr(line, field, None)
                break
    if not any(value for value in (line.shift1_mb, line.shift2_mb, line.shift3_mb)):
        line.delete()
        return
    line.crin_request = None
    line.save(update_fields=["shift1_fb", "shift2_fb", "shift3_fb", "shift1_mb", "shift2_mb", "shift3_mb", "crin_request", "updated_at"])


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
@transaction.atomic
def delete_crin(request, crin_id: int):
    crin = get_object_or_404(CompoundingCRIN.objects.select_for_update().select_related("plan"), id=crin_id)
    plan = crin.plan
    page = "planning/fb/" if request.POST.get("return_to") == "fb" else "planning/"
    redirect_url = f"/compounding/{page}?date={plan.plan_date.isoformat()}"
    # Lock board lines so actuals cannot be saved while the CRIN is being removed.
    board_lines = list(crin.planned_lines.select_for_update())

    if not _crin_can_be_deleted(crin):
        messages.error(
            request,
            f"{crin.display_number} cannot be deleted because production has been recorded on it.",
        )
        return redirect(redirect_url)

    crin_number = crin.display_number
    compound_code = crin.compound_code
    header_id = crin.request_id
    for line in board_lines:
        _remove_crin_from_line(line, crin)
    crin.delete()
    if header_id and not CompoundingCRIN.objects.filter(request_id=header_id).exists():
        CompoundingCRINRequest.objects.filter(id=header_id).delete()
    logger.info("Compounding CRIN %s deleted by user id %s", crin_number, request.user.pk)

    board_text = " and removed it from the board" if board_lines else ""
    messages.success(request, f"Deleted {crin_number} ({compound_code}){board_text}.")
    compound_key = _normalize_compound_code(compound_code)
    mb_on_board = any(
        _normalize_compound_code(line.compound_code) == compound_key
        and any(value for value in (line.shift1_mb, line.shift2_mb, line.shift3_mb))
        for line in plan.lines.filter(actuals_saved=False)
    )
    if mb_on_board:
        messages.warning(request, f"MB for {compound_code} is still on the board. It was not changed.")
    return redirect(redirect_url)


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def apply_crin(request, crin_id: int):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    crin = get_object_or_404(CompoundingCRIN, id=crin_id, plan=plan)

    # Legacy endpoint: keep behaviour but route through the explicit send-to-board logic.
    return send_to_board(request, crin_id=crin.id)


def _split_crin_for_stock(crin: CompoundingCRIN, *, send_batches: int, spec: Dict[str, Any]) -> CompoundingCRIN:
    """
    Send only the FB batches the MB stock covers.

    The batches that can be made now go to a new part of the same CRIN (same shown number);
    the rest stays on this CRIN, in FB planning, waiting for MB.
    """
    total_batches = int(crin.fb_planning or 0)
    batch_size = spec.get("fb_total_weight")
    if batch_size is not None and batch_size > 0:
        sent_quantity = int(Decimal(send_batches) * batch_size)
    else:
        sent_quantity = int(crin.quantity) * send_batches // max(total_batches, 1)
    sent_quantity = min(max(sent_quantity, 1), max(int(crin.quantity) - 1, 1))

    base = crin.display_number
    part = 1
    while CompoundingCRIN.objects.filter(crin_number=f"{base}-{part}").exists():
        part += 1
    sibling = CompoundingCRIN.objects.create(
        plan=crin.plan,
        request=crin.request,
        crin_number=f"{base}-{part}",
        compound_folder=crin.compound_folder,
        compound_code=crin.compound_code,
        quantity=sent_quantity,
        ordered_quantity=sent_quantity,
        fb_batch_size=crin.fb_batch_size,
        fb_planning=send_batches,
        mb_planning=None,
        mb_msl=crin.mb_msl,
        created_by=crin.created_by,
        created_at=crin.created_at,
    )
    crin.quantity = max(int(crin.quantity) - sent_quantity, 1)
    crin.fb_planning = total_batches - send_batches
    crin.save(update_fields=["quantity", "fb_planning", "updated_at"])
    return sibling


def _ensure_line_for_crin(*, plan: CompoundingPlan, crin: CompoundingCRIN) -> CompoundingLine:
    existing = plan.lines.filter(crin_request=crin).order_by("position", "id").first()
    if existing:
        return existing

    max_pos = plan.lines.aggregate(models.Max("position")).get("position__max")
    next_pos = int(max_pos) + 1 if isinstance(max_pos, int) else plan.lines.count()
    return CompoundingLine.objects.create(
        plan=plan,
        position=next_pos,
        compound_code=crin.compound_code,
        crin_request=crin,
    )

def _ensure_line_for_compound(*, plan: CompoundingPlan, compound_code: str) -> CompoundingLine:
    compound_code = (compound_code or "").strip()
    # MB gets its own open row: a saved row cannot take actuals, and a CRIN row holds that CRIN's FB.
    existing = (
        plan.lines.filter(compound_code=compound_code, actuals_saved=False, crin_request__isnull=True)
        .order_by("position", "id")
        .first()
    )
    if existing:
        return existing

    max_pos = plan.lines.aggregate(models.Max("position")).get("position__max")
    next_pos = int(max_pos) + 1 if isinstance(max_pos, int) else plan.lines.count()
    return CompoundingLine.objects.create(
        plan=plan,
        position=next_pos,
        compound_code=compound_code,
        crin_request=None,
    )


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def fb_planning_set(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    try:
        crin_id = int(request.POST.get("crin_id") or 0)
    except (TypeError, ValueError):
        crin_id = 0
    crin = get_object_or_404(CompoundingCRIN, id=crin_id, plan=plan)

    spec = _sheet_spec_for_compound(plan, crin.compound_code)
    batch_size = spec.get("fb_total_weight")
    if batch_size is None:
        messages.error(
            request,
            f"FB batch size not found for {crin.compound_code}. Add MB & FB total weight (or MB + Chemicals weight) in Master sheet.",
        )
        return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    batches = _compute_batches(crin.quantity, batch_size)
    crin.fb_batch_size = batch_size
    crin.fb_planning = batches
    crin.save(update_fields=["fb_batch_size", "fb_planning", "updated_at"])

    messages.success(
        request,
        f"FB planning set for {crin.display_number}: {batches} batches (batch size { _decimal_text(batch_size) }).",
    )
    return redirect(f"/compounding/planning/fb/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def mb_planning_set(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)

    compound_code = (request.POST.get("compound_code") or "").strip()
    if not compound_code:
        messages.error(request, "Compound code is required.")
        return redirect(f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}")

    spec = _sheet_spec_for_compound(plan, compound_code)
    stock_level = spec.get("mb_stock")
    if stock_level is None:
        messages.error(
            request,
            f"MB Correct Stock Level not found for {compound_code}. Add MB Correct Stock Lev in Master sheet.",
        )
        return redirect(f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}")
    stock_level_int = max(int(stock_level), 0)

    planning_quantity_raw = (request.POST.get("planning_quantity") or "").strip()
    if planning_quantity_raw:
        try:
            planning_quantity = max(int(Decimal(planning_quantity_raw)), 0)
        except (ArithmeticError, ValueError):
            messages.error(request, "Planning quantity must be a whole number.")
            return redirect(f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}")
    else:
        planning_quantity = _mb_availability(plan, compound_code, spec=spec)["suggested_quantity"]

    batch_size = spec.get("mb_weight")
    if batch_size is None:
        messages.error(
            request,
            f"MB batch size not found for {compound_code}. Add MB weight in Master sheet.",
        )
        return redirect(f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}")

    mb_qty = max(planning_quantity, 0)
    mb_batches = _compute_batches(mb_qty, batch_size)

    mb_plan, _created = CompoundingMBPlan.objects.get_or_create(
        plan=plan,
        compound_code=compound_code,
        defaults={"created_by": request.user},
    )
    # Keep using existing `msl` column as stock snapshot to avoid a schema migration.
    mb_plan.msl = stock_level_int
    mb_plan.batch_size = batch_size
    mb_plan.total_quantity = planning_quantity
    mb_plan.mb_quantity = int(mb_qty)
    mb_plan.mb_batches = int(mb_batches)
    mb_plan.created_by = mb_plan.created_by or request.user
    mb_plan.save(update_fields=["msl", "batch_size", "total_quantity", "mb_quantity", "mb_batches", "created_by", "updated_at"])

    messages.success(
        request,
        f"MB planning saved for {compound_code}: MB={mb_batches} batches (qty {mb_qty}, batch size { _decimal_text(batch_size) }).",
    )
    return redirect(f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def send_to_board(request, crin_id: int):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    crin = get_object_or_404(CompoundingCRIN, id=crin_id)
    if crin.plan_id != plan.id and crin.status != CompoundingCRIN.STATUS_OPEN:
        raise Http404("This CRIN is closed.")

    def _redirect_after_send_block(default_path: str = ""):
        next_url = (request.POST.get("next") or "").strip()
        if next_url.startswith("/"):
            return redirect(next_url)
        referer = (request.META.get("HTTP_REFERER") or "").strip()
        if referer:
            return redirect(referer)
        return redirect(default_path or f"/compounding/planning/?date={plan.plan_date.isoformat()}")

    if CompoundingLine.objects.filter(crin_request=crin).exists():
        messages.info(request, f"{crin.display_number} is already on the compounding board.")
        return _redirect_after_send_block()

    shift = _parse_shift(request.POST.get("shift"))
    if shift is None:
        messages.error(request, "Select a valid shift before sending (1st, 2nd, or 3rd).")
        return _redirect_after_send_block()

    # "Send" should work as "Save + Send" for FB planning.
    # Only auto-compute when CRIN FB planning is not already saved.
    spec = _sheet_spec_for_compound(plan, crin.compound_code)
    if crin.fb_planning is None:
        batch_size = spec.get("fb_total_weight")
        if batch_size is None:
            messages.error(
                request,
                f"FB batch size not found for {crin.compound_code}. Add MB & FB total weight (or MB + Chemicals weight) in Master sheet.",
            )
            return _redirect_after_send_block(f"/compounding/planning/fb/?date={plan.plan_date.isoformat()}")

        batches = _compute_batches(crin.quantity, batch_size)
        crin.fb_batch_size = batch_size
        crin.fb_planning = batches
        crin.save(update_fields=["fb_batch_size", "fb_planning", "updated_at"])

    fb_batches = int(crin.fb_planning or 0)
    required_mb_stock = _fb_required_mb_stock(fb_batches=fb_batches, mb_weight=spec.get("mb_weight"))

    # Lock the plan so two simultaneous sends cannot reserve the same MB stock.
    with transaction.atomic():
        CompoundingPlan.objects.select_for_update().get(pk=plan.pk)
        if CompoundingLine.objects.filter(crin_request=crin).exists():
            messages.info(request, f"{crin.display_number} is already on the compounding board.")
            return _redirect_after_send_block()

        availability = _mb_availability(plan, crin.compound_code, spec=spec)
        free_mb_stock = availability["free"]
        short = fb_batches > 0 and (
            required_mb_stock is None or free_mb_stock is None or free_mb_stock < required_mb_stock
        )
        if short and request.POST.get("partial"):
            free_batches = _available_mb_batches(mb_stock=free_mb_stock, mb_weight=spec.get("mb_weight")) or 0
            if 0 < free_batches < fb_batches:
                crin = _split_crin_for_stock(crin, send_batches=free_batches, spec=spec)
                fb_batches = free_batches
                short = False
        if short:
            free_mb_batches = _available_mb_batches(mb_stock=free_mb_stock, mb_weight=spec.get("mb_weight"))
            available_batches_text = str(free_mb_batches) if free_mb_batches is not None else "0"
            required_stock_text = _decimal_text(required_mb_stock, places=3) if required_mb_stock is not None else "unknown"
            free_stock_text = _decimal_text(free_mb_stock, places=3) if free_mb_stock is not None else "0"
            reserved = availability["reserved"]
            reserved_text = (
                f" {_decimal_text(reserved, places=3)} stock is already reserved by FB on the board."
                if reserved
                else ""
            )
            messages.error(
                request,
                (
                    f"Insufficient MB available for {crin.compound_code}. "
                    f"FB requires {fb_batches} MB batches ({required_stock_text} stock), "
                    f"but only {available_batches_text} batches ({free_stock_text} stock) are available."
                    f"{reserved_text} Create MB first in MB planning."
                ),
            )
            return _redirect_after_send_block(f"/compounding/planning/fb/?date={plan.plan_date.isoformat()}")

        line = _ensure_line_for_crin(plan=plan, crin=crin)
        update_fields = ["updated_at"]
        if shift == 1:
            line.shift1_mb = crin.mb_planning
            line.shift1_fb = crin.fb_planning
            update_fields.extend(["shift1_mb", "shift1_fb"])
        elif shift == 2:
            line.shift2_mb = crin.mb_planning
            line.shift2_fb = crin.fb_planning
            update_fields.extend(["shift2_mb", "shift2_fb"])
        else:
            line.shift3_mb = crin.mb_planning
            line.shift3_fb = crin.fb_planning
            update_fields.extend(["shift3_mb", "shift3_fb"])

        line.save(update_fields=update_fields)

    messages.success(request, f"Sent {crin.display_number} to the compounding board (shift {shift}).")
    next_url = (request.POST.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    referer = (request.META.get("HTTP_REFERER") or "").strip()
    if referer:
        return redirect(referer)
    return redirect(f"/compounding/planning/?date={plan.plan_date.isoformat()}")


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["GET"])
def fb_planning_page(request):
    plan_date = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    scope = _recent_crin_scope(request, plan)
    crin_objs = list(
        CompoundingCRIN.objects.filter(scope["query"])
        .select_related("plan")
        .annotate(
            has_line=models.Exists(
                CompoundingLine.objects.filter(crin_request=models.OuterRef("pk"), actuals_saved=False)
            ),
            has_done_line=models.Exists(
                CompoundingLine.objects.filter(crin_request=models.OuterRef("pk"), actuals_saved=True)
            ),
        )
        .order_by("-created_at", "-id")
    )
    demand = _mb_demand_by_compound(plan)
    crins: list[dict[str, Any]] = []
    for crin in crin_objs:
        spec = _sheet_spec_for_compound(plan, crin.compound_code)
        auto_batch = spec.get("fb_total_weight")
        auto_batches = _compute_batches(crin.quantity, auto_batch) if auto_batch is not None else None
        fb_batches = crin.fb_planning if crin.fb_planning is not None else auto_batches
        availability = _mb_availability(plan, crin.compound_code, spec=spec, demand=demand)
        mb_needed = (
            _fb_required_mb_stock(fb_batches=int(fb_batches), mb_weight=spec.get("mb_weight"))
            if fb_batches is not None
            else None
        )
        free_mb = availability["free"]
        crins.append(
            {
                "id": crin.id,
                "crin_number": crin.display_number,
                "compound_code": crin.compound_code,
                "quantity": crin.quantity,
                "fb_planning": fb_batches,
                "fb_batch_size": crin.fb_batch_size,
                "fb_batch_size_auto": auto_batch,
                "has_line": bool(getattr(crin, "has_line", False)),
                "has_done_line": bool(getattr(crin, "has_done_line", False)),
                "mb_needed": _decimal_text(mb_needed) if mb_needed is not None else "—",
                "mb_free": _decimal_text(free_mb) if free_mb is not None else "—",
                "plan_date": crin.plan.plan_date,
                "is_older": crin.plan_id != plan.id,
                "mb_short": "—",
                "mb_is_short": False,
                "mb_ready": True,
                "mb_alert": "",
                "is_open": crin.status == CompoundingCRIN.STATUS_OPEN,
                "can_delete": _crin_can_be_deleted(crin),
                "_key": _normalize_compound_code(crin.compound_code),
                "_needed": mb_needed,
                "_free": free_mb,
                "_mb_weight": spec.get("mb_weight"),
                "partial_batches": 0,
            }
        )

    # MB first: give free stock to the oldest waiting CRIN. The rest wait for MB planning.
    free_left: Dict[str, Decimal] = {}
    for row in reversed(crins):
        if not row["is_open"] or row["has_line"] or row["has_done_line"] or int(row["fb_planning"] or 0) <= 0:
            continue
        needed = row["_needed"]
        free = row["_free"]
        if needed is None or free is None:
            row["mb_ready"] = False
            row["mb_alert"] = f"MB stock or weight is missing for {row['compound_code']}. Check the master sheet."
            continue
        left = free_left.setdefault(row["_key"], free)
        row["mb_free"] = _decimal_text(left)
        if left >= needed:
            free_left[row["_key"]] = left - needed
            row["mb_short"] = "0"
            continue
        row["mb_ready"] = False
        row["mb_is_short"] = True
        row["mb_short"] = _decimal_text(needed - left)
        weight = row["_mb_weight"]
        if weight and weight > 0 and left > 0:
            covered = int((left / weight).to_integral_value(rounding=ROUND_FLOOR))
            if 0 < covered < int(row["fb_planning"] or 0):
                row["partial_batches"] = covered
                free_left[row["_key"]] = left - Decimal(covered) * weight
        row["mb_alert"] = (
            f"MB stock is not available for {row['compound_code']}. "
            f"Required {_decimal_text(needed)}, available {_decimal_text(left)}, "
            f"short by {_decimal_text(needed - left)}. Plan MB first."
        )
    return render(
        request,
        "magicwand/compounding_fb_plan.html",
        {
            "plan": plan,
            "crins": crins,
            "mb_alerts": _mb_shortage_alerts(plan, demand=demand),
            "show_all_open": scope["show_all"],
            "hidden_older": scope["hidden_older"],
            "since": scope["since"],
        },
    )


@login_required
@require_service_access("compounding_planning")
@require_http_methods(["GET"])
def mb_planning_page(request):
    plan_date = _parse_date(request.GET.get("date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    totals = list(
        CompoundingCRIN.objects.filter(plan=plan)
        .exclude(compound_code="")
        .values("compound_code")
        .annotate(total_quantity=Sum("quantity"))
        .order_by("compound_code")
    )
    total_map: dict[str, int] = {}
    total_keys: list[str] = []
    display_map: dict[str, str] = {}
    for item in totals:
        code = _clean_compound_code(item.get("compound_code"))
        key = _normalize_compound_code(code)
        if not key:
            continue
        if key not in total_map:
            total_map[key] = 0
            total_keys.append(key)
        total_map[key] += int(item.get("total_quantity") or 0)
        display_map.setdefault(key, code)

    # We consider a compound "on board" if any line for that compound already has MB set on any shift.
    line_map: dict[str, CompoundingLine] = {}
    line_keys: list[str] = []
    for line in plan.lines.all().order_by("position", "id"):
        code = _clean_compound_code(line.compound_code)
        key = _normalize_compound_code(code)
        if not key:
            continue
        if key in line_map:
            continue
        line_map[key] = line
        line_keys.append(key)
        display_map.setdefault(key, code)

    mb_plan_map: dict[str, CompoundingMBPlan] = {}
    mb_plan_keys: list[str] = []
    for mb in plan.mb_plans.all().order_by("id"):
        code = _clean_compound_code(mb.compound_code)
        key = _normalize_compound_code(code)
        if not key:
            continue
        if key in mb_plan_map:
            continue
        mb_plan_map[key] = mb
        mb_plan_keys.append(key)
        display_map.setdefault(key, code)

    master_codes = _available_master_compounds(plan)
    ordered_keys: list[str] = []
    seen_keys: set[str] = set()

    def _append_key(code_or_key: str, *, prefer_display: bool = False) -> None:
        code = _clean_compound_code(code_or_key)
        key = _normalize_compound_code(code)
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        ordered_keys.append(key)
        if prefer_display or key not in display_map:
            display_map[key] = code

    # Primary order comes from the master sheet (Excel row order).
    for code in master_codes:
        _append_key(code, prefer_display=True)
    # Non-master compounds are appended in a deterministic order.
    for key in total_keys:
        _append_key(key)
    for key in line_keys:
        _append_key(key)
    for key in mb_plan_keys:
        _append_key(key)

    demand = _mb_demand_by_compound(plan)
    rows_all: list[dict[str, Any]] = []
    key_to_row: dict[str, dict[str, Any]] = {}
    for row_index, key in enumerate(ordered_keys, start=1):
        code = display_map.get(key) or key
        if not code:
            continue
        total_qty_int = int(total_map.get(key) or 0)
        spec = _sheet_spec_for_compound(plan, code)
        auto_batch = spec.get("mb_weight")
        stock_level = spec.get("mb_stock")
        stock_level_int = int(stock_level) if stock_level is not None else None
        mb_plan = mb_plan_map.get(key)
        availability = _mb_availability(plan, code, spec=spec, demand=demand)
        # A saved plan keeps its quantity; otherwise default to the actual MB shortage.
        planning_quantity = (
            int(mb_plan.total_quantity or 0)
            if mb_plan is not None
            else int(availability["suggested_quantity"])
        )
        line = line_map.get(key)
        mb_already_sent = False
        if line is not None and any(
            value not in (None, "")
            for value in (line.shift1_mb, line.shift2_mb, line.shift3_mb)
        ):
            mb_already_sent = True

        can_plan = auto_batch is not None
        computed_mb_qty = max(planning_quantity, 0) if can_plan else 0
        computed_mb_batches = _compute_batches(computed_mb_qty, auto_batch) if can_plan else 0

        if mb_plan is not None:
            mb_plan_number = f"MBP-{int(plan.cjc_number)}-{int(mb_plan.id):03d}"
        else:
            mb_plan_number = f"MBP-{int(plan.cjc_number)}-{row_index:03d}"

        planning_quantity_display = str(planning_quantity)
        if mb_plan is not None and can_plan and computed_mb_batches > 0:
            planning_quantity_display = _decimal_text(Decimal(computed_mb_batches) * auto_batch)

        row_payload = {
            "mb_plan_number": mb_plan_number,
            "compound_code": code,
            "total_quantity": total_qty_int,
            "planning_quantity": planning_quantity,
            "planning_quantity_display": planning_quantity_display,
            "mb_stock": stock_level_int,
            "batch_size": auto_batch,
            "batch_size_auto": auto_batch,
            "mb_quantity": computed_mb_qty,
            "mb_batches": computed_mb_batches,
            "can_plan": can_plan,
            "mb_already_sent": mb_already_sent,
            "availability": _mb_availability_display(availability),
        }
        rows_all.append(row_payload)
        key_to_row[key] = row_payload

    compound_options = [{"value": row["compound_code"], "label": row["compound_code"]} for row in rows_all]
    selected_compound_raw = _clean_compound_code(request.GET.get("compound") or "")
    selected_key = _normalize_compound_code(selected_compound_raw)
    selected_row = key_to_row.get(selected_key)
    if selected_row is None and not selected_key:
        # If user opens MB planning without selecting a compound, auto-focus
        # the first compound that is short of MB, then any with pending MB batches.
        selected_row = next((row for row in rows_all if row["availability"]["has_shortage"]), None) or next(
            (row for row in rows_all if int(row.get("mb_batches") or 0) > 0),
            None,
        )
    rows: list[dict[str, Any]] = [selected_row] if selected_row else []

    return render(
        request,
        "magicwand/compounding_mb_plan.html",
        {
            "plan": plan,
            "rows": rows,
            # Every compound, needing MB first, so nobody has to pick one compound at a time.
            "all_rows": sorted(
                rows_all,
                key=lambda row: (
                    0 if row["availability"]["has_shortage"] else 1 if row["availability"]["open_fb"] else 2,
                ),
            ),
            "needs_mb_count": sum(1 for row in rows_all if row["availability"]["has_shortage"]),
            "compound_options": compound_options,
            "mb_alerts": _mb_shortage_alerts(plan, demand=demand),
            "selected_compound": selected_row["compound_code"] if selected_row else "",
            "focus_compound": selected_row["compound_code"] if selected_row and selected_key else "",
        },
    )

@login_required
@require_service_access("compounding_planning")
@require_http_methods(["POST"])
def mb_send_to_board(request):
    plan_date = _parse_date(request.POST.get("plan_date") or "") or compounding_business_date()
    plan = _get_or_create_plan(plan_date, user=request.user)
    base_url = f"/compounding/planning/mb/?date={plan.plan_date.isoformat()}"

    compound_code = (request.POST.get("compound_code") or "").strip()
    def _mb_redirect(*, code: str = "") -> str:
        back = _safe_next(request)
        if back:
            return back
        selected = _clean_compound_code(code or compound_code)
        if not selected:
            return base_url
        return f"{base_url}&compound={quote_plus(selected)}"

    if not compound_code:
        messages.error(request, "Compound code is required.")
        return redirect(base_url)

    shift = _parse_shift(request.POST.get("shift"))
    if shift is None:
        messages.error(request, "Select a valid shift before sending (1st, 2nd, or 3rd).")
        return redirect(_mb_redirect())

    available_codes = _available_master_compounds(plan)
    normalized_code_map = {_normalize_compound_code(code): code for code in available_codes}
    selected_key = _normalize_compound_code(compound_code)
    if selected_key in normalized_code_map:
        compound_code = normalized_code_map[selected_key]

    spec = _sheet_spec_for_compound(plan, compound_code)
    stock_level = spec.get("mb_stock")
    if stock_level is None:
        messages.error(
            request,
            f"MB Correct Stock Level not found for {compound_code}. Add MB Correct Stock Lev in Master sheet.",
        )
        return redirect(_mb_redirect(code=compound_code))
    stock_level_int = max(int(stock_level), 0)

    planning_quantity_raw = (request.POST.get("planning_quantity") or "").strip()
    existing_mb_plan = CompoundingMBPlan.objects.filter(plan=plan, compound_code=compound_code).first()
    if planning_quantity_raw:
        try:
            planning_quantity = max(int(Decimal(planning_quantity_raw)), 0)
        except (ArithmeticError, ValueError):
            messages.error(request, "Planning quantity must be a whole number.")
            return redirect(_mb_redirect(code=compound_code))
    elif existing_mb_plan is not None:
        planning_quantity = int(existing_mb_plan.total_quantity or 0)
    else:
        planning_quantity = _mb_availability(plan, compound_code, spec=spec)["suggested_quantity"]

    batch_size = spec.get("mb_weight")
    if batch_size is None:
        messages.error(
            request,
            f"MB batch size not found for {compound_code}. Add MB weight in Master sheet.",
        )
        return redirect(_mb_redirect(code=compound_code))
    if Decimal(str(batch_size)) <= 0:
        messages.error(request, "MB batch size must be greater than zero.")
        return redirect(_mb_redirect(code=compound_code))

    if (request.POST.get("planning_source") or "").strip() == "batches":
        batches_text = (request.POST.get("mb_batches") or "").strip()
        if not re.fullmatch(r"[0-9]+", batches_text) or Decimal(batches_text) * batch_size > _MAX_CRIN_QUANTITY:
            messages.error(request, "MB batches must be a whole number.")
            return redirect(_mb_redirect(code=compound_code))
        mb_batches = int(batches_text)
        planning_quantity = max(
            int((Decimal(mb_batches) * batch_size).to_integral_value(rounding=ROUND_FLOOR)),
            _min_quantity_for_mb_batches(batches=mb_batches, batch_size=batch_size),
        )
        mb_qty = planning_quantity
    else:
        mb_qty = max(planning_quantity, 0)
        mb_batches = _compute_batches(mb_qty, batch_size)

    if mb_batches <= 0:
        messages.error(request, "Enter at least 1 MB batch before sending to the board.")
        return redirect(_mb_redirect(code=compound_code))

    shift_field = f"shift{shift}_mb"
    with transaction.atomic():
        CompoundingPlan.objects.select_for_update().get(pk=plan.pk)
        line = _ensure_line_for_compound(plan=plan, compound_code=compound_code)
        current_batches = getattr(line, shift_field)
        if current_batches is not None:
            # Keep the saved plan and the board in step when nothing new is sent.
            messages.warning(
                request,
                f"{compound_code} already has {current_batches} MB batch{'es' if current_batches != 1 else ''} "
                f"on the board for shift {shift}. Choose another shift.",
            )
            return redirect(_mb_redirect(code=compound_code))

        mb_plan, _created = CompoundingMBPlan.objects.get_or_create(
            plan=plan,
            compound_code=compound_code,
            defaults={"created_by": request.user},
        )
        mb_plan.msl = stock_level_int
        mb_plan.batch_size = batch_size
        mb_plan.total_quantity = planning_quantity
        mb_plan.mb_quantity = int(mb_qty)
        mb_plan.mb_batches = int(mb_batches)
        mb_plan.created_by = mb_plan.created_by or request.user
        mb_plan.save(update_fields=["msl", "batch_size", "total_quantity", "mb_quantity", "mb_batches", "created_by", "updated_at"])

        setattr(line, shift_field, mb_batches)
        line.save(update_fields=[shift_field, "updated_at"])
    messages.success(request, f"Sent {compound_code} MB to the compounding board (shift {shift}, {mb_batches} batches).")
    return redirect(_mb_redirect(code=compound_code))


@login_required
@require_service_access("compounding_planning", deny="json")
@require_http_methods(["POST"])
def save(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    plan_id = payload.get("plan_id")
    plan_date = _parse_date(payload.get("plan_date") or "")
    if not plan_date:
        return JsonResponse({"error": "Missing plan_date."}, status=400)

    with transaction.atomic():
        if plan_id:
            plan = get_object_or_404(CompoundingPlan.objects.select_for_update(), id=plan_id)
            if plan.plan_date != plan_date:
                # If switching dates, point to that date's plan instead.
                plan = _get_or_create_plan(plan_date, user=request.user)
        else:
            plan = _get_or_create_plan(plan_date, user=request.user)

        def normalize_int(value: Any) -> Optional[int]:
            if value in ("", None):
                return None
            try:
                v = int(value)
            except (TypeError, ValueError):
                return None
            return v if v >= 0 else None

        def normalize_str(value: Any) -> str:
            return (value or "").strip()

        incoming_lines: List[Dict[str, Any]] = list(payload.get("lines") or [])
        incoming_pending: List[Dict[str, Any]] = list(payload.get("pending") or [])

        existing_lines = {str(obj.id): obj for obj in plan.lines.all()}
        seen_line_ids = set()
        for idx, row in enumerate(incoming_lines):
            obj_id = str(row.get("id") or "").strip()
            compound_code = normalize_str(row.get("compound_code"))
            if obj_id and obj_id in existing_lines:
                obj = existing_lines[obj_id]
                seen_line_ids.add(obj_id)
            else:
                obj = CompoundingLine(plan=plan)
            obj.position = idx
            obj.compound_code = compound_code
            obj.shift1_mb = normalize_int(row.get("shift1_mb"))
            obj.shift1_fb = normalize_int(row.get("shift1_fb"))
            obj.shift1_priority = normalize_str(row.get("shift1_priority"))
            obj.shift2_mb = normalize_int(row.get("shift2_mb"))
            obj.shift2_fb = normalize_int(row.get("shift2_fb"))
            obj.shift2_priority = normalize_str(row.get("shift2_priority"))
            obj.shift3_mb = normalize_int(row.get("shift3_mb"))
            obj.shift3_fb = normalize_int(row.get("shift3_fb"))
            obj.shift3_priority = normalize_str(row.get("shift3_priority"))
            obj.save()

        for obj_id, obj in existing_lines.items():
            if obj_id not in seen_line_ids:
                obj.delete()

        existing_pending = {str(obj.id): obj for obj in plan.pending.all()}
        seen_pending_ids = set()
        for idx, row in enumerate(incoming_pending):
            obj_id = str(row.get("id") or "").strip()
            if obj_id and obj_id in existing_pending:
                obj = existing_pending[obj_id]
                seen_pending_ids.add(obj_id)
            else:
                obj = PendingMixingLine(plan=plan)
            obj.position = idx
            obj.compound_code = normalize_str(row.get("compound_code"))
            obj.total_batch = normalize_int(row.get("total_batch"))
            obj.crtn = normalize_str(row.get("crtn"))
            weight = row.get("weight_kg")
            if weight in ("", None):
                obj.weight_kg = None
            else:
                try:
                    obj.weight_kg = float(weight)
                except (TypeError, ValueError):
                    obj.weight_kg = None
            obj.save()

        for obj_id, obj in existing_pending.items():
            if obj_id not in seen_pending_ids:
                obj.delete()

    messages.success(request, "Compounding plan saved.")
    return JsonResponse({"ok": True, "redirect": f"/compounding/planning/?date={plan.plan_date.isoformat()}"})

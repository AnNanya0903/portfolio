"""Monthly compounding production record, built from board actuals or imported Excel months."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from django.db import transaction
from openpyxl import load_workbook

from .models import (
    IST_TIMEZONE,
    CompoundingDailyScrap,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingRecordMonth,
    CompoundingRecordRow,
    CompoundingStockEntry,
    compounding_business_date,
)
from .stock import normalize_compound_code

ZERO = Decimal("0")
MONTH_NAMES = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
MONTH_NAMES.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})


class RecordImportError(ValueError):
    """The uploaded workbook cannot be imported."""


def to_decimal(value: Any) -> Decimal:
    if value is None or value == "" or isinstance(value, bool):
        return ZERO
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return ZERO
    return number if number.is_finite() else ZERO


def month_bounds(month: date) -> tuple[date, date]:
    start = month.replace(day=1)
    return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])


def parse_month(value: str) -> Optional[date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", (value or "").strip())
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def available_months() -> List[date]:
    months = set(CompoundingRecordMonth.objects.values_list("month", flat=True))
    months.update(CompoundingPlan.objects.filter(lines__actuals_saved=True).dates("plan_date", "month"))
    months.add(compounding_business_date().replace(day=1))
    return sorted(months, reverse=True)


def _new_row(code: str, key: str, position: int, mb_weight: Decimal, chemical_weight: Decimal) -> Dict[str, Any]:
    return {
        "position": position,
        "compound_code": code,
        "key": key,
        "mb_weight": mb_weight,
        "chemical_weight": chemical_weight,
        "previous_balance": ZERO,
        "daily_batches": defaultdict(lambda: ZERO),
        "daily_kg": defaultdict(lambda: ZERO),
        "daily_fb": defaultdict(lambda: ZERO),
        "production_batches": ZERO,
        "production_kg": ZERO,
        "opening_batches": ZERO,
        "opening_kg": ZERO,
        "adjustment_kg": ZERO,
    }


def _imported_rows(record: CompoundingRecordMonth) -> List[Dict[str, Any]]:
    rows = []
    for item in record.rows.all():
        row = _new_row(item.compound_code, item.compound_key, item.position, item.mb_weight, item.chemical_weight)
        row["previous_balance"] = item.previous_balance
        row["opening_batches"] = item.opening_batches
        row["opening_kg"] = item.opening_batches * item.mb_weight
        row["production_batches"] = item.production_batches
        row["production_kg"] = item.production_batches * item.mb_weight
        for day, batches in (item.daily_batches or {}).items():
            amount = to_decimal(batches)
            row["daily_batches"][int(day)] = amount
            row["daily_kg"][int(day)] = amount * item.mb_weight
            row["daily_fb"][int(day)] = amount * (item.mb_weight + item.chemical_weight)
        rows.append(row)
    return rows


def _entry_date(entry: CompoundingStockEntry) -> date:
    if entry.plan_id:
        return entry.plan.plan_date
    return entry.created_at.astimezone(IST_TIMEZONE).date()


def _live_rows(month: date) -> List[Dict[str, Any]]:
    from . import views

    start, end = month_bounds(month)
    rows: Dict[str, Dict[str, Any]] = {}
    sheet_stock: Dict[str, Decimal] = {}
    sheet = (
        CompoundingMasterSheet.objects.filter(plan__plan_date__lte=end)
        .order_by("-plan__plan_date", "-updated_at")
        .first()
    )
    if sheet:
        headers, sheet_rows, columns = views._sheet_table_with_detected_headers(
            list(sheet.headers or []), list(sheet.rows or [])
        )
        code_idx = columns.get("compound_code")
        if code_idx is not None:
            for index, _raw in enumerate(sheet_rows):
                cells = views._get_sheet_row(sheet_rows, index, len(headers))
                code = views._clean_compound_code(cells[code_idx])
                key = normalize_compound_code(code)
                if not key or key in rows:
                    continue
                mb_weight, fb_weight = views._row_batch_weights(cells, columns)
                rows[key] = _new_row(code, key, len(rows), mb_weight, max(fb_weight - mb_weight, ZERO))
                if columns.get("mb_stock") is not None:
                    sheet_stock[key] = to_decimal(cells[columns["mb_stock"]])

    def row_for(code: str) -> Optional[Dict[str, Any]]:
        key = normalize_compound_code(code)
        if not key:
            return None
        if key not in rows:
            rows[key] = _new_row(code.strip(), key, len(rows), ZERO, ZERO)
        return rows[key]

    lines = list(
        CompoundingLine.objects.filter(plan__plan_date__range=(start, end), actuals_saved=True).select_related("plan")
    )
    line_entries: Dict[int, Dict[str, Decimal]] = defaultdict(dict)
    for line_id, entry_type, quantity in CompoundingStockEntry.objects.filter(line__in=lines).values_list(
        "line_id", "entry_type", "quantity"
    ):
        line_entries[line_id][entry_type] = quantity

    for line in lines:
        row = row_for(line.compound_code)
        if row is None:
            continue
        entries = line_entries.get(line.id, {})
        day = line.plan.plan_date.day
        fb_batches = Decimal(int(line.actual_fb or 0))
        mb_batches = Decimal(int(line.actual_mb or 0))
        mixed_kg = -entries.get(CompoundingStockEntry.ENTRY_MB_USED, -(fb_batches * row["mb_weight"]))
        row["daily_batches"][day] += fb_batches
        row["daily_kg"][day] += mixed_kg
        row["daily_fb"][day] += entries.get(
            CompoundingStockEntry.ENTRY_FB_MADE,
            fb_batches * (row["mb_weight"] + row["chemical_weight"]),
        )
        row["production_batches"] += mb_batches
        row["production_kg"] += entries.get(CompoundingStockEntry.ENTRY_MB_MADE, mb_batches * row["mb_weight"])

    has_ledger: set[str] = set()
    for entry in CompoundingStockEntry.objects.filter(stock_type=CompoundingStockEntry.STOCK_MB).select_related("plan"):
        entry_day = _entry_date(entry)
        if entry_day > end:
            continue
        row = row_for(entry.compound_code)
        if row is None:
            continue
        has_ledger.add(row["key"])
        if entry_day < start:
            row["opening_kg"] += entry.quantity
        elif entry.entry_type in (CompoundingStockEntry.ENTRY_OPENING, CompoundingStockEntry.ENTRY_STOCK_COUNT):
            row["adjustment_kg"] += entry.quantity

    for key, row in rows.items():
        if key not in has_ledger:
            row["opening_kg"] = sheet_stock.get(key, ZERO)
        if row["mb_weight"] > 0:
            row["opening_batches"] = row["opening_kg"] / row["mb_weight"]
    return sorted(rows.values(), key=lambda item: item["position"])


def _finish_row(row: Dict[str, Any], days: List[int]) -> Dict[str, Any]:
    row["day_cells"] = [{"batches": row["daily_batches"].get(day, ZERO), "kg": row["daily_kg"].get(day, ZERO)} for day in days]
    row["batches"] = sum((cell["batches"] for cell in row["day_cells"]), ZERO)
    row["mixing_kg"] = sum((cell["kg"] for cell in row["day_cells"]), ZERO)
    row["fb_kg"] = sum((row["daily_fb"].get(day, ZERO) for day in days), ZERO)
    row["closing_kg"] = row["opening_kg"] + row["adjustment_kg"] + row["production_kg"] - row["mixing_kg"]
    if row["mb_weight"] > 0:
        row["closing_batches"] = row["closing_kg"] / row["mb_weight"]
    else:
        row["closing_batches"] = row["opening_batches"] + row["production_batches"] - row["batches"]
    row["is_negative"] = row["closing_kg"] < 0 or row["closing_batches"] < 0
    row["is_active"] = any(
        row[field] != 0
        for field in ("batches", "production_batches", "opening_kg", "adjustment_kg", "closing_kg", "previous_balance")
    )
    return row


def build_month_report(month: date, *, show_all: bool = False) -> Dict[str, Any]:
    start, end = month_bounds(month)
    days = list(range(1, end.day + 1))
    record = CompoundingRecordMonth.objects.filter(month=start).prefetch_related("rows").first()
    rows = [_finish_row(row, days) for row in (_imported_rows(record) if record else _live_rows(start))]

    scrap = {item.date.day: item.quantity for item in CompoundingDailyScrap.objects.filter(date__range=(start, end))}
    day_totals = []
    for index, day in enumerate(days):
        kg = sum((row["day_cells"][index]["kg"] for row in rows), ZERO)
        day_scrap = scrap.get(day, ZERO)
        day_totals.append(
            {
                "day": day,
                "batches": sum((row["day_cells"][index]["batches"] for row in rows), ZERO),
                "kg": kg,
                "fb": sum((row["daily_fb"].get(day, ZERO) for row in rows), ZERO),
                "scrap": scrap.get(day),
                "rejection": day_scrap / kg * 100 if kg else ZERO,
            }
        )

    def total(field: str) -> Decimal:
        return sum((row[field] for row in rows), ZERO)

    totals = {
        field: total(field)
        for field in (
            "previous_balance", "batches", "mixing_kg", "production_batches", "production_kg", "opening_batches",
            "opening_kg", "adjustment_kg", "closing_batches", "closing_kg", "fb_kg",
        )
    }
    totals["fb_with_previous"] = totals["fb_kg"] + totals["previous_balance"]
    totals["scrap"] = sum(scrap.values(), ZERO)
    totals["rejection"] = totals["scrap"] / totals["mixing_kg"] * 100 if totals["mixing_kg"] else ZERO

    board_rows_not_included = 0
    if record:
        board_rows_not_included = CompoundingLine.objects.filter(
            plan__plan_date__range=(start, end), actuals_saved=True
        ).count()
    return {
        "month": start,
        "days": days,
        "rows": rows if show_all else [row for row in rows if row["is_active"]],
        "all_rows": rows,
        "day_totals": day_totals,
        "totals": totals,
        "negative_rows": [row for row in rows if row["is_negative"]],
        "record": record,
        "board_rows_not_included": board_rows_not_included,
    }


def build_compound_summary(months: Iterable[date]) -> Dict[str, Any]:
    months = sorted(months)
    reports = [build_month_report(month, show_all=True) for month in months]
    combined: Dict[str, Dict[str, Any]] = {}
    for month_index, report in enumerate(reports):
        for row in report["all_rows"]:
            item = combined.get(row["key"])
            if item is None:
                item = {
                    "compound_code": row["compound_code"],
                    "key": row["key"],
                    "position": row["position"],
                    "cells": [None] * len(reports),
                    "batches": ZERO,
                    "mixing_kg": ZERO,
                    "fb_kg": ZERO,
                    "production_kg": ZERO,
                }
                combined[row["key"]] = item
            item["cells"][month_index] = {
                "batches": row["batches"],
                "mixing_kg": row["mixing_kg"],
                "fb_kg": row["fb_kg"],
                "production_kg": row["production_kg"],
                "closing_kg": row["closing_kg"],
                "is_negative": row["is_negative"],
            }
            for field in ("batches", "mixing_kg", "fb_kg", "production_kg"):
                item[field] += row[field]

    blank = {"batches": ZERO, "mixing_kg": ZERO, "fb_kg": ZERO, "production_kg": ZERO, "closing_kg": ZERO, "is_negative": False}
    rows = sorted(combined.values(), key=lambda item: item["position"])
    grand_fb = sum((row["fb_kg"] for row in rows), ZERO)
    for row in rows:
        row["cells"] = [cell or dict(blank) for cell in row["cells"]]
        row["fb_share"] = row["fb_kg"] / grand_fb * 100 if grand_fb else ZERO
    month_totals = [
        {field: sum((row["cells"][index][field] for row in rows), ZERO) for field in ("batches", "mixing_kg", "fb_kg", "production_kg", "closing_kg")}
        for index in range(len(reports))
    ]
    return {
        "months": months,
        "rows": rows,
        "month_totals": month_totals,
        "totals": {field: sum((row[field] for row in rows), ZERO) for field in ("batches", "mixing_kg", "fb_kg", "production_kg")},
    }


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _sheet_month(title: str) -> Optional[date]:
    match = re.fullmatch(r"\s*([A-Za-z]+)[-\s]+(\d{4})\s*", title or "")
    if not match or match.group(1).lower() not in MONTH_NAMES:
        return None
    return date(int(match.group(2)), MONTH_NAMES[match.group(1).lower()], 1)


def _parse_sheet(ws) -> Dict[str, Any]:
    columns: Dict[str, int] = {}
    day_columns: Dict[int, int] = {}
    header_row = None
    for row_index in range(1, 11):
        keys = {cell.column: _header_key(cell.value) for cell in ws[row_index] if cell.value is not None}
        if "compoundcode" in keys.values():
            header_row = row_index
            for column, key in keys.items():
                if key == "compoundcode":
                    columns["code"] = column
                elif key.startswith("previousbal"):
                    columns["previous"] = column
                elif key == "mbproductionqty":
                    columns["production"] = column
                elif key == "mbopeningstockqty":
                    columns["opening"] = column
                elif key in ("mbbatchweight", "mbbathweight"):
                    columns["mb_weight"] = column
                elif key == "finalchemicalweight":
                    columns["chemical"] = column
                elif key.isdigit() and 1 <= int(key) <= 31:
                    day_columns[int(key)] = column
            break
    required = {"code", "production", "opening", "mb_weight", "chemical"}
    if header_row is None or not required.issubset(columns) or not day_columns:
        raise RecordImportError(f"{ws.title}: the production record headings were not found.")

    first_row = header_row + 2
    total_row = None
    for row_index in range(first_row, ws.max_row + 1):
        if _header_key(ws.cell(row_index, 1).value).startswith("totalproduction"):
            total_row = row_index
            break
    if total_row is None:
        raise RecordImportError(f"{ws.title}: the Total production row was not found.")

    parsed: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for position, row_index in enumerate(range(first_row, total_row, 2)):
        raw = str(ws.cell(row_index, columns["code"]).value or "").strip()
        code = re.sub(r"^\d+\}\s*", "", raw).strip()
        batch_row = row_index + 1
        daily = {}
        for day, column in day_columns.items():
            amount = to_decimal(ws.cell(batch_row, column).value)
            if amount:
                daily[str(day)] = str(amount)
        production = to_decimal(ws.cell(batch_row, columns["production"]).value)
        opening = to_decimal(ws.cell(batch_row, columns["opening"]).value)
        previous = to_decimal(ws.cell(row_index, columns["previous"]).value) if "previous" in columns else ZERO
        if not code:
            if not (daily or production or opening or previous):
                continue
            code = f"Unnamed (row {row_index})"
        key = normalize_compound_code(code)
        if key in seen:
            raise RecordImportError(f"{ws.title}: {code} appears more than once.")
        seen.add(key)
        parsed.append(
            {
                "position": position,
                "compound_code": code[:64],
                "compound_key": key[:64],
                "mb_weight": to_decimal(ws.cell(row_index, columns["mb_weight"]).value),
                "chemical_weight": to_decimal(ws.cell(row_index, columns["chemical"]).value),
                "previous_balance": previous,
                "opening_batches": opening,
                "production_batches": production,
                "daily_batches": daily,
            }
        )

    scrap: Dict[int, Decimal] = {}
    for row_index in range(total_row, min(total_row + 4, ws.max_row + 1)):
        if _header_key(ws.cell(row_index, 1).value).startswith("totalscrap"):
            for day, column in day_columns.items():
                amount = to_decimal(ws.cell(row_index, column).value)
                if amount > 0:
                    scrap[day] = amount
    return {"rows": parsed, "scrap": scrap}


def import_workbook(upload, *, user, filename: str = "") -> List[Dict[str, Any]]:
    """Import every month sheet. Nothing is saved if any month is invalid."""
    try:
        workbook = load_workbook(upload, data_only=True)
    except Exception as exc:
        raise RecordImportError("The Excel file could not be read.") from exc
    try:
        parsed = {}
        for ws in workbook.worksheets:
            month = _sheet_month(ws.title)
            if month:
                if month in parsed:
                    raise RecordImportError(f"{month:%B %Y} appears more than once.")
                parsed[month] = _parse_sheet(ws)
    finally:
        workbook.close()
    if not parsed:
        raise RecordImportError("No month sheets were found. Sheet names must look like November-2026.")

    results = []
    with transaction.atomic():
        for month, data in sorted(parsed.items()):
            CompoundingRecordMonth.objects.filter(month=month).delete()
            record = CompoundingRecordMonth.objects.create(month=month, source_filename=filename[:255], imported_by=user)
            CompoundingRecordRow.objects.bulk_create([CompoundingRecordRow(record=record, **row) for row in data["rows"]])
            _start, end = month_bounds(month)
            for day, quantity in data["scrap"].items():
                if day <= end.day:
                    CompoundingDailyScrap.objects.update_or_create(
                        date=month.replace(day=day),
                        defaults={"quantity": quantity, "updated_by": user},
                    )
            results.append({"month": month, "rows": len(data["rows"])})
    return results

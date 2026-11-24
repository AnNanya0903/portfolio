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
    CompoundingRecordChange,
    CompoundingRecordMonth,
    CompoundingRecordRow,
    CompoundingRecordValue,
    CompoundingStockEntry,
    compounding_business_date,
)
from .stock import normalize_compound_code

ZERO = Decimal("0")
SOURCE_DAY = "record_day"
SOURCE_PRODUCTION = "record_production"
SOURCE_OPENING = "record_opening"
MAX_RECORD_VALUE = Decimal("9999999")
GRID_FIELDS = {"day", "production", "opening", "previous", "mb_weight", "chemical_weight", "scrap"}
MONTH_NAMES = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
MONTH_NAMES.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})


class RecordImportError(ValueError):
    """The uploaded workbook cannot be imported."""


class RecordSaveError(ValueError):
    """A grid change cannot be saved."""


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
        "locked_days": set(),
        "board_production_batches": ZERO,
        "board_production_kg": ZERO,
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


def _add_board_after_import(rows: List[Dict[str, Any]], record: CompoundingRecordMonth, start: date, end: date) -> tuple[int, int]:
    """
    Add saved board actuals to an imported Excel month.

    A board save made before the import is left out only when the Excel file
    already has batches for that compound on that day, so nothing is counted
    twice. Returns (included, left out).
    """
    by_key = {row["key"]: row for row in rows}
    excel_days = {(row["key"], day) for row in rows for day, batches in row["daily_batches"].items() if batches}
    lines = list(
        CompoundingLine.objects.filter(plan__plan_date__range=(start, end), actuals_saved=True).select_related("plan")
    )
    after = [
        line
        for line in lines
        if line.updated_at >= record.imported_at
        or (normalize_compound_code(line.compound_code), line.plan.plan_date.day) not in excel_days
    ]
    line_entries: Dict[int, Dict[str, Decimal]] = defaultdict(dict)
    for line_id, entry_type, quantity in CompoundingStockEntry.objects.filter(line__in=after).values_list(
        "line_id", "entry_type", "quantity"
    ):
        line_entries[line_id][entry_type] = quantity
    for line in after:
        key = normalize_compound_code(line.compound_code)
        if not key:
            continue
        row = by_key.get(key)
        if row is None:
            row = _new_row(line.compound_code.strip(), key, len(rows), ZERO, ZERO)
            rows.append(row)
            by_key[key] = row
        entries = line_entries.get(line.id, {})
        day = line.plan.plan_date.day
        fb_batches = Decimal(int(line.actual_fb or 0))
        mb_batches = Decimal(int(line.actual_mb or 0))
        row["daily_batches"][day] += fb_batches
        row["daily_kg"][day] += -entries.get(CompoundingStockEntry.ENTRY_MB_USED, -(fb_batches * row["mb_weight"]))
        row["daily_fb"][day] += entries.get(
            CompoundingStockEntry.ENTRY_FB_MADE, fb_batches * (row["mb_weight"] + row["chemical_weight"])
        )
        made_kg = entries.get(CompoundingStockEntry.ENTRY_MB_MADE, mb_batches * row["mb_weight"])
        row["production_batches"] += mb_batches
        row["production_kg"] += made_kg
        row["board_production_batches"] += mb_batches
        row["board_production_kg"] += made_kg
        if fb_batches > 0:
            row["locked_days"].add(day)
    return len(after), len(lines) - len(after)


def _entry_date(entry: CompoundingStockEntry) -> date:
    if entry.movement_date:
        return entry.movement_date
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

    typed_values = list(CompoundingRecordValue.objects.filter(month=start))
    for value in typed_values:
        row = row_for(value.compound_code)
        if row is None:
            continue
        if value.mb_weight is not None:
            row["mb_weight"] = value.mb_weight
        if value.chemical_weight is not None:
            row["chemical_weight"] = value.chemical_weight
        row["previous_balance"] = value.previous_balance

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
        made_kg = entries.get(CompoundingStockEntry.ENTRY_MB_MADE, mb_batches * row["mb_weight"])
        row["production_batches"] += mb_batches
        row["production_kg"] += made_kg
        row["board_production_batches"] += mb_batches
        row["board_production_kg"] += made_kg
        if fb_batches > 0:
            row["locked_days"].add(day)

    for value in typed_values:
        row = row_for(value.compound_code)
        if row is None:
            continue
        for day_text, batches in (value.daily_batches or {}).items():
            day = int(day_text)
            amount = to_decimal(batches)
            row["daily_batches"][day] += amount
            row["daily_kg"][day] += amount * row["mb_weight"]
            row["daily_fb"][day] += amount * (row["mb_weight"] + row["chemical_weight"])
        row["production_batches"] += value.production_batches
        row["production_kg"] += value.production_batches * row["mb_weight"]

    has_ledger: set[str] = set()
    for entry in CompoundingStockEntry.objects.filter(stock_type=CompoundingStockEntry.STOCK_MB).select_related("plan"):
        entry_day = _entry_date(entry)
        if entry_day > end:
            continue
        row = row_for(entry.compound_code)
        if row is None:
            continue
        has_ledger.add(row["key"])
        if entry_day < start or entry.record_source == SOURCE_OPENING:
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
    row["day_cells"] = [
        {
            "day": day,
            "batches": row["daily_batches"].get(day, ZERO),
            "kg": row["daily_kg"].get(day, ZERO),
            "fb": row["daily_fb"].get(day, ZERO),
            "locked": day in row["locked_days"],
        }
        for day in days
    ]
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
    board_rows_included = board_rows_not_included = 0
    if record:
        base_rows = _imported_rows(record)
        board_rows_included, board_rows_not_included = _add_board_after_import(base_rows, record, start, end)
    else:
        base_rows = _live_rows(start)
    rows = [_finish_row(row, days) for row in base_rows]

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
        "board_rows_included": board_rows_included,
        "has_adjustments": any(row["adjustment_kg"] for row in rows),
        "changes": list(CompoundingRecordChange.objects.filter(month=start).select_related("changed_by")[:30]),
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


def _parse_grid_value(change: Dict[str, Any]) -> Decimal:
    text = str(change.get("value") if change.get("value") is not None else "").strip()
    if not text:
        return ZERO
    places = Decimal("0.0001") if change.get("field") in ("mb_weight", "chemical_weight") else Decimal("0.001")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise RecordSaveError(f"{text} is not a valid number.") from exc
    if not value.is_finite() or value < 0 or value > MAX_RECORD_VALUE or value != value.quantize(places):
        raise RecordSaveError(f"{text} must be 0 or more, with up to {abs(places.as_tuple().exponent)} decimal places.")
    return value


def _sheet_stock(month: date, key: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    from . import views

    _start, end = month_bounds(month)
    sheet = (
        CompoundingMasterSheet.objects.filter(plan__plan_date__lte=end)
        .order_by("-plan__plan_date", "-updated_at")
        .first()
    )
    if not sheet:
        return None, None
    headers, rows, columns = views._sheet_table_with_detected_headers(list(sheet.headers or []), list(sheet.rows or []))
    code_idx = columns.get("compound_code")
    if code_idx is None:
        return None, None
    for index, _raw in enumerate(rows):
        cells = views._get_sheet_row(rows, index, len(headers))
        if normalize_compound_code(cells[code_idx]) == key:
            mb = views._to_decimal(cells[columns["mb_stock"]]) if columns.get("mb_stock") is not None else None
            fb = views._to_decimal(cells[columns["fb_stock"]]) if columns.get("fb_stock") is not None else None
            return mb, fb
    return None, None


def _post_movement(*, key, code, stock_type, entry_type, source, movement_date, quantity, user) -> None:
    quantity = quantity.quantize(Decimal("0.001"))
    if quantity == 0:
        return
    CompoundingStockEntry.objects.create(
        compound_key=key,
        compound_code=code[:64],
        stock_type=stock_type,
        entry_type=entry_type,
        quantity=quantity,
        movement_date=movement_date,
        record_source=source,
        note="Production record",
        created_by=user,
    )


def _sync_stock(month: date, value: CompoundingRecordValue, user) -> None:
    """Post only the difference between typed record values and what the ledger already holds."""
    key = value.compound_key
    code = value.compound_code
    start, _end = month_bounds(month)
    sheet_mb, sheet_fb = _sheet_stock(month, key)
    for stock_type, stock in ((CompoundingStockEntry.STOCK_MB, sheet_mb), (CompoundingStockEntry.STOCK_FB, sheet_fb)):
        if stock is not None and not CompoundingStockEntry.objects.filter(compound_key=key, stock_type=stock_type).exists():
            _post_movement(
                key=key, code=code, stock_type=stock_type, entry_type=CompoundingStockEntry.ENTRY_OPENING,
                source="", movement_date=start - timedelta(days=1), quantity=stock, user=user,
            )

    row = next(item for item in _live_rows(start) if item["key"] == key)
    mb_weight, fb_weight = row["mb_weight"], row["mb_weight"] + row["chemical_weight"]
    end = month_bounds(start)[1]
    typed = {start.replace(day=int(day)): to_decimal(batches) for day, batches in (value.daily_batches or {}).items()}
    posted: Dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for movement_date, entry_type, quantity in CompoundingStockEntry.objects.filter(
        compound_key=key, record_source=SOURCE_DAY, movement_date__range=(start, end)
    ).values_list("movement_date", "entry_type", "quantity"):
        posted[(movement_date, entry_type)] += quantity
    for movement_date in set(typed) | {item[0] for item in posted}:
        batches = typed.get(movement_date, ZERO)
        for stock_type, entry_type, target in (
            (CompoundingStockEntry.STOCK_MB, CompoundingStockEntry.ENTRY_MB_USED, -(batches * mb_weight)),
            (CompoundingStockEntry.STOCK_FB, CompoundingStockEntry.ENTRY_FB_MADE, batches * fb_weight),
        ):
            _post_movement(
                key=key, code=code, stock_type=stock_type, entry_type=entry_type, source=SOURCE_DAY,
                movement_date=movement_date, quantity=target - posted[(movement_date, entry_type)], user=user,
            )

    def posted_total(source: str) -> Decimal:
        return sum(
            CompoundingStockEntry.objects.filter(
                compound_key=key, record_source=source, movement_date__range=(start, end)
            ).values_list("quantity", flat=True),
            ZERO,
        )

    _post_movement(
        key=key, code=code, stock_type=CompoundingStockEntry.STOCK_MB, entry_type=CompoundingStockEntry.ENTRY_MB_MADE,
        source=SOURCE_PRODUCTION, movement_date=start,
        quantity=value.production_batches * mb_weight - posted_total(SOURCE_PRODUCTION), user=user,
    )
    if value.opening_batches is not None:
        current = next(item for item in _live_rows(start) if item["key"] == key)["opening_kg"]
        _post_movement(
            key=key, code=code, stock_type=CompoundingStockEntry.STOCK_MB,
            entry_type=CompoundingStockEntry.ENTRY_STOCK_COUNT, source=SOURCE_OPENING, movement_date=start,
            quantity=value.opening_batches * mb_weight - current, user=user,
        )


def _current_grid_value(row: Optional[Dict[str, Any]], field: str, day: Optional[int], scrap: Dict[int, Decimal]) -> Decimal:
    if field == "scrap":
        return scrap.get(day, ZERO)
    if row is None:
        raise RecordSaveError("This compound was not found. Reload the page.")
    if field == "day":
        return row["daily_batches"].get(day, ZERO)
    return {
        "production": row["production_batches"],
        "opening": row["opening_batches"],
        "previous": row["previous_balance"],
        "mb_weight": row["mb_weight"],
        "chemical_weight": row["chemical_weight"],
    }[field]


def apply_record_changes(month: date, changes: List[Dict[str, Any]], *, user, can_edit_all: bool) -> int:
    """Save grid changes together. Nothing is saved if any change is invalid or out of date."""
    start, end = month_bounds(month)
    if not changes:
        return 0
    if len(changes) > 5000:
        raise RecordSaveError("Too many changes at once. Save in smaller parts.")

    with transaction.atomic():
        # One lock per month stops two people saving over the same stock movement.
        list(CompoundingPlan.objects.select_for_update().filter(plan_date__range=(start, end)).values_list("id", flat=True))
        list(CompoundingRecordValue.objects.select_for_update().filter(month=start).values_list("id", flat=True))
        record =CompoundingRecordMonth.objects.select_for_update().filter(month=start).first()
        report = build_month_report(start, show_all=True)
        rows = {row["key"]: row for row in report["all_rows"]}
        scrap = {item.date.day: item.quantity for item in CompoundingDailyScrap.objects.filter(date__range=(start, end))}
        record_rows = {item.compound_key: item for item in record.rows.all()} if record else {}
        values: Dict[str, CompoundingRecordValue] = {}
        audit: List[CompoundingRecordChange] = []
        seen: set[tuple] = set()

        for change in changes:
            if not isinstance(change, dict):
                raise RecordSaveError("Invalid change. Reload the page.")
            field = str(change.get("field") or "")
            key = str(change.get("key") or "")
            if field not in GRID_FIELDS:
                raise RecordSaveError("Invalid change. Reload the page.")
            if field != "scrap" and not can_edit_all:
                raise RecordSaveError("Only Compounding planning users can change production values.")
            day = None
            if field in ("day", "scrap"):
                try:
                    day = int(change.get("day"))
                except (TypeError, ValueError) as exc:
                    raise RecordSaveError("Invalid day. Reload the page.") from exc
                if not 1 <= day <= end.day:
                    raise RecordSaveError("Invalid day. Reload the page.")
            identity = (key, field, day)
            if identity in seen:
                raise RecordSaveError("The same cell was sent twice. Reload the page.")
            seen.add(identity)

            row = rows.get(key)
            new_value = _parse_grid_value(change)
            current = _current_grid_value(row, field, day, scrap)
            old_text = str(change.get("old") if change.get("old") is not None else "").strip()
            try:
                old_value = Decimal(old_text) if old_text else ZERO
            except InvalidOperation as exc:
                raise RecordSaveError("Invalid change. Reload the page.") from exc
            label = row["compound_code"] if row else "Scrap"
            if abs(old_value - current) > Decimal("0.00011"):
                raise RecordSaveError(f"{label} was changed by someone else. Reload the page and try again.")

            if field == "scrap":
                if new_value:
                    CompoundingDailyScrap.objects.update_or_create(
                        date=start.replace(day=day), defaults={"quantity": new_value, "updated_by": user}
                    )
                else:
                    CompoundingDailyScrap.objects.filter(date=start.replace(day=day)).delete()
            elif record:
                item = record_rows.get(key)
                if item is None:
                    raise RecordSaveError(f"{label} comes only from board actuals. Edit it on the Compounding Board.")
                if field == "day" and day in row["locked_days"]:
                    raise RecordSaveError(f"{label}, day {day} includes saved board actuals and is locked.")
                if field == "production":
                    if new_value < row["board_production_batches"]:
                        raise RecordSaveError(
                            f"{label}: MB production cannot be below {row['board_production_batches']} batches saved on the board."
                        )
                    new_value = new_value - row["board_production_batches"]
                if field == "day":
                    daily = dict(item.daily_batches or {})
                    if new_value:
                        daily[str(day)] = str(new_value)
                    else:
                        daily.pop(str(day), None)
                    item.daily_batches = daily
                elif field == "production":
                    item.production_batches = new_value
                elif field == "opening":
                    item.opening_batches = new_value
                elif field == "previous":
                    item.previous_balance = new_value
                elif field == "mb_weight":
                    item.mb_weight = new_value
                else:
                    item.chemical_weight = new_value
                item.save()
            else:
                value = values.get(key)
                if value is None:
                    value, _created = CompoundingRecordValue.objects.select_for_update().get_or_create(
                        month=start, compound_key=key, defaults={"compound_code": row["compound_code"][:64]}
                    )
                    values[key] = value
                if field == "day":
                    if day in row["locked_days"]:
                        raise RecordSaveError(f"{label}, day {day} comes from saved board actuals and is locked.")
                    daily = dict(value.daily_batches or {})
                    if new_value:
                        daily[str(day)] = str(new_value)
                    else:
                        daily.pop(str(day), None)
                    value.daily_batches = daily
                elif field == "production":
                    if new_value < row["board_production_batches"]:
                        raise RecordSaveError(
                            f"{label}: MB production cannot be below {row['board_production_batches']} batches saved on the board."
                        )
                    value.production_batches = new_value - row["board_production_batches"]
                elif field == "opening":
                    value.opening_batches = new_value
                elif field == "previous":
                    value.previous_balance = new_value
                elif field == "mb_weight":
                    value.mb_weight = new_value
                else:
                    value.chemical_weight = new_value
                value.save()
            audit.append(
                CompoundingRecordChange(
                    month=start, compound_code=label[:64], field=field, day=day,
                    old_value=str(current.normalize())[:32] if current else "0",
                    new_value=str(new_value.normalize())[:32] if new_value else "0",
                    changed_by=user,
                )
            )
        for value in values.values():
            _sync_stock(start, value, user)
        CompoundingRecordChange.objects.bulk_create(audit)
    return len(audit)


def month_workbook(report: Dict[str, Any]):
    """Excel file in the production record layout, with working formulas."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter as L

    wb = Workbook()
    ws = wb.active
    month = report["month"]
    ws.title = f"{month:%B-%Y}"
    bold = Font(name="Arial", bold=True)
    head_fill = PatternFill("solid", fgColor="D9E2F3")
    total_fill = PatternFill("solid", fgColor="EAD1DC")
    ws["C1"] = "AVIGIRI URETHANE & RUBBER INDUSTRIES PVT.LTD."
    ws["C2"] = f"COMPOUNDING MONTHLY PRODUCTION RECORD - {month:%B- %Y}"
    ws["C1"].font = ws["C2"].font = bold
    day_columns = {day: 5 + day for day in range(1, 32)}
    headings = {
        1: "SL NO", 2: "Compound Code", 3: "Description", 4: "Uom", 5: "Previous bal. qty.",
        37: "Total MB Mixing Qty", 38: "MB Production Qty", 39: "MB Opening Stock Qty",
        40: "MB Minimum Stock & Closing Balance Qty", 41: "Total FB Qty", 42: "MB Batch Weight", 43: "Final Chemical Weight",
    }
    headings.update({column: day for day, column in day_columns.items()})
    for column, heading in headings.items():
        cell = ws.cell(3, column, heading)
        cell.font = bold
        cell.fill = head_fill
        cell.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")

    first = 5
    kg_rows = []
    for index, row in enumerate(report["all_rows"]):
        kg = first + index * 2
        batch = kg + 1
        kg_rows.append(kg)
        ws.cell(kg, 1, index + 1)
        ws.cell(kg, 2, row["compound_code"]).font = bold
        ws.cell(kg, 3, "Compound")
        ws.cell(kg, 4, "kgs")
        ws.cell(batch, 3, "Batch")
        ws.cell(batch, 4, "Nos")
        if row["previous_balance"]:
            ws.cell(kg, 5, float(row["previous_balance"]))
        ws[f"AP{kg}"] = float(row["mb_weight"])
        ws[f"AQ{kg}"] = float(row["chemical_weight"])
        for cell_data in row["day_cells"]:
            letter = L(day_columns[cell_data["day"]])
            ws[f"{letter}{kg}"] = f"=$AP${kg}*{letter}{batch}"
            if cell_data["batches"]:
                ws[f"{letter}{batch}"] = float(cell_data["batches"])
        for day in range(end_day(month) + 1, 32):
            letter = L(day_columns[day])
            ws[f"{letter}{kg}"] = f"=$AP${kg}*{letter}{batch}"
        opening_batches = row["opening_batches"]
        if row["adjustment_kg"] and row["mb_weight"] > 0:
            opening_batches += row["adjustment_kg"] / row["mb_weight"]
        if row["production_batches"]:
            ws[f"AL{batch}"] = float(row["production_batches"])
        if opening_batches:
            ws[f"AM{batch}"] = float(opening_batches)
        ws[f"AK{kg}"] = f"=SUM(F{kg}:AJ{kg})"
        ws[f"AK{batch}"] = f"=SUM(F{batch}:AJ{batch})"
        ws[f"AL{kg}"] = f"=AP{kg}*AL{batch}"
        ws[f"AM{kg}"] = f"=AP{kg}*AM{batch}"
        ws[f"AN{kg}"] = f"=(AL{kg}+AM{kg})-(AK{kg})"
        ws[f"AN{batch}"] = f"=(AL{batch}+AM{batch})-(AK{batch})"
        ws[f"AO{kg}"] = f"=AQ{kg}*AK{batch}+AK{kg}"
        ws[f"AO{batch}"] = f"=AK{batch}"

    last_batch = first + len(kg_rows) * 2 - 1
    total = max(last_batch + 1, first)
    scrap_row, rejection_row, batch_row, fb_row = total + 1, total + 2, total + 3, total + 4
    for row_number, label in (
        (total, "Total production"), (scrap_row, "Total scrap"), (rejection_row, "Rejection in percentage"),
        (batch_row, "Total batches"), (fb_row, "Total FB qty (MB + chemical)"),
    ):
        ws.cell(row_number, 1, label).font = bold
        ws.merge_cells(start_row=row_number, start_column=1, end_row=row_number, end_column=2)
        for column in range(1, 44):
            ws.cell(row_number, column).fill = total_fill

    def sum_rows(letter: str, offset: int = 0) -> str:
        return "=" + "+".join(f"{letter}{row + offset}" for row in kg_rows) if kg_rows else "=0"

    scrap_by_day = {item["day"]: item["scrap"] for item in report["day_totals"]}
    for letter in ["E"] + [L(column) for column in day_columns.values()] + ["AK", "AL", "AM", "AN"]:
        ws[f"{letter}{total}"] = sum_rows(letter)
    ws[f"AO{total}"] = sum_rows("AO") + f"+E{total}"
    for letter in [L(column) for column in day_columns.values()] + ["AK", "AL", "AM", "AN", "AO"]:
        ws[f"{letter}{batch_row}"] = sum_rows(letter, 1)
    for day, column in day_columns.items():
        letter = L(column)
        if scrap_by_day.get(day):
            ws[f"{letter}{scrap_row}"] = float(scrap_by_day[day])
        ws[f"{letter}{rejection_row}"] = f"=IF({letter}{total}=0,0,{letter}{scrap_row}/{letter}{total}*100)"
        if kg_rows:
            ws[f"{letter}{fb_row}"] = f"={letter}{total}+SUMPRODUCT($AQ${first}:$AQ${last_batch - 1},{letter}{first + 1}:{letter}{last_batch})"
        else:
            ws[f"{letter}{fb_row}"] = "=0"
    ws[f"AK{scrap_row}"] = f"=SUM(F{scrap_row}:AJ{scrap_row})"
    ws[f"AK{rejection_row}"] = f"=IF(AK{total}=0,0,AK{scrap_row}/AK{total}*100)"
    ws[f"AK{fb_row}"] = f"=SUM(F{fb_row}:AJ{fb_row})"

    for row in ws.iter_rows(min_row=first, max_row=fb_row, min_col=5, max_col=43):
        for cell in row:
            cell.number_format = "0.00" if cell.row == rejection_row else "0.0##"
            if cell.row >= total:
                cell.font = bold
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is not None and cell.font.name != "Arial":
                cell.font = Font(name="Arial", bold=cell.font.bold)
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 12
    for column in range(5, 44):
        ws.column_dimensions[L(column)].width = 10 if column <= 36 else 14
    ws.row_dimensions[3].height = 48
    ws.freeze_panes = "C5"
    wb.calculation.fullCalcOnLoad = True
    return wb


def end_day(month: date) -> int:
    return month_bounds(month)[1].day


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

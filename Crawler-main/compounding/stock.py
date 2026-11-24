from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from django.db.models import Sum

from .models import CompoundingCRIN, CompoundingLine, CompoundingPlan, CompoundingStockEntry

QUANTITY_PLACES = Decimal("0.001")


class FBIssueError(ValueError):
    """The requested FB issue cannot be made."""


def normalize_compound_code(value: Any) -> str:
    text = (str(value or "").strip()).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _quantity(value: Decimal) -> Decimal:
    return Decimal(value).quantize(QUANTITY_PLACES)


def stock_balances(compound_keys: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Decimal]]:
    """Return {compound_key: {"mb": balance, "fb": balance}} for compounds with entries."""
    entries = CompoundingStockEntry.objects.all()
    if compound_keys is not None:
        entries = entries.filter(compound_key__in=list(compound_keys))
    balances: Dict[str, Dict[str, Decimal]] = {}
    for item in entries.order_by().values("compound_key", "stock_type").annotate(total=Sum("quantity")):
        balances.setdefault(item["compound_key"], {})[item["stock_type"]] = _quantity(item["total"] or 0)
    return balances


def record_uploaded_stock(
    *,
    plan: CompoundingPlan,
    items: Iterable[Dict[str, Any]],
    user,
    source: str = "",
) -> int:
    """
    Treat uploaded Excel stock as a physical count.

    The first value becomes the opening balance. Later values add a stock-count
    entry for the difference, so earlier history stays visible. Blank cells are ignored.
    """
    items = list(items)
    keys = {normalize_compound_code(item.get("compound_code")) for item in items}
    balances = stock_balances(key for key in keys if key)
    note = f"Excel: {source}"[:255] if source else ""
    new_entries: List[CompoundingStockEntry] = []
    seen: set[str] = set()

    for item in items:
        code = str(item.get("compound_code") or "").strip()
        key = normalize_compound_code(code)
        if not key or key in seen:
            continue
        seen.add(key)
        for stock_type, field in (
            (CompoundingStockEntry.STOCK_MB, "mb_stock"),
            (CompoundingStockEntry.STOCK_FB, "fb_stock"),
        ):
            counted = item.get(field)
            if counted is None:
                continue
            counted = _quantity(counted)
            current = (balances.get(key) or {}).get(stock_type)
            if current is None:
                entry_type = CompoundingStockEntry.ENTRY_OPENING
                quantity = counted
            else:
                entry_type = CompoundingStockEntry.ENTRY_STOCK_COUNT
                quantity = counted - current
                if quantity == 0:
                    continue
            new_entries.append(
                CompoundingStockEntry(
                    compound_key=key,
                    compound_code=code[:64],
                    stock_type=stock_type,
                    entry_type=entry_type,
                    quantity=quantity,
                    plan=plan,
                    note=note,
                    created_by=user,
                )
            )
    CompoundingStockEntry.objects.bulk_create(new_entries)
    return len(new_entries)


def record_line_actuals(
    *,
    line: CompoundingLine,
    user,
    mb_weight: Optional[Decimal],
    fb_weight: Optional[Decimal],
    sheet_mb_stock: Optional[Decimal],
    sheet_fb_stock: Optional[Decimal],
) -> Dict[str, Any]:
    """
    Record the movements from saved board actuals.

    A compound with no ledger yet starts from its current sheet stock. A movement
    is skipped when its weight or starting stock is unknown, and is reported back.
    """
    code = (line.compound_code or "").strip()
    key = normalize_compound_code(code)
    result: Dict[str, Any] = {"posted": 0, "skipped": []}
    if not key:
        return result

    actual_mb = int(line.actual_mb or 0)
    actual_fb = int(line.actual_fb or 0)
    balances = stock_balances([key]).get(key) or {}
    starting = {
        CompoundingStockEntry.STOCK_MB: sheet_mb_stock,
        CompoundingStockEntry.STOCK_FB: sheet_fb_stock,
    }
    has_stock = set(balances)

    def ensure_start(stock_type: str) -> bool:
        if stock_type in has_stock:
            return True
        value = starting.get(stock_type)
        if value is None:
            return False
        CompoundingStockEntry.objects.create(
            compound_key=key,
            compound_code=code[:64],
            stock_type=stock_type,
            entry_type=CompoundingStockEntry.ENTRY_OPENING,
            quantity=_quantity(value),
            plan=line.plan,
            note="Opening stock from master sheet",
            created_by=user,
        )
        has_stock.add(stock_type)
        return True

    movements = [
        (CompoundingStockEntry.STOCK_MB, CompoundingStockEntry.ENTRY_MB_MADE, actual_mb, mb_weight, 1),
        (CompoundingStockEntry.STOCK_MB, CompoundingStockEntry.ENTRY_MB_USED, actual_fb, mb_weight, -1),
        (CompoundingStockEntry.STOCK_FB, CompoundingStockEntry.ENTRY_FB_MADE, actual_fb, fb_weight, 1),
    ]
    for stock_type, entry_type, batches, weight, sign in movements:
        if batches <= 0:
            continue
        if weight is None or weight <= 0 or not ensure_start(stock_type):
            result["skipped"].append(dict(CompoundingStockEntry.ENTRY_CHOICES)[entry_type])
            continue
        CompoundingStockEntry.objects.create(
            compound_key=key,
            compound_code=code[:64],
            stock_type=stock_type,
            entry_type=entry_type,
            quantity=_quantity(Decimal(batches) * weight * sign),
            batches=batches,
            batch_weight=_quantity(weight),
            plan=line.plan,
            line=line,
            crin=line.crin_request,
            created_by=user,
        )
        result["posted"] += 1
    return result


def crin_fb_totals(crin_ids: Iterable[int]) -> Dict[int, Dict[str, Decimal]]:
    """Return {crin_id: {"made": quantity, "issued": quantity}}."""
    totals: Dict[int, Dict[str, Decimal]] = {}
    rows = (
        CompoundingStockEntry.objects.filter(
            crin_id__in=list(crin_ids),
            entry_type__in=[CompoundingStockEntry.ENTRY_FB_MADE, CompoundingStockEntry.ENTRY_FB_ISSUED],
        )
        .order_by()
        .values("crin_id", "entry_type")
        .annotate(total=Sum("quantity"))
    )
    for row in rows:
        entry = totals.setdefault(row["crin_id"], {"made": Decimal("0.000"), "issued": Decimal("0.000")})
        if row["entry_type"] == CompoundingStockEntry.ENTRY_FB_MADE:
            entry["made"] = _quantity(row["total"] or 0)
        else:
            entry["issued"] = _quantity(-(row["total"] or 0))
    return totals


def record_fb_issue(
    *,
    crin: CompoundingCRIN,
    quantity: Decimal,
    user,
    sheet_fb_stock: Optional[Decimal],
    note: str = "",
) -> Dict[str, Any]:
    """
    Issue FB to Production against a CRIN. Must run inside a transaction.

    The issue is complete once Compounding records it. The CRIN closes when the
    full order quantity has been issued.
    """
    quantity = _quantity(quantity)
    code = (crin.compound_code or "").strip()
    key = normalize_compound_code(code)
    # Lock this compound's FB entries so two issues cannot spend the same stock.
    list(
        CompoundingStockEntry.objects.select_for_update()
        .filter(compound_key=key, stock_type=CompoundingStockEntry.STOCK_FB)
        .order_by("id")
        .values_list("id", flat=True)
    )
    ledger_balance = (stock_balances([key]).get(key) or {}).get(CompoundingStockEntry.STOCK_FB)
    balance = ledger_balance if ledger_balance is not None else sheet_fb_stock
    if balance is None:
        raise FBIssueError(f"FB stock is not available for {code}. Add it in the master sheet.")
    balance = _quantity(balance)

    issued = (crin_fb_totals([crin.id]).get(crin.id) or {}).get("issued", Decimal("0.000"))
    outstanding = Decimal(crin.order_quantity) - issued
    if quantity > outstanding:
        raise FBIssueError(f"Only {outstanding.normalize():f} is left to issue on {crin.crin_number}.")
    if quantity > balance:
        raise FBIssueError(f"Only {max(balance, Decimal('0')).normalize():f} FB stock is available for {code}.")

    if ledger_balance is None:
        CompoundingStockEntry.objects.create(
            compound_key=key,
            compound_code=code[:64],
            stock_type=CompoundingStockEntry.STOCK_FB,
            entry_type=CompoundingStockEntry.ENTRY_OPENING,
            quantity=balance,
            plan=crin.plan,
            note="Opening stock from master sheet",
            created_by=user,
        )
    CompoundingStockEntry.objects.create(
        compound_key=key,
        compound_code=code[:64],
        stock_type=CompoundingStockEntry.STOCK_FB,
        entry_type=CompoundingStockEntry.ENTRY_FB_ISSUED,
        quantity=-quantity,
        plan=crin.plan,
        crin=crin,
        note=(note or "")[:255],
        created_by=user,
    )
    remaining = outstanding - quantity
    closed = remaining <= 0
    if closed:
        crin.status = CompoundingCRIN.STATUS_CLOSED
        crin.save(update_fields=["status", "updated_at"])
    return {"closed": closed, "remaining": remaining, "stock": balance - quantity}

"""Bulk import of customers and contacts from an Excel (.xlsx) or CSV sheet.

One row per contact. Rows with the same customer code belong to one customer: the first row gives the
company details, every row can add a contact. Customers that are already in KYC are never changed.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Tuple

from django.db import transaction

from .forms import CustomerContactForm, CustomerForm
from .models import Customer
from .teams import is_yes, looks_like_purchaser, team_from_text

MAX_ROWS = 2000

# Sheet column -> the headings it may have (compared in lower case, without punctuation).
COLUMNS: Dict[str, Tuple[str, ...]] = {
    "code": ("customer code", "code", "cust code", "customer id", "customer no"),
    "name": ("company name", "customer name", "customer", "company", "name"),
    "address": ("address", "company address", "billing address"),
    "city": ("city", "town"),
    "state": ("state",),
    "pincode": ("pin", "pincode", "pin code", "postal code", "zip"),
    "country": ("country",),
    "gstin": ("gstin", "gst", "gst no", "gst number", "gstin no"),
    "pan": ("pan", "pan no", "pan number"),
    "phone": ("company phone", "phone", "telephone", "landline", "office phone"),
    "website": ("website", "web"),
    "notes": ("notes", "remarks"),
    "contact_name": ("contact name", "contact person", "contact", "person"),
    "designation": ("designation", "title", "role"),
    "email": ("email", "e mail", "email id", "contact email", "mail id"),
    "contact_phone": ("contact phone", "mobile", "mobile no", "contact no", "contact number", "cell"),
    "team": ("team", "department", "dept", "category"),
    "purchaser": ("purchaser", "is purchaser"),
}
TEMPLATE_HEADERS = ["Customer code", "Company name", "Address", "City", "State", "PIN", "Country", "GSTIN", "PAN",
                    "Company phone", "Website", "Contact name", "Designation", "Email", "Mobile", "Team", "Purchaser"]
TEMPLATE_SAMPLE = ["AV-0001", "Sample Seals Pvt Ltd", "12 Industrial Area", "Bengaluru", "Karnataka", "560058",
                   "India", "", "", "080-0000000", "", "Sample Person", "Purchase Manager",
                   "purchase@sample.example", "9000000000", "Sales and marketing", "Yes"]

_CUSTOMER_KEYS = ("code", "name", "address", "city", "state", "pincode", "country", "gstin", "pan", "phone",
                  "website", "notes")


class SheetError(ValueError):
    """The message is shown to the user."""


def _heading(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():  # Excel stores PIN codes and mobiles as 560058.0
        value = int(value)
    return str(value).strip()


def _table(uploaded) -> List[List[Any]]:
    name = (getattr(uploaded, "name", "") or "").lower()
    data = uploaded.read()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        except Exception as exc:
            raise SheetError("This Excel file could not be opened. Save it as .xlsx and try again.") from exc
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return [list(row) for row in csv.reader(io.StringIO(text))]
    raise SheetError("Upload an Excel file (.xlsx) or a CSV file (.csv).")


def read_rows(uploaded) -> List[Dict[str, Any]]:
    """Rows of the sheet as {"row": sheet row number, <column>: text}, for the columns that were recognised."""
    table = [row for row in _table(uploaded)]
    header_at = next((i for i, row in enumerate(table) if any(_cell(c) for c in row)), None)
    if header_at is None:
        raise SheetError("The sheet is empty.")

    lookup = {alias: column for column, aliases in COLUMNS.items() for alias in aliases}
    columns: Dict[int, str] = {}
    for index, value in enumerate(table[header_at]):
        column = lookup.get(_heading(value))
        if column and column not in columns.values():
            columns[index] = column
    found = set(columns.values())
    if "code" not in found or "name" not in found:
        raise SheetError("The first row must have the headings “Customer code” and “Company name”. "
                         "Download the template to see the headings.")

    rows: List[Dict[str, Any]] = []
    for offset, raw in enumerate(table[header_at + 1:], start=header_at + 2):
        row = {column: _cell(raw[index]) if index < len(raw) else "" for index, column in columns.items()}
        if not any(row.values()):
            continue
        row["row"] = offset
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise SheetError(f"The sheet has more than {MAX_ROWS} rows. Split it into smaller files.")
    if not rows:
        raise SheetError("The sheet has headings but no rows.")
    return rows


def _first_error(form) -> str:
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        return f"{label}: {errors[0]}" if label else str(errors[0])
    return "Not valid."


def _contact_data(row: Dict[str, Any]) -> Dict[str, Any]:
    email = (row.get("email") or "").strip()
    phone = row.get("contact_phone") or ""
    name = row.get("contact_name") or ""
    if not (name or email or phone):
        return {}
    designation = row.get("designation") or ""
    purchaser = row.get("purchaser")
    return {
        "name": name or email or phone,
        "designation": designation,
        "email": email,
        "phone": phone,
        "category": team_from_text(row.get("team") or "", designation),
        "is_purchaser": bool(email) and (is_yes(purchaser) or (not purchaser and looks_like_purchaser(designation))),
    }


def build_plan(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What importing these rows would do. Plain lists and dicts, so it can wait in the session."""
    groups: Dict[str, Dict[str, Any]] = {}
    problems: List[Dict[str, Any]] = []
    for row in rows:
        code = re.sub(r"\s+", "", row.get("code") or "").upper()
        if not code:
            problems.append({"row": row["row"], "message": "No customer code, so this row was left out."})
            continue
        group = groups.setdefault(code, {"code": code, "row": row["row"], "contacts": [],
                                         "data": {key: row.get(key) or "" for key in _CUSTOMER_KEYS}})
        group["data"]["code"] = code
        contact = _contact_data(row)
        if not contact:
            continue
        form = CustomerContactForm(contact)
        if form.is_valid():
            group["contacts"].append(contact)
        else:
            problems.append({"row": row["row"], "message": f"Contact left out. {_first_error(form)}"})

    existing = set(Customer.objects.filter(code__in=list(groups)).values_list("code", flat=True))
    customers: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for code, group in groups.items():
        if code in existing:
            skipped.append({"code": code, "name": group["data"]["name"]})
            continue
        if not group["data"]["country"]:
            group["data"]["country"] = "India"
        form = CustomerForm(group["data"])
        if not form.is_valid():
            problems.append({"row": group["row"], "message": f"Customer {code} left out. {_first_error(form)}"})
            continue
        customers.append(group)
    problems.sort(key=lambda p: p["row"])
    return {"customers": customers, "skipped": skipped, "problems": problems,
            "contact_count": sum(len(c["contacts"]) for c in customers)}


def apply_plan(plan: Dict[str, Any], user) -> Dict[str, int]:
    """Create the customers and contacts of a plan. Checked again, as time has passed since the preview."""
    created = contacts = skipped = 0
    for group in plan.get("customers") or []:
        form = CustomerForm(group.get("data") or {})
        if not form.is_valid():  # for example the code was added by someone else meanwhile
            skipped += 1
            continue
        with transaction.atomic():
            customer = form.save(commit=False)
            customer.created_by = customer.updated_by = user
            customer.save()
            for data in group.get("contacts") or []:
                contact_form = CustomerContactForm(data)
                if not contact_form.is_valid():
                    continue
                contact = contact_form.save(commit=False)
                contact.customer = customer
                contact.created_by = contact.updated_by = user
                contact.save()
                contacts += 1
        created += 1
    return {"customers": created, "contacts": contacts, "skipped": skipped}


def template_csv() -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(TEMPLATE_SAMPLE)
    return out.getvalue()

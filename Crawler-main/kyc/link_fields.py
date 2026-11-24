"""The fields of the KYC form, the steps they are on, and how each one is checked.

The steps are the eight sections of AVIGIRI's Customer Registration Form (forms/Avigiri_Customer_Registration_Form.pdf),
in its order: company details, addresses, statutory and tax details, contact details, commercial and logistics, bank
details, documents to be enclosed, declaration. A * on the paper form is `required=True` here. Payment terms and the
credit limit are set by AVIGIRI staff and are not fields of this form.

The same rules run in the browser (handed over by `client_config`) and on the server (`clean`, `conditional`).
No Aadhaar number is collected anywhere: the Udyam field refuses a 12-digit number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from . import logistics
from .models import CustomerContact

GSTIN_RE = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
PAN_RE = r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$"
IFSC_RE = r"^[A-Z]{4}0[A-Z0-9]{6}$"
# Indian mobile or landline after removing spaces and dashes: +91 / 0 prefix optional.
PHONE_RE = r"^(?:\+91|91|0)?(?:[6-9][0-9]{9}|[1-9][0-9]{9})$"
ACCOUNT_RE = r"^[0-9]{9,18}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
PIN_RE = r"^[1-9][0-9]{5}$"
YEAR_RE = r"^(19|20)[0-9]{2}$"
BUSINESS_RE = r"^[0-9]{1,7}(\.[0-9]{1,2})?$"
WEBSITE_RE = r"^(https?://)?[^\s/]+\.[^\s/]+\S*$"
MAX_EMAILS = 5

YES_NO = (("YES", "Yes"), ("NO", "No"))
# Tick boxes of the paper form. The code is what is stored; the label is what is shown and printed.
NATURE_OF_COMPANY = (("PROPRIETORSHIP", "Proprietorship"), ("PARTNERSHIP", "Partnership"), ("LLP", "LLP"),
                     ("PRIVATE_LTD", "Private Ltd."), ("PUBLIC_LTD", "Public Ltd."), ("GOVT_PSU", "Govt. / PSU"))
NATURE_OF_BUSINESS = (("OEM", "OEM / Manufacturer"), ("DEALER", "Dealer / Distributor"), ("TRADER", "Trader"),
                      ("END_USER", "End User"), ("EXPORTER", "Exporter"), ("OTHERS", "Others"))
MSME_CATEGORIES = (("MICRO", "Micro"), ("SMALL", "Small"), ("MEDIUM", "Medium"))
TRANSPORTERS = (("ARC", "ARC"), ("VRL", "VRL"), ("OTHER", "Other"))
DELIVERY = (("DOOR", "Door delivery"), ("GODOWN", "Go-Down delivery"))
PAYMENT_TERMS_SUGGESTIONS = ("100 % Advance", "50 % Advance, balance before dispatch", "Against delivery",
                             "30 days credit", "45 days credit", "60 days credit")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    step: int
    required: bool = True
    kind: str = "text"  # text, textarea, select, emails, tel, checkbox (a "Same as" tick: stored as a boolean)
    pattern: str = ""
    upper: bool = False  # stored in capitals without spaces
    sensitive: bool = False  # encrypted at rest, shown masked
    max_length: int = 255
    hint: str = ""
    choices: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    # Required only when another field has a value: (other key, value).
    required_if: Tuple[str, str] = ("", "")
    section: str = ""  # a heading shown above this field
    grid: int = 0  # >0: shares a row with the neighbouring fields that have a grid; width in twelfths


def _address(prefix: str, title: str) -> Tuple[Field, ...]:
    """Address box + city / district / state / PIN, as on the paper form (the name of the company is printed on
    every address, so it is not typed here)."""
    step = 2
    return (
        Field(f"{prefix}_address", f"{title} address", step, kind="textarea", max_length=1000, section=title,
              hint="Street and area"),
        Field(f"{prefix}_city", f"{title}: city / town", step, max_length=100, grid=3),
        Field(f"{prefix}_district", f"{title}: district", step, required=False, max_length=100, grid=3),
        Field(f"{prefix}_state", f"{title}: state", step, max_length=100, grid=3),
        Field(f"{prefix}_pincode", f"{title}: PIN code", step, pattern=PIN_RE, max_length=6, grid=3,
              hint="6 digits"),
    )


# The "Same as ..." ticks copy an address when ticked (in the browser, and again on the server when saved).
ADDRESS_PARTS = ("address", "city", "district", "state", "pincode")
SAME_AS = {"same_billing_as_company": ("company", "billing"), "same_delivery_as_billing": ("billing", "delivery")}

FIELDS: Tuple[Field, ...] = (
    # 1  Company details
    Field("company_name", "Name of the company / firm (as per GST)", 1),
    Field("nature_of_company", "Nature of company", 1, kind="select", choices=NATURE_OF_COMPANY, max_length=20),
    Field("nature_of_business", "Nature of business", 1, kind="select", choices=NATURE_OF_BUSINESS, max_length=20),
    Field("year_established", "Year of establishment", 1, required=False, pattern=YEAR_RE, max_length=4, grid=4,
          hint="e.g. 2011"),
    Field("cin", "CIN / registration no.", 1, required=False, upper=True, max_length=30, grid=4),
    Field("website", "Website", 1, required=False, pattern=WEBSITE_RE, max_length=255, grid=4),
    # 2  Addresses
    *_address("company", "Registered / company address"),
    Field("same_billing_as_company", "Billing address is the same as the company address", 2, required=False,
          kind="checkbox"),
    *_address("billing", "Billing address / buyer"),
    Field("same_delivery_as_billing", "Delivery address is the same as the billing address", 2, required=False,
          kind="checkbox"),
    *_address("delivery", "Delivery address / consignee"),
    # 3  Statutory & tax details
    Field("gstin", "GSTIN No.", 3, pattern=GSTIN_RE, upper=True, max_length=15, grid=6,
          hint="15 characters, e.g. 29ABCDE1234F1Z5. The first 2 digits are the GST state code."),
    Field("pan", "PAN No.", 3, pattern=PAN_RE, upper=True, sensitive=True, max_length=10, grid=6,
          hint="10 characters, e.g. ABCDE1234F"),
    Field("msme_applicable", "MSME applicable", 3, kind="select", choices=YES_NO, max_length=3, grid=4),
    Field("udyam_aadhar_no", "Udyam registration no.", 3, required=False, upper=True, max_length=50, grid=4,
          required_if=("msme_applicable", "YES"), hint="e.g. UDYAM-TN-12-0012345"),
    Field("msme_category", "MSME category", 3, required=False, kind="select", choices=MSME_CATEGORIES, max_length=6,
          grid=4),
    Field("has_certification", "ISO / IATF / other certification", 3, required=False, kind="select", choices=YES_NO,
          max_length=3, grid=4),
    Field("certification_no", "If yes, certificate no. / standard", 3, required=False, max_length=100, grid=8,
          required_if=("has_certification", "YES")),
    # 4  Contact details: the purchase contact is mandatory; more people go in the rows below (EXTRA_CONTACTS)
    Field("contact_name", "Name", 4, max_length=200, section="Purchase contact (mandatory)"),
    Field("contact_designation", "Designation", 4, required=False, max_length=200),
    Field("contact_email", "Email ID", 4, kind="emails", max_length=500,
          hint="One or more, separated by commas"),
    Field("contact_phone", "Phone / mobile", 4, kind="tel", pattern=PHONE_RE, max_length=20,
          hint="10-digit mobile number"),
    # 5  Commercial & logistics
    Field("transportation", "Transportation", 5, max_length=150,
          hint="Transporter, freight and delivery as you want them, e.g. VRL-TO PAY-DOOR, Paid, Ex-Works / Self Pick-up"),
    Field("delivery_type", "Delivery type", 5, kind="select", choices=DELIVERY, max_length=10),
    Field("preferred_transporter", "Preferred transporter", 5, kind="select", choices=TRANSPORTERS, max_length=10,
          hint="Default: ARC"),
    Field("other_transporter", "Transporter name", 5, required=False, max_length=100,
          required_if=("preferred_transporter", "OTHER")),
    Field("products_of_interest", "Products / applications of interest", 5, required=False, max_length=255),
    Field("expected_annual_business", "Expected annual business (Rs. lacs)", 5, required=False, pattern=BUSINESS_RE,
          max_length=10, hint="A number, e.g. 25 or 12.5"),
    Field("special_requirements", "Special requirements", 5, kind="textarea", required=False, max_length=2000,
          hint="Packing, labelling, documents, test certificates, delivery timings, etc."),
    # 6  Bank details (optional on the paper form)
    Field("bank_name", "Bank name & branch", 6, required=False, max_length=200),
    Field("account_holder_name", "Account holder name", 6, required=False, max_length=200),
    Field("account_number", "Account no.", 6, required=False, pattern=ACCOUNT_RE, sensitive=True, max_length=18,
          hint="9 to 18 digits"),
    Field("ifsc", "IFSC code", 6, required=False, pattern=IFSC_RE, upper=True, max_length=11,
          hint="11 characters, e.g. HDFC0001234"),
)
FIELDS_BY_KEY: Dict[str, Field] = {f.key: f for f in FIELDS}

STEPS = (
    (1, "Company details"),
    (2, "Addresses"),
    (3, "Statutory & tax details"),
    (4, "Contact details"),
    (5, "Commercial & logistics"),
    (6, "Bank details"),
    (7, "Documents to be enclosed"),
    (8, "Declaration and consent"),
)
PAYMENT_TERMS_STEP = 5  # where the customer is shown the payment terms AVIGIRI set
FIELD_STEPS = tuple(s for s in STEPS if any(f.step == s[0] for f in FIELDS))

# More contact persons, one per row, each from a department (the ➕ on the contact details step): Managing Director
# (top management), Design / Technical, Accounts, Quality, Stores / Logistics. Stored as a list
# of dicts on KycDetails.extra_contacts; on approval each one becomes a CustomerContact in that department.
EXTRA_CONTACTS = "extra_contacts"
EXTRA_CONTACTS_STEP = 4
EXTRA_CONTACTS_LABEL = "Other contact persons"
MAX_EXTRA_CONTACTS = 10
DEPARTMENTS = CustomerContact.CATEGORY_CHOICES
CONTACT_FIELDS: Tuple[Field, ...] = (
    Field("department", "Department", EXTRA_CONTACTS_STEP, kind="select", choices=DEPARTMENTS, max_length=20),
    Field("name", "Name", EXTRA_CONTACTS_STEP, max_length=200),
    Field("designation", "Designation", EXTRA_CONTACTS_STEP, required=False, max_length=200),
    Field("email", "Email ID", EXTRA_CONTACTS_STEP, kind="emails", max_length=254,
          hint="One or more, separated by commas"),
    Field("phone", "Phone No.", EXTRA_CONTACTS_STEP, kind="tel", pattern=PHONE_RE, max_length=20,
          hint="10-digit mobile number"),
)

# (doc_type, label) — kept in step with KycRequestDocument.TYPE_CHOICES. The signed form is uploaded after submit; the
# generated form is made by AVIGIRI. Only the documents marked * on the paper form are mandatory.
DOCUMENTS = (
    ("gst_certificate", "GST registration certificate"),
    ("pan_card", "PAN card of the company"),
    ("udyam_certificate", "Udyam (MSME) certificate, if applicable"),
    ("cancelled_cheque", "Cancelled cheque / bank letter"),
    ("incorporation_certificate", "Certificate of incorporation / partnership deed"),
    ("iso_certificate", "ISO / IATF certificate, if applicable"),
)
DOCUMENT_TYPES = tuple(key for key, _ in DOCUMENTS)
REQUIRED_DOCUMENTS = ("gst_certificate", "pan_card")
SIGNED_FORM = "signed_form"
GENERATED_FORM = "generated_form"

CONSENT_TEXT = "I consent to AVIGIRI collecting and processing this data for KYC purposes"

# Fields and documents a reviewer can send back after a rejection.
REOPENABLE = tuple(f.key for f in FIELDS) + (EXTRA_CONTACTS,) + DOCUMENT_TYPES
LABELS: Dict[str, str] = {**{f.key: f.label for f in FIELDS}, EXTRA_CONTACTS: EXTRA_CONTACTS_LABEL,
                          **dict(DOCUMENTS)}

_AADHAAR_LIKE = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")


def display(f: Field, value) -> str:
    """The label of a choice ("DOOR" -> "Door delivery"); other values as they are; a tick as Yes / blank."""
    if f.kind == "checkbox":
        return "Yes" if value else ""
    if f.choices:
        return dict(f.choices).get(value, value)
    return value


def split_emails(value: str) -> List[str]:
    return [p for p in re.split(r"[,;\s]+", str(value or "").strip().lower()) if p]


def normalise(f: Field, value) -> str:
    text = str(value if value is not None else "").strip()
    if f.kind != "textarea":
        text = " ".join(text.split())
    if f.upper:
        text = re.sub(r"\s+", "", text).upper()
    if f.kind == "tel" or f.key == "account_number":
        text = re.sub(r"[\s-]", "", text)
    if f.kind == "emails":
        text = ", ".join(dict.fromkeys(split_emails(text)))
    if f.kind == "select":
        text = text.upper()
    if f.key == "udyam_aadhar_no":
        text = logistics.udyam(text)
    if f.key == "website":
        text = text.lower()
    return text


def check_value(f: Field, value: str) -> Optional[str]:
    """An error message for a non-empty value, or None."""
    if len(value) > f.max_length:
        return f"{f.label} is too long."
    if f.kind == "select" and value not in dict(f.choices):
        return f"Choose {f.label.lower()}."
    if f.kind == "emails":
        addresses = split_emails(value)
        if len(addresses) > MAX_EMAILS:
            return f"Up to {MAX_EMAILS} email addresses."
        for address in addresses:
            try:
                validate_email(address)
            except ValidationError:
                return f"“{address}” is not a valid email address."
    if f.pattern and not re.match(f.pattern, value):
        return f"Enter a valid {f.label.lower()}." + (f" ({f.hint})" if f.hint else "")
    if f.key == "year_established" and int(value) > timezone.localdate().year:
        return "The year of establishment cannot be in the future."
    if f.key == "udyam_aadhar_no":
        if re.fullmatch(r"\d{12}", value):
            return "That looks like an Aadhaar number. Enter the Udyam registration number instead."
        if logistics.udyam_looks_wrong(value):
            return "Enter the Udyam number as UDYAM-XX-00-0000000."
    if not f.pattern and f.kind in ("text", "textarea") and f.key != "udyam_aadhar_no" \
            and _AADHAAR_LIKE.search(value):
        return "Please do not enter an Aadhaar number."
    return None


def clean(data: Dict, allowed: Optional[List[str]] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(values, errors) for the submitted keys. Keys that are not form fields, or not in `allowed`, are ignored.

    Empty values are kept (a draft may clear a field); only filled-in values are checked here. Whether every
    required field is there is checked on submit (`missing`).
    """
    values: Dict = {}
    errors: Dict[str, str] = {}
    for key, raw in (data or {}).items():
        if key == EXTRA_CONTACTS and (allowed is None or key in allowed):
            rows, row_errors = clean_contacts(raw)
            if row_errors:
                errors.update(row_errors)  # the list is saved whole or not at all
            else:
                values[key] = rows
            continue
        f = FIELDS_BY_KEY.get(key)
        if f is None or (allowed is not None and key not in allowed):
            continue
        if f.kind == "checkbox":
            values[key] = raw is True or str(raw).strip().lower() in ("1", "true", "on", "yes")
            continue
        value = normalise(f, raw)
        if f.sensitive and not value:
            continue  # an empty PAN / account number box means "keep what is saved"
        if value:
            problem = check_value(f, value)
            if problem:
                errors[key] = problem
                continue
        values[key] = value
    return values, errors


def contact_error_key(index: int, sub: str) -> str:
    return f"{EXTRA_CONTACTS}.{index}.{sub}"


def clean_contacts(raw) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
    """(rows, errors) for the extra contact persons. Rows left completely empty are dropped; every other row needs
    a department, name, email and phone. Error keys are "extra_contacts.<row>.<field>" (row as sent)."""
    if not isinstance(raw, list):
        return [], ({EXTRA_CONTACTS: "Could not read the contact persons."} if raw else {})
    rows: List[Dict[str, str]] = []
    errors: Dict[str, str] = {}
    for index, item in enumerate(raw):
        item = item if isinstance(item, dict) else {}
        row = {}
        for f in CONTACT_FIELDS:
            value = item.get(f.key)
            value = str(value).strip().lower() if f.key == "department" and value is not None else normalise(f, value)
            row[f.key] = value
        if not any(row.values()):
            continue
        for f in CONTACT_FIELDS:
            value = row[f.key]
            problem = check_value(f, value) if value else ("This is required." if f.required else None)
            if problem:
                errors[contact_error_key(index, f.key)] = problem
        rows.append(row)
    if len(rows) > MAX_EXTRA_CONTACTS:
        errors[EXTRA_CONTACTS] = f"Up to {MAX_EXTRA_CONTACTS} more contact persons."
    return rows, errors


def contact_lines(rows) -> List[str]:
    """One readable line per extra contact: "Accounts: Name (Designation) · email · phone"."""
    departments = dict(DEPARTMENTS)
    out = []
    for row in rows or []:
        who = row.get("name", "") + (f" ({row['designation']})" if row.get("designation") else "")
        parts = [who] + [row.get(k, "") for k in ("email", "phone") if row.get(k)]
        out.append(f"{departments.get(row.get('department', ''), row.get('department', ''))}: " + " · ".join(parts))
    return out


def missing(present: Dict[str, bool], values: Dict[str, str]) -> List[str]:
    """Required field keys that have no value. `present` maps each key to whether it is filled in and `values`
    holds the plain values (for the fields required only when another field has a value)."""
    out = []
    for f in FIELDS:
        if f.kind == "checkbox":
            continue
        other, wanted = f.required_if
        needed = f.required or (other and values.get(other) == wanted)
        if needed and not present.get(f.key):
            out.append(f.key)
    return out


def client_config() -> Dict:
    """What the browser needs to show and check the form."""
    def describe(f: Field) -> Dict:
        return {"key": f.key, "label": f.label, "step": f.step, "required": f.required, "kind": f.kind,
                "pattern": f.pattern, "upper": f.upper, "sensitive": f.sensitive, "maxLength": f.max_length,
                "hint": f.hint, "choices": [list(c) for c in f.choices], "section": f.section, "grid": f.grid,
                "requiredIf": list(f.required_if) if f.required_if[0] else None}

    from .link_pdf import DECLARATION  # the text printed on the form, so the customer reads the same words

    return {
        "declaration": DECLARATION,
        "steps": [{"number": n, "title": t} for n, t in STEPS],
        "fields": [describe(f) for f in FIELDS],
        "extraContacts": {"key": EXTRA_CONTACTS, "step": EXTRA_CONTACTS_STEP, "label": EXTRA_CONTACTS_LABEL,
                          "after": "contact_phone", "max": MAX_EXTRA_CONTACTS,
                          "fields": [describe(f) for f in CONTACT_FIELDS]},
        "documents": [{"key": k, "label": label, "required": k in REQUIRED_DOCUMENTS} for k, label in DOCUMENTS],
        "sameAs": {key: {"from": src, "to": dst, "parts": list(ADDRESS_PARTS)} for key, (src, dst) in SAME_AS.items()},
        "paymentStep": PAYMENT_TERMS_STEP,
        "consentText": CONSENT_TEXT,
        "maxEmails": MAX_EMAILS,
    }

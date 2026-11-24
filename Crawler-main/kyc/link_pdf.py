"""The KYC details printed as AVIGIRI's Customer Registration Form (A4), for the customer to sign and stamp.

The layout follows forms/Avigiri_Customer_Registration_Form.pdf: company, addresses, tax, contacts, logistics,
bank, documents, declaration and an office-use panel. Whatever was not given stays as an empty box for the
customer to complete by hand before signing. PAN and the bank account number are printed masked, with only the
last four characters. The credit limit and the approval are printed only once the KYC is approved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from django.conf import settings
from django.utils import timezone

from . import link_fields
from .departments import departments_of
from .models import CustomerContact, KycRequest, KycRequestDocument

COMPANY_LINES = ("AVIGIRI Urethane &", "Rubber Industries Pvt. Ltd.")
WEBSITE = "www.avigiri.com"
CIN = "CIN : U25203KA2011PTC059765"
FOOTER = ("169D Vasanthanarasapura Industrial Area, Kora Hobli, Tumakuru District, TUMAKURU - 572106, Karnataka, India.",
          "Tel: +91 816 297 00 27 / 25   |   Email: apai@avigiri.com   |   www.avigiri.com")
TITLE = "Customer Registration Form"
SUBTITLE = "Know Your Customer (KYC)"
DECLARATION = ("We hereby declare that the information given above is true and correct in all respects. We undertake "
               "to inform AVIGIRI Urethane & Rubber Industries Pvt. Ltd. in writing of any change in the above "
               "particulars, and agree that supplies will be invoiced and dispatched as per the details registered here.")

NAVY = (36, 35, 140)
NAVY_DARK = (26, 26, 102)
LIME = (181, 204, 46)
INK = (31, 35, 48)
MUTED = (91, 98, 117)
LINE = (201, 206, 221)
FIELD = (244, 246, 251)
TINT = (233, 236, 247)
PALE = (213, 217, 242)
RED = (192, 57, 43)
WHITE = (255, 255, 255)
IST = timezone.get_fixed_timezone(330)

NATURE_OF_COMPANY = tuple(label for _, label in link_fields.NATURE_OF_COMPANY)
NATURE_OF_BUSINESS = tuple(label for _, label in link_fields.NATURE_OF_BUSINESS)
MSME_CATEGORIES = tuple(label for _, label in link_fields.MSME_CATEGORIES)
DELIVERY_OPTIONS = (("DOOR", "Door Delivery"), ("GODOWN", "Go-Down Delivery"))
PURCHASE = "Purchase *"
CONTACT_ROWS = ("Managing Director", PURCHASE, "Design / Technical", "Accounts / Payments", "Quality",
                "Stores / Logistics")
ROW_FOR_CATEGORY = {
    CustomerContact.CATEGORY_MANAGEMENT: "Managing Director",
    CustomerContact.CATEGORY_TECHNICAL: "Design / Technical",
    CustomerContact.CATEGORY_ACCOUNTS: "Accounts / Payments",
    CustomerContact.CATEGORY_SALES: "Sales & Marketing",
    CustomerContact.CATEGORY_QUALITY: "Quality",
    CustomerContact.CATEGORY_STORES: "Stores / Logistics",
}
DOCUMENT_ROWS = (
    (KycRequestDocument.TYPE_GST, "GST Registration Certificate *"),
    (KycRequestDocument.TYPE_PAN, "PAN Card of the Company *"),
    (KycRequestDocument.TYPE_UDYAM, "Udyam (MSME) Certificate, if applicable"),
    (KycRequestDocument.TYPE_CHEQUE, "Cancelled Cheque / Bank Letter"),
    (KycRequestDocument.TYPE_INCORPORATION, "Certificate of Incorporation / Partnership Deed"),
    (KycRequestDocument.TYPE_ISO, "ISO / IATF Certificate, if applicable"),
)

_REPLACE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
            "₹": "Rs.", "•": "-", "·": "-", " ": " ", "…": "..."}


def _latin1(text) -> str:
    """The built-in PDF fonts only have Latin-1 characters; anything else becomes the nearest plain character."""
    text = str(text or "")
    for bad, good in _REPLACE.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


# ---------------------------------------------------------------------------------------------------- the data
@dataclass
class Address:
    title: str
    text: str
    city: str = ""
    district: str = ""
    state: str = ""
    pincode: str = ""
    same_as: str = ""  # the "Same as ..." box next to the title; "" for the company address
    same: bool = False


@dataclass
class Contact:
    name: str = ""
    designation: str = ""
    email: str = ""
    phone: str = ""


@dataclass
class Sheet:
    """Everything printed on the form. Empty values print as empty boxes."""
    customer_code: str = ""
    registered_on: str = ""
    company_name: str = ""
    nature_of_company: str = ""  # a code of link_fields.NATURE_OF_COMPANY, or ""
    nature_of_business: str = ""
    year_established: str = ""
    cin: str = ""
    website: str = ""
    addresses: List[Address] = field(default_factory=list)
    gstin: str = ""
    pan: str = ""
    msme: str = ""  # "YES", "NO" or ""
    udyam: str = ""
    msme_category: str = ""  # "MICRO", "SMALL", "MEDIUM" or ""
    certification: str = ""  # ISO / IATF / other certification: "YES", "NO" or ""
    certification_no: str = ""
    contacts: List[Tuple[str, Optional[Contact]]] = field(default_factory=list)
    transportation: str = ""
    transporter: str = ""
    delivery: str = ""  # "DOOR", "GODOWN" or ""
    products: str = ""
    annual_business: str = ""
    special_requirements: str = ""
    bank_name: str = ""
    account_no: str = ""
    ifsc: str = ""
    documents: Set[str] = field(default_factory=set)
    payment_terms: str = ""
    credit_limit: str = ""
    approved_on: str = ""
    verified_by: str = ""
    approved_by: str = ""
    note: str = ""


def _mask(last4: str, length: int) -> str:
    last4 = (last4 or "")[-4:]
    return "X" * (length - len(last4)) + last4 if last4 else ""


def _same(a: str, b: str) -> bool:
    return bool(a) and " ".join(a.split()).lower() == " ".join((b or "").split()).lower()


def _with_name(name: str, text: str) -> str:
    return "\n".join(p for p in (name, text) if p)


def _date(value) -> str:
    return f"{timezone.localtime(value, IST):%d %b %Y}" if value else ""


def _user(user) -> str:
    return (user.get_full_name() or user.get_username()) if user else ""


def _label_of(choices, code: str) -> str:
    return dict(choices).get(code, "")


def _money(value) -> str:
    return f"{value:,.2f}" if value is not None else ""


def _verified_by(user) -> str:
    """Accounts verify the KYC: the approver's name goes in this box when the approver is in Accounts."""
    if user and any(d.name == "Accounts" for d in departments_of(user)):
        return _user(user)
    return ""


def _contact_table(main: Optional[Contact], others: Sequence[Tuple[str, Contact]]) -> List[Tuple[str, Optional[Contact]]]:
    """The fixed department rows, filled from the contacts; a second contact for a row (or a department with no
    row, such as sales) is added below as its own row."""
    filled = {label: None for label in CONTACT_ROWS}
    extra: List[Tuple[str, Optional[Contact]]] = []

    def place(label: str, contact: Contact):
        if label in filled and filled[label] is None:
            filled[label] = contact
        else:
            extra.append((label.rstrip(" *"), contact))

    if main:
        place(PURCHASE, main)
    for category, contact in others:
        place(ROW_FOR_CATEGORY.get(category, "Other"), contact)
    return [(label, filled[label]) for label in CONTACT_ROWS] + extra


def request_sheet(req: KycRequest) -> Sheet:
    """A KYC request as the customer filled it in."""
    from .link_requests import details_for

    d = details_for(req)
    by_key = link_fields.FIELDS_BY_KEY
    transporter = link_fields.display(by_key["preferred_transporter"], d.preferred_transporter)
    if d.preferred_transporter == "OTHER" and d.other_transporter:
        transporter = d.other_transporter
    main = Contact(d.contact_name, d.contact_designation, ", ".join(link_fields.split_emails(d.contact_email)),
                   d.contact_phone) if d.contact_name else None
    others = [(row.get("department", ""), Contact(row.get("name", ""), row.get("designation", ""),
                                                  ", ".join(link_fields.split_emails(row.get("email", ""))),
                                                  row.get("phone", "")))
              for row in d.extra_contacts or []]
    approved = req.status == KycRequest.STATUS_APPROVED
    when = timezone.localtime(req.submitted_at or timezone.now(), IST)
    by_staff = " by AVIGIRI staff on behalf of the customer" if req.source == KycRequest.SOURCE_STAFF else ""
    customer = req.customer

    def address(prefix, title, same_as="", same=False):
        return Address(title, _with_name(d.company_name, getattr(d, f"{prefix}_address")),
                       city=getattr(d, f"{prefix}_city"), district=getattr(d, f"{prefix}_district"),
                       state=getattr(d, f"{prefix}_state"), pincode=getattr(d, f"{prefix}_pincode"),
                       same_as=same_as, same=same)

    return Sheet(
        customer_code=customer.customer_code or customer.code or "",
        registered_on=_date(req.submitted_at),
        company_name=d.company_name,
        nature_of_company=d.nature_of_company,
        nature_of_business=d.nature_of_business,
        year_established=d.year_established,
        cin=d.cin,
        website=d.website or customer.website,
        addresses=[
            address("company", "Registered / Company Address"),
            address("billing", "Billing Address / Buyer", "Same as Company Address", d.same_billing_as_company),
            address("delivery", "Delivery Address / Consignee", "Same as Billing Address", d.same_delivery_as_billing),
        ],
        gstin=d.gstin,
        pan=_mask(d.pan_last4, 10),
        msme=d.msme_applicable,
        udyam=d.udyam_aadhar_no if d.msme_applicable == "YES" else "",
        msme_category=d.msme_category if d.msme_applicable == "YES" else "",
        certification=d.has_certification,
        certification_no=d.certification_no if d.has_certification == "YES" else "",
        contacts=_contact_table(main, others),
        transportation=d.transportation,
        transporter=transporter,
        delivery=d.delivery_type,
        products=d.products_of_interest,
        annual_business=d.expected_annual_business,
        special_requirements=d.special_requirements,
        bank_name=d.bank_name,
        account_no=_mask(d.account_last4, 12),
        ifsc=d.ifsc,
        documents=set(req.documents.values_list("doc_type", flat=True)),
        payment_terms=req.payment_terms,
        credit_limit=_money(req.credit_limit) if approved else "",
        approved_on=_date(req.reviewed_at) if approved else "",
        verified_by=_verified_by(req.reviewed_by) if approved else "",
        approved_by=_user(req.reviewed_by) if approved else "",
        note=f"Filled online on {when:%d %b %Y, %I:%M %p} IST{by_staff}. "
             "Please check, sign, stamp and upload this form on the KYC link.",
    )


def customer_sheet(customer) -> Sheet:
    """The saved customer record. The customer has one address, so it is the company, billing and delivery
    address; payment terms and the approval come from the latest KYC request that has them."""
    contacts = list(customer.contacts.all())
    main_record = next((c for c in contacts if c.is_purchaser), contacts[0] if contacts else None)

    def as_contact(c) -> Contact:
        return Contact(c.name, c.designation, ", ".join(link_fields.split_emails(c.email)), c.phone)

    others = [(c.category, as_contact(c)) for c in contacts if c is not main_record]
    requests = list(customer.kyc_requests.order_by("-id"))
    terms = customer.payment_terms or next((r.payment_terms for r in requests if r.payment_terms), "")
    approved = next((r for r in requests if r.status == KycRequest.STATUS_APPROVED), None)
    transporter = customer.preferred_transporter or ""
    if transporter == "OTHER":
        transporter = customer.other_transporter or "Other"
    address = _with_name(customer.name, customer.address)
    place = dict(city=customer.city, state=customer.state, pincode=customer.pincode)
    now = timezone.localtime(timezone.now(), IST)
    code = customer.customer_code or customer.code or ""
    return Sheet(
        customer_code=code,
        registered_on=_date(customer.created_at),
        company_name=customer.name,
        website=customer.website,
        addresses=[
            Address("Registered / Company Address", address, **place),
            Address("Billing Address / Buyer", address, same_as="Same as Company Address", same=True, **place),
            Address("Delivery Address / Consignee", address, same_as="Same as Billing Address", same=True, **place),
        ],
        gstin=customer.gstin,
        pan=_mask(customer.pan, 10),
        msme="YES" if customer.msme_applicable else "NO",
        udyam=(customer.udyam_aadhar_no or "") if customer.msme_applicable else "",
        contacts=_contact_table(as_contact(main_record) if main_record else None, others),
        transportation=customer.transportation or "",
        transporter=transporter,
        delivery=customer.delivery_type or "",
        payment_terms=terms,
        credit_limit=_money(customer.credit_limit),
        approved_on=_date(approved.reviewed_at) if approved else "",
        verified_by=_verified_by(approved.reviewed_by) if approved else "",
        approved_by=_user(approved.reviewed_by) if approved else "",
        note=f"Customer {code}. From the KYC record on {now:%d %b %Y, %I:%M %p} IST.",
    )


# ---------------------------------------------------------------------------------------------------- the layout
def _pdf_class():
    from fpdf import FPDF

    class FormPdf(FPDF):
        def header(self):
            logo = Path(settings.BASE_DIR) / "static" / "images" / "avigiri-logo.jpg"
            if logo.exists():
                self.image(str(logo), x=11, y=9, h=16)  # the logo is 2:1, so 32 mm wide
            self.set_text_color(*NAVY)
            self.set_font("Helvetica", "B", 15)
            for i, line in enumerate(COMPANY_LINES):
                self.set_xy(46, 9 + i * 6.6)
                self.cell(0, 6.6, line)
            self.set_xy(46, 23)
            self.set_font("Helvetica", "B", 8)
            self.cell(26, 4, WEBSITE)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*MUTED)
            self.cell(0, 4, CIN)
            self.set_fill_color(*NAVY)
            self.rect(12, 30, 186, 1.1, style="F")
            self.set_fill_color(*LIME)
            self.rect(12, 30, 32, 1.1, style="F")

        def footer(self):
            self.set_fill_color(*NAVY)
            self.rect(12, 281, 186, 0.6, style="F")
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(*MUTED)
            for i, line in enumerate(FOOTER):
                self.set_xy(12, 283 + i * 3.6)
                self.cell(186, 3.5, _latin1(line), align="C")
            self.set_font("Helvetica", "B", 7)
            self.set_text_color(*NAVY)
            # "{nb}" is filled in after layout, so measure with a two-digit stand-in to right-align it.
            label = f"Page {self.page_no()} of "
            self.set_xy(198 - self.get_string_width(label + "99") - 2, 283)
            self.cell(0, 3.5, label + "{nb}")

    return FormPdf


class _Writer:
    M, CW = 12, 186
    TOP, BOTTOM = 35, 277
    GAP, LABEL_H, ROW_GAP, LH = 2.8, 4.0, 2.4, 4.1
    MIN_H = 7.0
    TICK = 3.2

    def __init__(self):
        self.pdf = _pdf_class()(format="A4", unit="mm")
        self.pdf.set_auto_page_break(False)
        self.pdf.set_margins(self.M, 12, self.M)
        self.pdf.add_page()
        self.y = self.TOP

    # -- basics
    def ensure(self, height: float):
        if self.y + height > self.BOTTOM:
            self.pdf.add_page()
            self.y = self.TOP

    def font(self, size: float, style: str = "", color=INK):
        self.pdf.set_font("Helvetica", style, size)
        self.pdf.set_text_color(*color)

    def text(self, x, y, value, size=9, style="", color=INK, w=0, h=4, align="L"):
        self.font(size, style, color)
        self.pdf.set_xy(x, y)
        self.pdf.cell(w, h, _latin1(value), align=align)

    def width(self, value, size, style="") -> float:
        self.pdf.set_font("Helvetica", style, size)
        return self.pdf.get_string_width(_latin1(value))

    def box_height(self, w: float, value: str, min_h: float, size: float = 9, lh: float = LH,
                   pad: float = 2.8) -> float:
        if not value:
            return min_h
        self.font(size)
        lines = self.pdf.multi_cell(w - 2, lh, _latin1(value), dry_run=True, output="LINES")
        return max(min_h, len(lines) * lh + pad)

    def rect(self, x, y, w, h, fill=FIELD, border=LINE, radius=0.7):
        self.pdf.set_draw_color(*border)
        self.pdf.set_line_width(0.2)
        self.pdf.set_fill_color(*fill)
        self.pdf.rect(x, y, w, h, style="DF", round_corners=radius > 0, corner_radius=radius)

    def box(self, x, y, w, h, value="", fill=FIELD):
        self.rect(x, y, w, h, fill=fill)
        if value:
            self.font(9)
            self.pdf.set_xy(x + 1, y + 1.4)
            self.pdf.multi_cell(w - 2, self.LH, _latin1(value))

    def label(self, x, y, value, required=False):
        value = value.upper()
        self.text(x, y, value, 6.3, "B", MUTED, h=3)
        if required:
            self.text(x + self.width(value, 6.3, "B") + 0.6, y, "*", 6.3, "B", RED, h=3)

    def check(self, x, y, value, ticked=False, size=8, style="", color=INK) -> float:
        t = self.TICK
        self.pdf.set_draw_color(*NAVY)
        self.pdf.set_line_width(0.25)
        self.pdf.set_fill_color(*WHITE)
        self.pdf.rect(x, y, t, t, style="DF")
        if ticked:
            self.pdf.set_line_width(0.55)
            self.pdf.line(x + 0.6, y + 1.7, x + 1.35, y + 2.55)
            self.pdf.line(x + 1.35, y + 2.55, x + 2.7, y + 0.6)
        style = "B" if ticked else style
        self.text(x + t + 1.4, y - 0.4, value, size, style, NAVY if ticked else color)
        return x + t + 1.4 + self.width(value, size, style) + 5

    # -- blocks
    def section(self, number: int, title: str, note: str = "", keep: float = 18):
        self.ensure(10 + keep)
        self.y += 1
        h = 6.7
        self.rect(self.M, self.y, self.CW, h, fill=NAVY, border=NAVY, radius=1)
        self.rect(self.M, self.y, 8.5, h, fill=LIME, border=LIME, radius=1)
        self.rect(self.M + 7, self.y, 1.5, h, fill=LIME, border=LIME, radius=0)
        self.text(self.M, self.y + 1.4, str(number), 9.5, "B", NAVY_DARK, w=8.5, align="C")
        self.text(self.M + 11, self.y + 1.4, title.upper(), 9.5, "B", WHITE)
        if note:
            self.text(self.M, self.y + 1.6, note, 7, "I", PALE, w=self.CW - 3, align="R")
        self.y += h + 3

    def row(self, cells, min_h: float = MIN_H):
        """cells: (label, fraction, kind, value). kind is "text" (value: str) or "choice" (value: (options,
        ticked labels)). A label ending in "*" is mandatory."""
        free = self.CW - self.GAP * (len(cells) - 1)
        widths = [free * c[1] for c in cells]
        h = max(self.box_height(w, c[3], min_h) if c[2] == "text" else min_h for c, w in zip(cells, widths))
        self.ensure(self.LABEL_H + h + self.ROW_GAP)
        x = self.M
        for (lab, _, kind, value), w in zip(cells, widths):
            self.label(x, self.y, lab.rstrip(" *"), lab.endswith("*"))
            top = self.y + self.LABEL_H
            if kind == "text":
                self.box(x, top, w, h, value)
            else:
                options, ticked = value
                self.rect(x, top, w, h, fill=WHITE)
                cx = x + 2.4
                for option in options:
                    cx = self.check(cx, top + (h - self.TICK) / 2, option, option in ticked)
            x += w + self.GAP
        self.y += self.LABEL_H + h + self.ROW_GAP

    def address(self, a: Address, required=True):
        h = self.box_height(self.CW, a.text, 10.5)
        has_place = any((a.city, a.state, a.pincode))
        self.ensure(self.LABEL_H + h + self.ROW_GAP + (self.LABEL_H + self.MIN_H + self.ROW_GAP if has_place else 0))
        self.label(self.M, self.y, a.title, required)
        if a.same_as:
            w = self.width(a.same_as, 7, "B")
            self.check(self.M + self.CW - w - self.TICK - 3.4, self.y - 0.6, a.same_as, a.same, size=7, style="B",
                       color=NAVY)
        self.box(self.M, self.y + self.LABEL_H, self.CW, h, a.text)
        self.y += self.LABEL_H + h + self.ROW_GAP
        if has_place:
            self.row([("City / Town *", .32, "text", a.city), ("District", .24, "text", a.district),
                      ("State *", .24, "text", a.state), ("PIN Code *", .20, "text", a.pincode)])

    def contact_table(self, rows: List[Tuple[str, Optional[Contact]]]):
        cols = (("Department", .19), ("Name", .21), ("Designation", .16), ("Email ID", .26), ("Phone / Mobile", .18))
        widths = [self.CW * f for _, f in cols]
        head_h = 5.6

        def head():
            self.rect(self.M, self.y, self.CW, head_h, fill=TINT, radius=0)
            x = self.M
            for (title, _), w in zip(cols, widths):
                self.text(x + 1.8, self.y + 1.3, title.upper(), 6.6, "B", NAVY)
                x += w
            self.y += head_h

        size, lh = 8.4, 3.7

        def height(values):
            return max(5.6, *(self.box_height(w, v, 5.6, size, lh, 1.9) for w, v in zip(widths[1:], values)))

        first = rows[0][1] if rows else None
        self.ensure(head_h + height(self._contact_values(first)))
        head()
        for department, contact in rows:
            values = self._contact_values(contact)
            h = height(values)
            if self.y + h > self.BOTTOM:
                self.pdf.add_page()
                self.y = self.TOP
                head()
            self.rect(self.M, self.y, widths[0], h, fill=WHITE, radius=0)
            self.text(self.M + 1.8, self.y + (h - 4) / 2, department, 7.6, "B")
            x = self.M + widths[0]
            for w, value in zip(widths[1:], values):
                self.rect(x, self.y, w, h, fill=FIELD, radius=0)
                if value:
                    self.font(size)
                    lines = self.pdf.multi_cell(w - 2, lh, _latin1(value), dry_run=True, output="LINES")
                    self.pdf.set_xy(x + 1, self.y + (h - len(lines) * lh) / 2)
                    self.pdf.multi_cell(w - 2, lh, _latin1(value))
                x += w
            self.y += h
        self.y += self.ROW_GAP + 1

    @staticmethod
    def _contact_values(c: Optional[Contact]) -> List[str]:
        return [c.name, c.designation, c.email, c.phone] if c else ["", "", "", ""]

    def paragraph(self, value: str, size=7.8):
        self.font(size)
        lines = self.pdf.multi_cell(self.CW, 3.8, _latin1(value), dry_run=True, output="LINES")
        self.ensure(len(lines) * 3.8 + 2)
        self.pdf.set_xy(self.M, self.y)
        self.pdf.multi_cell(self.CW, 3.8, _latin1(value))
        self.y += len(lines) * 3.8 + 2


def _title_block(w: _Writer, s: Sheet):
    y = w.y
    w.text(w.M, y, TITLE, 17, "B", NAVY, h=7)
    w.text(w.M, y + 8, f"{SUBTITLE}  -  details as registered with AVIGIRI", 8.5, "", MUTED)
    w.text(w.M, y + 12.4, "Please check every detail, complete any empty box by hand, then sign and stamp.", 7,
           "I", RED)
    bw = 70
    bx = w.M + w.CW - bw
    w.rect(bx, y, bw, 14, fill=TINT, radius=1)
    for i, (lab, value) in enumerate((("Customer Code", s.customer_code), ("Date of Registration", s.registered_on))):
        yy = y + 1.6 + i * 6
        w.text(bx + 2.5, yy + 0.7, lab.upper(), 6.4, "B", NAVY, h=3.5)
        w.rect(bx + 38, yy, 29.5, 4.8, fill=WHITE, radius=0)
        w.text(bx + 39, yy + 0.4, value, 8, "B", INK, h=4)
    w.y += 19


def _declaration(w: _Writer, s: Sheet):
    w.section(8, "Declaration", keep=46)
    w.paragraph(DECLARATION)
    w.y += 1
    w.row([("Place", .26, "text", ""), ("Date", .20, "text", ""), ("Name of Authorised Signatory", .54, "text", "")])
    left = (w.CW - w.GAP) * .46
    sx = w.M + left + w.GAP
    sw = w.CW - left - w.GAP
    w.label(w.M, w.y, "Designation")
    w.box(w.M, w.y + w.LABEL_H, left, w.MIN_H)
    w.label(sx, w.y, "Signature of Authorised Signatory with Company Seal")
    w.pdf.set_draw_color(*NAVY)
    w.pdf.set_line_width(0.3)
    w.pdf.set_dash_pattern(dash=1.1, gap=0.8)
    w.pdf.set_fill_color(*WHITE)
    w.pdf.rect(sx, w.y + w.LABEL_H, sw, 14, style="D")
    w.pdf.set_dash_pattern()
    if s.note:
        w.font(6.6, "I", MUTED)
        w.pdf.set_xy(w.M, w.y + w.LABEL_H + w.MIN_H + 2)
        w.pdf.multi_cell(left, 3.2, _latin1(s.note))
    w.y += w.LABEL_H + 14 + w.ROW_GAP + 1


def _office_use(w: _Writer, s: Sheet):
    h = 20.5
    w.ensure(h + 1)
    w.rect(w.M, w.y, w.CW, h, fill=TINT, border=NAVY, radius=1)
    w.text(w.M + 3, w.y + 1.8, "FOR AVIGIRI OFFICE USE ONLY", 7.5, "B", NAVY, h=3.5)
    w.pdf.set_fill_color(*LIME)
    w.pdf.rect(w.M + 3, w.y + 5.4, 43, 0.5, style="F")
    cols = (("Date of Approval", .17, s.approved_on), ("Payment Terms", .22, s.payment_terms),
            ("Credit Limit (Rs.)", .17, s.credit_limit), ("Verified by (Accounts)", .22, s.verified_by),
            ("Approved by", .22, s.approved_by))
    inner = w.CW - 6 - w.GAP * (len(cols) - 1)
    x = w.M + 3
    for lab, frac, value in cols:
        cw = inner * frac
        w.text(x, w.y + 7.4, lab.upper(), 6, "B", MUTED, h=3)
        w.rect(x, w.y + 11, cw, 7, fill=WHITE)
        w.font(8.2, "B")
        w.pdf.set_xy(x + 1, w.y + 12.4)
        w.pdf.multi_cell(cw - 2, 3.6, _latin1(value), max_line_height=3.6)
        x += cw + w.GAP
    w.y += h


def render_sheet(s: Sheet) -> bytes:
    w = _Writer()
    w.pdf.set_title(f"{TITLE} - {_latin1(s.company_name)}" if s.company_name else TITLE)
    w.pdf.set_author("AVIGIRI Urethane & Rubber Industries Pvt. Ltd.")
    _title_block(w, s)

    w.section(1, "Company Details")
    w.row([("Name of the Company / Firm (as per GST) *", 1, "text", s.company_name)])
    w.row([("Nature of Company *", 1, "choice",
            (NATURE_OF_COMPANY, {_label_of(link_fields.NATURE_OF_COMPANY, s.nature_of_company)}))])
    w.row([("Nature of Business *", 1, "choice",
            (NATURE_OF_BUSINESS, {_label_of(link_fields.NATURE_OF_BUSINESS, s.nature_of_business)}))])
    w.row([("Year of Establishment", .22, "text", s.year_established), ("CIN / Registration No.", .43, "text", s.cin),
           ("Website", .35, "text", s.website)])

    w.section(2, "Addresses", "Name of the company is printed on every address", keep=24)
    for a in s.addresses:
        w.address(a)

    w.section(3, "Statutory & Tax Details")
    msme = {"YES": {"Yes"}, "NO": {"No"}}.get(s.msme, set())
    w.row([("GSTIN No. *", .40, "text", s.gstin), ("PAN No. *", .30, "text", s.pan),
           ("GST State Code", .30, "text", s.gstin[:2] if len(s.gstin) >= 2 else "")])
    w.row([("MSME Applicable *", .30, "choice", (("Yes", "No"), msme)),
           ("Udyam Registration No.", .40, "text", s.udyam),
           ("MSME Category", .30, "choice",
            (MSME_CATEGORIES, {_label_of(link_fields.MSME_CATEGORIES, s.msme_category)}))])
    certification = {"YES": {"Yes"}, "NO": {"No"}}.get(s.certification, set())
    w.row([("ISO / IATF / Other Certification", .30, "choice", (("Yes", "No"), certification)),
           ("If Yes, Certificate No. / Standard", .70, "text", s.certification_no)])

    w.section(4, "Contact Details", "Purchase contact is mandatory", keep=30)
    w.contact_table(s.contacts or _contact_table(None, []))

    w.section(5, "Commercial & Logistics")
    delivery = {label for key, label in DELIVERY_OPTIONS if key == s.delivery}
    w.row([("Transportation *", .5, "text", s.transportation),
           ("Delivery Type *", .5, "choice", ([label for _, label in DELIVERY_OPTIONS], delivery))])
    w.row([("Preferred Transporter (Default: ARC)", .32, "text", s.transporter),
           ("Products / Applications of Interest", .40, "text", s.products),
           ("Expected Annual Business (Rs. Lacs)", .28, "text", s.annual_business)])
    w.row([("Special Requirements (packing, labelling, documents, test certificates, delivery timings, etc.)", 1,
            "text", s.special_requirements)], min_h=9)

    w.section(6, "Bank Details", "Account number shows only the last four digits")
    w.row([("Bank Name & Branch", .40, "text", s.bank_name), ("Account No.", .34, "text", s.account_no),
           ("IFSC Code", .26, "text", s.ifsc)])

    w.section(7, "Documents to be Enclosed", "Ticked: already uploaded on the KYC link", keep=18)
    for i, (key, lab) in enumerate(DOCUMENT_ROWS):
        col, r = i % 2, i // 2
        w.check(w.M + 2 + col * (w.CW / 2), w.y + r * 5.3, lab, key in s.documents)
    w.y += 3 * 5.3 + 2

    _declaration(w, s)
    _office_use(w, s)
    return bytes(w.pdf.output())


# ---------------------------------------------------------------------------------------------------- entry points
def render(req: KycRequest) -> bytes:
    """A submitted KYC request, for the customer to sign and stamp."""
    return render_sheet(request_sheet(req))


def render_customer(customer) -> bytes:
    """The customer's saved details in the AVIGIRI Customer Registration Form."""
    return render_sheet(customer_sheet(customer))


def stored_name(req: KycRequest, when) -> str:
    """Customer-Registration-<customer code>-<yyyymmdd-hhmm>.pdf: the name a stored version is kept under."""
    code = req.customer.customer_code or req.customer.code or str(req.customer_id)
    safe = "".join(ch for ch in code if ch.isalnum() or ch in "-_")
    return f"Customer-Registration-{safe}-{when:%Y%m%d-%H%M}.pdf"


def customer_filename(customer) -> str:
    code = customer.customer_code or customer.code or str(customer.pk)
    return f"Customer-Registration-{''.join(ch for ch in code if ch.isalnum() or ch in '-_')}.pdf"


def filename(req: KycRequest) -> str:
    return customer_filename(req.customer)

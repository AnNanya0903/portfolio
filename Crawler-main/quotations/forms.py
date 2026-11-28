from __future__ import annotations

import os

from django import forms
from django.conf import settings

from kyc import links as kyc_links

MAX_FILE_BYTES = 20 * 1024 * 1024
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".csv", ".ods")

_TEXT = {"class": "form-control form-control-sm"}
_FILE = {"class": "form-control form-control-sm"}


def _check_size(upload, label, limit=MAX_FILE_BYTES):
    if upload.size > limit:
        raise forms.ValidationError(f"The {label} is larger than {limit // (1024 * 1024)} MB.")


def max_pdf_bytes() -> int:
    return int(getattr(settings, "QUOTATION_MAX_PDF_MB", 10) or 10) * 1024 * 1024


def clean_pdf(upload):
    """A real PDF: right extension, the file starts with %PDF, and at most QUOTATION_MAX_PDF_MB."""
    _check_size(upload, "PDF", max_pdf_bytes())
    start = upload.read(5)
    upload.seek(0)
    if os.path.splitext(upload.name or "")[1].lower() != ".pdf" or not start.startswith(b"%PDF"):
        raise forms.ValidationError("Please upload the quotation as a PDF file.")
    return upload


def clean_enquiry_number(value, request=None) -> str:
    """'' or the number of an enquiry that exists in the enquiry service ("ENQ-0012 · Acme · ..." from the
    picker is accepted too: only the number is kept)."""
    from .enquiry_link import LinkError, check

    text = " ".join(str(value or "").split())
    if not text:
        return ""
    number = text.split(" ", 1)[0].rstrip("·,;")
    try:
        return check(number, request)
    except LinkError as exc:
        raise forms.ValidationError(str(exc)) from exc


def clean_excel(upload):
    if not upload:
        return upload
    _check_size(upload, "Excel file")
    if os.path.splitext(upload.name or "")[1].lower() not in EXCEL_EXTENSIONS:
        raise forms.ValidationError("Please upload an Excel file (.xlsx, .xls, .xlsm, .ods or .csv).")
    return upload


class VersionForm(forms.Form):
    """A new version: the PDF for the customer and, optionally, the Excel working behind it."""

    pdf = forms.FileField(label="Quotation PDF", widget=forms.ClearableFileInput(
        attrs={**_FILE, "accept": "application/pdf,.pdf"}))
    excel = forms.FileField(label="Excel working", required=False, widget=forms.ClearableFileInput(
        attrs={**_FILE, "accept": ",".join(EXCEL_EXTENSIONS)}))
    price_note = forms.CharField(label="Quoted price", required=False, max_length=255, widget=forms.TextInput(
        attrs={**_TEXT, "placeholder": "e.g. Rs 42 per piece for 5,000 pieces"}))
    note = forms.CharField(label="Note for management", required=False, max_length=2000, widget=forms.Textarea(
        attrs={**_TEXT, "rows": 2, "placeholder": "What changed in this version"}))

    def clean_pdf(self):
        return clean_pdf(self.cleaned_data["pdf"])

    def clean_excel(self):
        return clean_excel(self.cleaned_data.get("excel"))


class QuotationForm(VersionForm):
    """A new quotation with its first version."""

    customer_code = forms.CharField(label="Customer code (KYC)", required=False, max_length=30, widget=forms.TextInput(
        attrs={**_TEXT, "placeholder": "e.g. AV-0001", "autocomplete": "off", "list": "kycCustomerOptions"}))
    customer_name = forms.CharField(label="Customer name", required=False, max_length=255,
                                    widget=forms.TextInput(attrs=_TEXT))
    enquiry_id = forms.CharField(label="Enquiry number", required=False, max_length=64, widget=forms.TextInput(
        attrs={**_TEXT, "placeholder": "Search ENQ number, customer or subject", "autocomplete": "off",
               "list": "enquiryOptions", "data-enquiry-picker": "1"}))
    enquiry_reference = forms.CharField(label="Customer reference (AG-QR)", required=False, max_length=120,
                                        widget=forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. AG-QR-ALSTOM-235"}))
    title = forms.CharField(label="Quotation for", max_length=255, widget=forms.TextInput(
        attrs={**_TEXT, "placeholder": "e.g. Door seal gasket, drawing DS-114"}))

    field_order = ["customer_code", "customer_name", "enquiry_id", "enquiry_reference", "title", "pdf", "excel",
                   "price_note", "note"]

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    def clean_enquiry_id(self):
        return clean_enquiry_number(self.cleaned_data.get("enquiry_id"), self.request)

    def clean_customer_code(self):
        code = kyc_links.normalize_code(self.cleaned_data.get("customer_code"))
        self.customer = kyc_links.customer_for_code(code) if code else None
        if code and self.customer is None:
            raise forms.ValidationError(f"Customer code {code} is not in KYC.")
        return code

    def clean(self):
        data = super().clean()
        name = " ".join((data.get("customer_name") or "").split())
        customer = getattr(self, "customer", None)
        if not name and customer is not None:
            name = customer.name
        if not name and "customer_code" not in self.errors:
            self.add_error("customer_name", "Enter the customer name or pick a customer code.")
        data["customer_name"] = name
        return data


class DecisionForm(forms.Form):
    APPROVE = "approve"
    REVISE = "revise"

    decision = forms.ChoiceField(choices=((APPROVE, "Price is good, OK to send to customer"),
                                          (REVISE, "Price needs revision")))
    comment = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(
        attrs={**_TEXT, "rows": 2, "placeholder": "What should change (the reason is needed when asking for a revision)"}))
    corrected_excel = forms.FileField(label="Corrected Excel", required=False, widget=forms.ClearableFileInput(
        attrs={**_FILE, "accept": ",".join(EXCEL_EXTENSIONS)}))

    def clean_corrected_excel(self):
        return clean_excel(self.cleaned_data.get("corrected_excel"))

    def clean(self):
        data = super().clean()
        if data.get("decision") == self.REVISE:
            if not (data.get("comment") or "").strip():
                self.add_error("comment", "Give the reason: say what should change.")
        elif data.get("corrected_excel"):
            self.add_error("corrected_excel", "A corrected Excel can only be attached when asking for a revision.")
        return data


class ChangeEnquiryForm(forms.Form):
    """Link a quotation to another enquiry (or unlink it). The change is written in the tracker."""

    enquiry_id = forms.CharField(label="Enquiry number", required=False, max_length=200, widget=forms.TextInput(
        attrs={**_TEXT, "placeholder": "Search ENQ number, customer or subject", "autocomplete": "off",
               "list": "enquiryOptions", "data-enquiry-picker": "1"}))

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    def clean_enquiry_id(self):
        return clean_enquiry_number(self.cleaned_data.get("enquiry_id"), self.request)

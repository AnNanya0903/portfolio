import re

from django import forms
from django.core.validators import validate_email

from . import logistics
from .models import (CONTACT_DESIGNATIONS, DESIGNATION_OTHER, Customer, CustomerContact, Department,
                     active_departments)

_TEXT = {"class": "form-control form-control-sm"}
_SELECT = {"class": "form-select form-select-sm"}

_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9/_-]*$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _compact_upper(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def _with_departments(data, ids):
    """A copy of the submitted data whose "departments" are the given ids."""
    data = data.copy()
    if hasattr(data, "setlist"):
        data.setlist("departments", [str(pk) for pk in ids])
    else:
        data["departments"] = list(ids)
    return data


class CustomerForm(forms.ModelForm):
    # Yes / No buttons; saved as True / False. Also accepts "Yes", "No", True, False from imports.
    msme_applicable = forms.TypedChoiceField(
        label="MSME applicable", choices=(("yes", "Yes"), ("no", "No")), coerce=lambda v: v == "yes",
        empty_value=False, required=False, initial="no",
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}))
    # Ticked to save a customer whose company name another customer already has (e.g. a branch).
    same_name_ok = forms.BooleanField(
        required=False, label="This is a different company with the same name",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    # "Select all departments": ticked by default; unticking a department clears it.
    all_departments = forms.BooleanField(
        required=False, initial=True, label="All departments",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    class Meta:
        model = Customer
        fields = ["code", "name", "address", "city", "state", "pincode", "country",
                  "gstin", "pan", "phone", "website", "notes", "departments",
    "transportation",
    "preferred_transporter",
    "other_transporter",
    "delivery_type",
    "msme_applicable",
    "udyam_aadhar_no"]

        widgets = {
            "code": forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. AV-0001", "autocomplete": "off"}),
            "name": forms.TextInput(attrs=_TEXT),
            "address": forms.Textarea(attrs={**_TEXT, "rows": 3}),
            "city": forms.TextInput(attrs=_TEXT),
            "state": forms.TextInput(attrs=_TEXT),
            "pincode": forms.TextInput(attrs=_TEXT),
            "country": forms.TextInput(attrs=_TEXT),
            "gstin": forms.TextInput(attrs={**_TEXT, "placeholder": "15 characters"}),
            "pan": forms.TextInput(attrs={**_TEXT, "placeholder": "10 characters"}),
            "phone": forms.TextInput(attrs=_TEXT),
            "website": forms.TextInput(attrs=_TEXT),
            "notes": forms.Textarea(attrs={**_TEXT, "rows": 3}),
            "departments": forms.CheckboxSelectMultiple(attrs={"class": "form-check-input kyc-dept-box"}),
            "transportation": forms.TextInput(attrs={**_TEXT, "list": "kycTransporters",
                                                     "placeholder": "e.g. VRL-TO PAY-DOOR"}),
            "preferred_transporter": forms.Select(attrs=_SELECT),
            "other_transporter": forms.TextInput(attrs={**_TEXT, "placeholder": "Name of the transporter"}),
            "delivery_type": forms.Select(attrs=_SELECT),
            "udyam_aadhar_no": forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. UDYAM-TN-12-0012345"}),
        }
        labels = {
            "transportation": "Transportation",
            "preferred_transporter": "Preferred transporter",
            "other_transporter": "Other transporter",
            "delivery_type": "Door / Go-Down delivery",
            "udyam_aadhar_no": "Udyam Aadhaar No.",
        }

    def __init__(self, *args, check_same_name: bool = False, hide_pan: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        # "Team" lists the active departments, plus any already assigned that was switched off since.
        assigned = list(self.instance.departments.values_list("pk", flat=True)) if self.instance.pk else []
        field = self.fields["departments"]
        field.queryset = Department.objects.filter(is_active=True) | Department.objects.filter(pk__in=assigned)
        everyone = set(active_departments().values_list("pk", flat=True))
        field.required = bool(everyone)  # nothing to choose from until the departments are seeded
        field.error_messages["required"] = "Select at least one department."
        field.label = "Team"
        if not self.is_bound:
            # A new customer starts with every department ticked; an existing one shows what it has.
            self.initial["departments"] = assigned if self.instance.pk else sorted(everyone)
            self.initial["all_departments"] = everyone <= set(self.initial["departments"])
        elif (self.data.get("all_departments")
              or not (self.data.get("departments") or self.data.get("departments_present"))):
            # "All departments" ticked, or nothing about the team was sent (an import or an API call):
            # every active department. The customer form always sends "departments_present", so ticking
            # none of them there is still an error.
            self.data = _with_departments(self.data, sorted(everyone))
        # A user who may not see PAN in full cannot read it back through the edit form either. The field is
        # left out, so saving keeps the PAN that is stored. (Typing a PAN for a new customer is still allowed.)
        if hide_pan and self.instance and self.instance.pk and "pan" in self.fields:
            del self.fields["pan"]
        # Pages where a person saves a customer warn about the same company name; sheet imports do not
        # (each row has its own customer code). A GSTIN that another customer has is always refused.
        self.check_same_name = check_same_name
        self.same_name_customers: list = []
        self.gstin_customer = None
        # Show the saved MSME value on the Yes / No buttons, and imports' "Yes" / "No" / True / False.
        if self.instance and self.instance.pk and "msme_applicable" not in self.initial:
            self.initial["msme_applicable"] = "yes" if self.instance.msme_applicable else "no"
        elif "msme_applicable" in self.initial:
            self.initial["msme_applicable"] = "yes" if logistics.msme(self.initial["msme_applicable"]) else "no"
        if self.data and "msme_applicable" in self.data:
            raw = self.data.get("msme_applicable")
            if raw not in ("yes", "no"):
                data = self.data.copy()
                data["msme_applicable"] = "yes" if logistics.msme(raw) else "no"
                self.data = data
        self.fields["preferred_transporter"].required = False
        self.fields["preferred_transporter"].choices = [
            choice for choice in self.fields["preferred_transporter"].choices if choice[0] != ""]
        if not self.instance.pk and not self.initial.get("preferred_transporter"):
            self.initial["preferred_transporter"] = logistics.DEFAULT_TRANSPORTER

    def clean_transportation(self):
        return " ".join((self.cleaned_data.get("transportation") or "").split())

    def clean_other_transporter(self):
        return " ".join((self.cleaned_data.get("other_transporter") or "").split())

    def clean_udyam_aadhar_no(self):
        number = logistics.udyam(self.cleaned_data.get("udyam_aadhar_no"))
        if number and logistics.udyam_looks_wrong(number):
            raise forms.ValidationError("A Udyam number looks like UDYAM-TN-12-0012345.")
        return number

    def clean(self):
        cleaned = super().clean()
        transporter = cleaned.get("preferred_transporter") or logistics.DEFAULT_TRANSPORTER
        cleaned["preferred_transporter"] = transporter
        if transporter == "OTHER":
            if not cleaned.get("other_transporter"):
                self.add_error("other_transporter", "Please enter the transporter's name.")
        else:
            cleaned["other_transporter"] = ""  # only kept for "Other"
        if cleaned.get("msme_applicable") and not cleaned.get("udyam_aadhar_no"):
            self.add_error("udyam_aadhar_no", "Required when MSME applies.")
        self._check_duplicates(cleaned)
        return cleaned

    def _check_duplicates(self, cleaned) -> None:
        others = Customer.objects.exclude(pk=self.instance.pk) if self.instance.pk else Customer.objects.all()
        gstin = cleaned.get("gstin")
        if gstin:
            self.gstin_customer = others.filter(gstin=gstin).first()
            if self.gstin_customer:
                self.add_error("gstin", f"This GSTIN is already used by customer {self.gstin_customer.code} "
                                        f"({self.gstin_customer.name}). The same GSTIN means the same business.")
        name = " ".join((cleaned.get("name") or "").split())
        if self.check_same_name and name and not cleaned.get("same_name_ok"):
            self.same_name_customers = list(others.filter(name__iexact=name).order_by("code")[:5])
            if self.same_name_customers:
                self.add_error("name", "Another customer already has this company name. Check below that it is "
                                       "a different company, then tick the box to save.")

    def clean_code(self):
        code = _compact_upper(self.cleaned_data.get("code"))
        if not _CODE_RE.match(code):
            raise forms.ValidationError("Use letters, numbers and - / _ only, for example AV-0001.")
        return code

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_gstin(self):
        gstin = _compact_upper(self.cleaned_data.get("gstin"))
        if gstin and not _GSTIN_RE.match(gstin):
            raise forms.ValidationError("A GSTIN has 15 characters and starts with the 2-digit state code.")
        return gstin

    def clean_pan(self):
        pan = _compact_upper(self.cleaned_data.get("pan"))
        if pan and not _PAN_RE.match(pan):
            raise forms.ValidationError("A PAN has 10 characters, for example ABCDE1234F.")
        return pan


class CustomerContactForm(forms.ModelForm):
    """One person at a customer. The mobile number is required and must be 10 digits. The designation is a
    dropdown; "Other" shows a box to type it. Email addresses are checked one by one."""

    designation = forms.ChoiceField(
        label="Designation", required=True,
        choices=[("", "Select a designation")] + [(d, d) for d in CONTACT_DESIGNATIONS],
        widget=forms.Select(attrs=_SELECT))
    designation_other = forms.CharField(
        label="Other designation", required=False, max_length=200,
        widget=forms.TextInput(attrs={**_TEXT, "placeholder": "Type the designation"}))
    phone = forms.CharField(label="Mobile number", max_length=40, required=True,
                            widget=forms.TextInput(attrs={**_TEXT, "inputmode": "numeric", "placeholder": "10 digits"}))
    alternate_phone = forms.CharField(
        label="Alternate number", max_length=40, required=False,
        widget=forms.TextInput(attrs={**_TEXT, "inputmode": "numeric", "placeholder": "Optional, 10 digits"}))

    class Meta:
        model = CustomerContact
        fields = ["category", "name", "designation", "email", "phone", "alternate_phone", "is_purchaser", "notes"]
        labels = {"is_purchaser": "Primary contact", "notes": "Remarks", "category": "Team"}
        widgets = {
            "category": forms.Select(attrs=_SELECT),
            "name": forms.TextInput(attrs=_TEXT),
            "email": forms.EmailInput(attrs=_TEXT),
            "is_purchaser": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.TextInput(attrs={**_TEXT, "placeholder": "Optional"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A saved designation that is not in the list was typed in by someone: show it as "Other" with the text.
        if self.instance.pk and self.instance.designation and not self.is_bound:
            if self.instance.designation in CONTACT_DESIGNATIONS:
                self.initial["designation"] = self.instance.designation
            else:
                self.initial["designation"] = DESIGNATION_OTHER
                self.initial["designation_other"] = self.instance.designation

    def clean_name(self):
        return " ".join((self.cleaned_data.get("name") or "").split())

    def clean_phone(self):
        return _ten_digits(self.cleaned_data.get("phone"), "The mobile number")

    def clean_alternate_phone(self):
        value = (self.cleaned_data.get("alternate_phone") or "").strip()
        return _ten_digits(value, "The alternate number") if value else ""

    def clean_email(self):
        raw = (self.cleaned_data.get("email") or "").strip()
        if not raw:
            return raw
        addresses = []
        for part in raw.split(","):
            part = part.strip().lower()
            if not part:
                continue
            try:
                validate_email(part)
            except forms.ValidationError:
                raise forms.ValidationError(f"“{part}” is not a valid email address.")
            if part not in addresses:
                addresses.append(part)
        return ", ".join(addresses)

    def clean(self):
        data = super().clean()
        if data.get("designation") == DESIGNATION_OTHER:
            other = " ".join((data.get("designation_other") or "").split())
            if other:
                data["designation"] = other
            else:
                self.add_error("designation_other", "Type the designation.")
        if data.get("is_purchaser") and not data.get("email") and "email" not in self.errors:
            self.add_error("email", "The primary contact needs an email address: enquiry emails go to this person.")
        return data


def _ten_digits(value: str, label: str) -> str:
    digits = re.sub(r"[\s-]", "", value or "")
    if not re.fullmatch(r"[0-9]{10}", digits):
        raise forms.ValidationError(f"{label} must be 10 digits, for example 9876543210.")
    return digits


DOCUMENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}
DOCUMENT_MAX_BYTES = 15 * 1024 * 1024
SHEET_MAX_BYTES = 5 * 1024 * 1024


class DocumentUploadForm(forms.Form):
    document = forms.FileField(
        required=False, label="Photo or PDF of the KYC form",
        widget=forms.ClearableFileInput(attrs={"class": "form-control form-control-sm",
                                               "accept": "image/jpeg,image/png,image/webp,image/heic,application/pdf"}))
    pasted_text = forms.CharField(
        required=False, max_length=20000, label="Or paste the details as text",
        widget=forms.Textarea(attrs={**_TEXT, "rows": 8,
                                     "placeholder": "Paste from an email, a Word file or an old sheet"}))

    def clean_document(self):
        document = self.cleaned_data.get("document")
        if not document:
            return document
        if (getattr(document, "content_type", "") or "").lower() not in DOCUMENT_TYPES:
            raise forms.ValidationError("Upload a photo (JPG, PNG, WEBP, HEIC) or a PDF.")
        if document.size > DOCUMENT_MAX_BYTES:
            raise forms.ValidationError("This file is larger than 15 MB.")
        return document

    def clean(self):
        data = super().clean()
        if not data.get("document") and not (data.get("pasted_text") or "").strip() and not self.errors:
            raise forms.ValidationError("Choose a photo or PDF, or paste the details as text.")
        return data


class SheetUploadForm(forms.Form):
    sheet = forms.FileField(
        label="Excel or CSV file",
        widget=forms.ClearableFileInput(attrs={"class": "form-control form-control-sm", "accept": ".xlsx,.csv"}))

    def clean_sheet(self):
        sheet = self.cleaned_data["sheet"]
        if sheet.size > SHEET_MAX_BYTES:
            raise forms.ValidationError("This file is larger than 5 MB.")
        return sheet


class ImportContactForm(CustomerContactForm):
    """A contact read from a document or an Excel sheet. It keeps the rules imports always had: the designation
    is free text, the mobile number is optional and not checked, and a contact needs an email or a phone number.
    The strict rules of the Contact Details form apply only to people typing a contact in."""

    designation = forms.CharField(label="Designation", required=False, max_length=200,
                                  widget=forms.TextInput(attrs=_TEXT))
    phone = forms.CharField(label="Mobile number", max_length=40, required=False,
                            widget=forms.TextInput(attrs=_TEXT))
    alternate_phone = forms.CharField(label="Alternate number", max_length=40, required=False,
                                      widget=forms.TextInput(attrs=_TEXT))

    def __init__(self, *args, **kwargs):
        super(CustomerContactForm, self).__init__(*args, **kwargs)  # skip the designation list set-up
        self.fields.pop("designation_other", None)

    def clean_phone(self):
        return (self.cleaned_data.get("phone") or "").strip()

    def clean_alternate_phone(self):
        return (self.cleaned_data.get("alternate_phone") or "").strip()

    def clean(self):
        data = forms.ModelForm.clean(self)
        if not data.get("email") and not (data.get("phone") or "").strip() and "email" not in self.errors:
            raise forms.ValidationError("Enter an email address or a phone number for this contact.")
        if data.get("is_purchaser") and not data.get("email") and "email" not in self.errors:
            self.add_error("email", "The primary contact needs an email address: enquiry emails go to this person.")
        return data


ContactFormSet = forms.formset_factory(ImportContactForm, extra=0, can_delete=True)


class VerificationSendForm(forms.Form):
    """Which of the customer's contacts get the verification email."""

    recipients = forms.MultipleChoiceField(
        label="Send to", widget=forms.CheckboxSelectMultiple,
        error_messages={"required": "Choose at least one person to send the email to."})

    def __init__(self, *args, contacts=(), **kwargs):
        super().__init__(*args, **kwargs)
        seen = {}
        for contact in contacts:
            if contact.email and contact.email not in seen:
                role = " (purchaser)" if contact.is_purchaser else ""
                seen[contact.email] = f"{contact.name}{role} · {contact.get_category_display()} · {contact.email}"
        self.fields["recipients"].choices = list(seen.items())
        if not self.is_bound:
            purchasers = [c.email for c in contacts if c.is_purchaser and c.email]
            self.fields["recipients"].initial = purchasers or list(seen)


class VerificationResponseForm(forms.Form):
    """The customer's answer on the public page."""

    response = forms.ChoiceField(choices=[("confirmed", ""), ("changes", ""), ("rejected", "")])
    comment = forms.CharField(required=False, max_length=4000, widget=forms.Textarea)

    def clean(self):
        data = super().clean()
        if data.get("response") == "changes" and not (data.get("comment") or "").strip():
            self.add_error("comment", "Please tell us what needs to change.")
        return data

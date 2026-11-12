import re

from django import forms

from .models import Customer, CustomerContact

_TEXT = {"class": "form-control form-control-sm"}
_SELECT = {"class": "form-select form-select-sm"}

_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9/_-]*$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def _compact_upper(value: str) -> str:
    return re.sub(r"\s+", "", value or "").upper()


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["code", "name", "address", "city", "state", "pincode", "country",
                  "gstin", "pan", "phone", "website", "notes"]
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
        }

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
    class Meta:
        model = CustomerContact
        fields = ["category", "name", "designation", "email", "phone", "is_purchaser", "notes"]
        widgets = {
            "category": forms.Select(attrs=_SELECT),
            "name": forms.TextInput(attrs=_TEXT),
            "designation": forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. Purchase Manager"}),
            "email": forms.EmailInput(attrs=_TEXT),
            "phone": forms.TextInput(attrs=_TEXT),
            "is_purchaser": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "notes": forms.TextInput(attrs=_TEXT),
        }

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip().lower()

    def clean(self):
        data = super().clean()
        if not data.get("email") and not (data.get("phone") or "").strip() and "email" not in self.errors:
            raise forms.ValidationError("Enter an email address or a phone number for this contact.")
        if data.get("is_purchaser") and not data.get("email") and "email" not in self.errors:
            self.add_error("email", "The purchaser needs an email address: enquiry emails go to this person.")
        return data


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


ContactFormSet = forms.formset_factory(CustomerContactForm, extra=0, can_delete=True)

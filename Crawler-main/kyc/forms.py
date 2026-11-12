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

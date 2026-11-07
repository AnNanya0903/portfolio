from django import forms

from .models import MarketingLead
from . import lost

_SELECT = {"class": "form-select form-select-sm"}
_TEXT = {"class": "form-control form-control-sm"}


class CampaignForm(forms.Form):
    name = forms.CharField(max_length=200, label="Campaign name",
                           widget=forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. Price revision – lost on price FY 2025-26"}))
    subject = forms.CharField(max_length=300, label="Email subject", widget=forms.TextInput(attrs=_TEXT))
    body = forms.CharField(label="Email text", widget=forms.Textarea(attrs={**_TEXT, "rows": 10}))
    kind = forms.ChoiceField(required=False, label="Status",
                             choices=[("", "Rejected + No response")] + list(lost.KIND_LABELS.items()),
                             widget=forms.Select(attrs=_SELECT))
    reason = forms.ChoiceField(required=False, label="Reason lost",
                               choices=[("", "All reasons")] + list(MarketingLead.REASON_CHOICES)
                               + [("none", "Reason not recorded")],
                               widget=forms.Select(attrs=_SELECT))
    fin_year = forms.CharField(required=False, max_length=7, label="Financial year",
                               widget=forms.TextInput(attrs={**_TEXT, "placeholder": "e.g. 2025-26"}))
    q = forms.CharField(required=False, max_length=200, label="Search",
                        widget=forms.TextInput(attrs={**_TEXT, "placeholder": "Customer, enquiry or email"}))

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if len(body) < 20:
            raise forms.ValidationError("Write a few sentences for the customer.")
        return body

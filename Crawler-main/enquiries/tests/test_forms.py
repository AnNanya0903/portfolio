from typing import Dict

from django.test import SimpleTestCase

from enquiries import forms


class EnquiryFormValidationTests(SimpleTestCase):
    def _base_payload(self) -> Dict[str, str]:
        return {
            "customer_name": "Acme Corp",
            "subject": "Sample enquiry",
            "channel": "email",
            "priority": "high",
            "description": "Details about the enquiry",
            "customer_reference": "REF-1001",
            "currency": "USD",
            "conversion_rate": "1",
        }

    def test_conversion_rate_required_for_non_usd_currency(self) -> None:
        payload = self._base_payload()
        payload.update({"currency": "INR", "conversion_rate": ""})
        form = forms.EnquiryCreateForm(data=payload)

        self.assertFalse(form.is_valid())
        self.assertIn("Conversion rate is required when currency is not USD.", form.errors["conversion_rate"])

    def test_negative_numeric_values_are_rejected(self) -> None:
        payload = self._base_payload()
        payload.update({"price_per_piece": "-1"})
        form = forms.EnquiryCreateForm(data=payload)

        self.assertFalse(form.is_valid())
        self.assertIn("Value cannot be negative.", form.errors["price_per_piece"])

    def test_usd_currency_defaults_conversion_rate_to_one(self) -> None:
        payload = self._base_payload()
        payload.update({"conversion_rate": ""})
        form = forms.EnquiryCreateForm(data=payload)

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["conversion_rate"], 1)

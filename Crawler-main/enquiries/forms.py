"""Forms used by the enquiries Django integration."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Dict, Iterable
from datetime import date, datetime

from django import forms

from .constants import (
    CHANNEL_CHOICES,
    CURRENCY_CHOICES,
    CURRENCY_COST_FIELDS,
    CURRENCY_SYMBOLS,
    DETAIL_FIELD_LABELS,
    INTEGER_FIELDS,
    NUMERIC_FIELDS,
    PRIORITY_CHOICES,
    STATUS_CHOICES,
)

_DECIMAL_FIELD_KWARGS = {
    "required": False,
    "max_digits": 14,
    "decimal_places": 4,
    "min_value": 0,
}


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    widget = MultiFileInput

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("required", False)
        widget = kwargs.get("widget")
        if widget is None:
            kwargs["widget"] = MultiFileInput(attrs={"multiple": True})
        else:
            widget.attrs.setdefault("multiple", True)
        super().__init__(*args, **kwargs)

    def clean(self, data: Any, initial: Any = None) -> list[Any]:
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]
        cleaned: list[Any] = []
        errors: list[Any] = []
        for item in data:
            if not item:
                continue
            try:
                cleaned.append(super().clean(item, initial))
            except forms.ValidationError as exc:
                errors.extend(exc.error_list)
        if errors:
            raise forms.ValidationError(errors)
        return cleaned


class JSONListField(forms.JSONField):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("required", False)
        kwargs.setdefault("initial", list)
        kwargs.setdefault("widget", forms.HiddenInput())
        super().__init__(*args, **kwargs)

    def clean(self, value: Any) -> list[Dict[str, Any]]:
        cleaned = super().clean(value)
        if cleaned in (None, ""):
            return []
        if isinstance(cleaned, tuple):
            cleaned = list(cleaned)
        if not isinstance(cleaned, list):
            raise forms.ValidationError("Invalid column data format.")

        normalized: list[Dict[str, Any]] = []
        for item in cleaned:
            if isinstance(item, Mapping):
                item = dict(item)
            if not isinstance(item, dict):
                raise forms.ValidationError("Column entry must be an object.")
            code = item.get("code")
            if not code or not isinstance(code, str):
                raise forms.ValidationError("Each column requires a code identifier.")
            title = item.get("title")
            if title is not None and not isinstance(title, str):
                title = None
            values = item.get("values") or {}
            if not isinstance(values, dict):
                raise forms.ValidationError(f"Invalid values for column {code}.")
            normalized_values: Dict[str, Any] = {}
            for raw_key, raw_value in values.items():
                key = str(raw_key)
                normalized_values[key] = raw_value
            image_ids_raw = item.get("image_ids") or []
            if isinstance(image_ids_raw, tuple):
                image_ids_raw = list(image_ids_raw)
            if not isinstance(image_ids_raw, Iterable) or isinstance(image_ids_raw, (str, bytes)):
                raise forms.ValidationError(f"Invalid image identifier list for column {code}.")
            image_ids: list[str] = []
            for image_id in image_ids_raw:
                if not image_id:
                    continue
                image_ids.append(str(image_id))
            normalized.append({
                "code": code,
                "title": title,
                "values": normalized_values,
                "image_ids": image_ids,
            })
        return normalized



class EnquiryBaseForm(forms.Form):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            css_class = widget.attrs.get("class", "")
            if isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs["class"] = f"{css_class} form-select".strip()
            elif isinstance(widget, forms.FileInput):
                widget.attrs["class"] = f"{css_class} form-control".strip()
            else:
                widget.attrs["class"] = f"{css_class} form-control".strip()
        currency_field = self.fields.get("currency")
        current_currency_code = None
        if currency_field:
            if self.is_bound:
                current_currency_code = self.data.get(self.add_prefix("currency")) or None
            else:
                initial_mapping: Dict[str, Any] = {}
                if isinstance(self.initial, dict):
                    initial_mapping = self.initial
                elif self.initial:
                    try:
                        initial_mapping = dict(self.initial)
                    except Exception:
                        initial_mapping = {}
                current_currency_code = initial_mapping.get("currency") or currency_field.initial
            if not current_currency_code and currency_field.choices:
                current_currency_code = currency_field.choices[0][0]
            currency_field.widget.attrs["data-currency-selector"] = "true"
            currency_field.widget.attrs["data_currency_selector"] = "true"
            currency_field.widget.attrs["data-currency-symbols"] = ",".join(
                f"{code}:{symbol}" for code, symbol in CURRENCY_SYMBOLS.items()
            )
            currency_field.widget.attrs["data_currency_symbols"] = ",".join(
                f"{code}:{symbol}" for code, symbol in CURRENCY_SYMBOLS.items()
            )
            if current_currency_code:
                currency_field.widget.attrs["data-currency-selected"] = current_currency_code
                currency_field.widget.attrs["data_selected_currency"] = current_currency_code
        conversion_rate_field = self.fields.get("conversion_rate")
        if conversion_rate_field:
            conversion_rate_field.widget.attrs["data-conversion-rate-field"] = "true"
            conversion_rate_field.widget.attrs["data_conversion_rate_field"] = "true"
            conversion_rate_field.widget.attrs.setdefault("inputmode", "decimal")
        current_symbol = CURRENCY_SYMBOLS.get(current_currency_code or "", "$")
        for percent_name in ("additional_dev_percent", "x20_percent", "scrap_percent"):
            percent_field = self.fields.get(percent_name)
            if percent_field:
                percent_field.widget.attrs.setdefault("inputmode", "decimal")
        for name in CURRENCY_COST_FIELDS:
            field = self.fields.get(name)
            if not field:
                continue
            field.widget.attrs["data-currency-field"] = "true"
            field.widget.attrs["data_currency_field"] = "true"
            field.widget.attrs["data-currency-symbol"] = current_symbol
            field.widget.attrs["data_currency_symbol"] = current_symbol
            field.widget.attrs.setdefault("inputmode", "decimal")
        for name in NUMERIC_FIELDS:
            field = self.fields.get(name)
            if not field:
                continue
            min_value = getattr(field, "min_value", None)
            if min_value == 0:
                field.error_messages.setdefault("min_value", "Value cannot be negative.")

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        # In relaxed flows (e.g., share-link generation), skip strict validation
        if getattr(self, "skip_strict_validation", False):
            return cleaned
        currency = cleaned.get("currency")
        conversion_rate = cleaned.get("conversion_rate")

        if conversion_rate in (None, ""):
            if currency not in (None, "", "USD"):
                self.add_error("conversion_rate", "Conversion rate is required when currency is not USD.")
            else:
                cleaned["conversion_rate"] = cleaned.get("conversion_rate") or 1
        else:
            try:
                if float(conversion_rate) <= 0:
                    self.add_error("conversion_rate", "Conversion rate must be greater than zero.")
            except (TypeError, ValueError):
                self.add_error("conversion_rate", "Enter a valid conversion rate.")

        for field_name in NUMERIC_FIELDS:
            value = cleaned.get(field_name)
            if value in (None, ""):
                continue
            try:
                if float(value) < 0:
                    self.add_error(field_name, "Value cannot be negative.")
            except (TypeError, ValueError):
                self.add_error(field_name, "Enter a valid number.")

        die_columns = cleaned.get("extrusion_die_columns") or []
        mold_columns = cleaned.get("mold_columns") or []
        die_count = cleaned.get("extrusion_die_count") or 0
        mold_count = cleaned.get("mold_count") or 0
        if die_count and len(die_columns) != die_count:
            self.add_error(
                "extrusion_die_columns",
                f"Provide details for {die_count} extrusion die column{'s' if die_count != 1 else ''}.",
            )
        if mold_count and len(mold_columns) != mold_count:
            self.add_error(
                "mold_columns",
                f"Provide details for {mold_count} mold column{'s' if mold_count != 1 else ''}.",
            )

        return cleaned

    customer_name = forms.CharField(label="Customer Name", max_length=255)
    subject = forms.CharField(label="Subject", max_length=255)
    channel = forms.ChoiceField(label="Channel", choices=CHANNEL_CHOICES)
    priority = forms.ChoiceField(label="Priority", choices=PRIORITY_CHOICES)
    description = forms.CharField(label="Description", widget=forms.Textarea(attrs={"rows": 3}))
    assigned_to = forms.CharField(
        label="Assigned To",
        required=False,
        max_length=255,
        empty_value=None,
    )
    currency = forms.ChoiceField(
        label="Currency",
        choices=CURRENCY_CHOICES,
        initial="USD",
    )
    conversion_rate = forms.DecimalField(
        label="Conversion Rate",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=4,
        initial=1,
        help_text="Used to normalise pricing data when importing enquiries.",
    )
    extrusion_die_count = forms.IntegerField(
        label="# of Extrusion Dies",
        required=False,
        min_value=0,
        max_value=12,
        initial=0,
    )
    mold_count = forms.IntegerField(
        label="# of Molds",
        required=False,
        min_value=0,
        max_value=12,
        initial=0,
    )
    extrusion_die_columns = JSONListField()
    mold_columns = JSONListField()

    material_customer = forms.CharField(label="Material (Customer)", required=False, max_length=255, empty_value=None)
    feasibility = forms.CharField(label="Feasibility", required=False, max_length=255, empty_value=None)
    required_trials_meters = forms.DecimalField(label="Req Trials (01 Trial = 10 Mtrs)", **_DECIMAL_FIELD_KWARGS)
    die_development_charge = forms.DecimalField(label="Die Development Charge", **_DECIMAL_FIELD_KWARGS)
    die_development_charge_internal = forms.DecimalField(
        label="Die Development Charge (Internal)", **_DECIMAL_FIELD_KWARGS
    )
    die_tool_development_charge_usd = forms.DecimalField(
        label="Die Tool Development Charge", **_DECIMAL_FIELD_KWARGS
    )
    supplier_mold_cost = forms.DecimalField(label="Supplier Mold Cost", **_DECIMAL_FIELD_KWARGS)
    supplier_cost_per_piece = forms.DecimalField(label="Supplier Cost / Piece", **_DECIMAL_FIELD_KWARGS)
    additional_dev_percent = forms.DecimalField(
        label="Additional Development %",
        required=False,
        max_digits=6,
        decimal_places=2,
    )
    mold_development_cost = forms.DecimalField(label="Mold Development Cost", **_DECIMAL_FIELD_KWARGS)
    mold_development_charge_usd = forms.DecimalField(
        label="Mold Development Charge", **_DECIMAL_FIELD_KWARGS
    )
    mold_development_charge_internal = forms.DecimalField(
        label="Mold Development Charge (Internal)", **_DECIMAL_FIELD_KWARGS
    )
    manufacturing_process = forms.CharField(label="Manufacturing Process", required=False, max_length=255, empty_value=None)
    manufacturing_process_internal = forms.CharField(
        label="Manufacturing Process (Internal)", required=False, max_length=255, empty_value=None
    )
    surface_treatment = forms.CharField(label="Surface Treatment", required=False, max_length=255, empty_value=None)
    area_or_volume = forms.CharField(label="Area / Volume", required=False, max_length=255, empty_value=None)
    gasket_thickness_length_mm = forms.CharField(
        label="Gasket Thickness / Length (mm)", required=False, max_length=255, empty_value=None
    )
    pack_size = forms.CharField(label="Pack Size", required=False, max_length=255, empty_value=None)
    die_number = forms.CharField(label="Die Number", required=False, max_length=255, empty_value=None)
    hardness_or_material = forms.CharField(label="Hardness / Material", required=False, max_length=255, empty_value=None)
    weight_per_meter_g = forms.DecimalField(label="Weight/Meter (In gms)", **_DECIMAL_FIELD_KWARGS)
    weight_per_meter_pricing = forms.DecimalField(
        label="Wt/m Considered for Pricing", **_DECIMAL_FIELD_KWARGS
    )
    weight_per_piece_g = forms.DecimalField(label="Weight/Pcs (In gms)", **_DECIMAL_FIELD_KWARGS)
    price_per_kg = forms.DecimalField(label="Price per KG", **_DECIMAL_FIELD_KWARGS)
    minimum_order_quantity = forms.IntegerField(label="MOQ", required=False, min_value=0)
    price_per_piece = forms.DecimalField(label="Price per pc", **_DECIMAL_FIELD_KWARGS)
    price_per_meter = forms.DecimalField(label="Price per meter", **_DECIMAL_FIELD_KWARGS)
    customer_reference = forms.CharField(label="Customer Reference", required=False, max_length=255, empty_value=None)
    test_standard = forms.CharField(label="Test Standard", required=False, max_length=255, empty_value=None)
    specific_gravity = forms.DecimalField(label="Sp. Gravity", **_DECIMAL_FIELD_KWARGS)
    project_name = forms.CharField(label="Project Name", required=False, max_length=255, empty_value=None)
    digital_cutting_price = forms.DecimalField(label="Digital Cutting Price", **_DECIMAL_FIELD_KWARGS)
    width_of_tape_required = forms.DecimalField(label="DSA Tape Width", **_DECIMAL_FIELD_KWARGS)
    dsa_tape_application_charge = forms.DecimalField(
        label="DSA Tape Application Charge", **_DECIMAL_FIELD_KWARGS
    )
    secondary_operation_charge = forms.DecimalField(
        label="Secondary Operation Charge", **_DECIMAL_FIELD_KWARGS
    )
    total_price_per_piece = forms.DecimalField(label="Total Price per Pc", **_DECIMAL_FIELD_KWARGS)
    total_price_per_meter = forms.DecimalField(label="Total Price per Meter", **_DECIMAL_FIELD_KWARGS)
    x20_percent = forms.DecimalField(label="Additional %", **_DECIMAL_FIELD_KWARGS)
    scrap_percent = forms.DecimalField(label="Scrap %", **_DECIMAL_FIELD_KWARGS)
    final_price_per_piece = forms.DecimalField(label="($) Final Price per Piece", **_DECIMAL_FIELD_KWARGS)
    final_price_per_meter = forms.DecimalField(label="($) Final Price per Meter", **_DECIMAL_FIELD_KWARGS)
    remarks = forms.CharField(
        label="Remarks",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        empty_value=None,
    )

    # New primary fields for create/edit based on updated requirements
    enquiry_date = forms.DateField(
        label="Date",
        required=False,
        initial=date.today(),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    enq_number = forms.CharField(label="ENQ#", required=False, max_length=255, empty_value=None)
    customer_part_number = forms.CharField(
        label="Customer Part#", required=False, max_length=255, empty_value=None
    )
    warranty_period = forms.CharField(label="Warranty Period", required=False, max_length=255, empty_value=None)
    die_development_lead_time = forms.CharField(
        label="Die Development Lead Time", required=False, max_length=255, empty_value=None
    )
    production_lead_time = forms.CharField(
        label="Production Lead Time", required=False, max_length=255, empty_value=None
    )
    avigiri_part_number = forms.CharField(
        label="AVIGIRI Part #", required=False, max_length=255, empty_value=None
    )
    material_specification = forms.CharField(
        label="Material  Specificaiton", required=False, max_length=255, empty_value=None
    )
    supplier_metal_percent = forms.DecimalField(label="Supplier Metal %", **_DECIMAL_FIELD_KWARGS)
    supplier_cost_per_piece_percent = forms.DecimalField(
        label="Supplier Cost per Pc %", **_DECIMAL_FIELD_KWARGS
    )
    incoterms_percent = forms.DecimalField(label="(Incoterms = F7) %", **_DECIMAL_FIELD_KWARGS)
    packing_percent = forms.DecimalField(label="Packing %", **_DECIMAL_FIELD_KWARGS)
    final_price_per_pack_meters = forms.DecimalField(label="($) Final Price per Pack (m)", **_DECIMAL_FIELD_KWARGS)
    final_price_per_pack_pcs = forms.DecimalField(label="($) Final Price per Pack (pcs)", **_DECIMAL_FIELD_KWARGS)
    total_die_development_charge = forms.DecimalField(
        label="($) Total Die Development Charge", **_DECIMAL_FIELD_KWARGS
    )
    total_mold_development_charge = forms.DecimalField(
        label="($) Total Mold Development Charge", **_DECIMAL_FIELD_KWARGS
    )
    supplier_mold_development_percent = forms.DecimalField(
        label="Supplier Mold Development %", **_DECIMAL_FIELD_KWARGS
    )
    nrc_payment_terms = forms.CharField(
        label="NRC Payment Terms", required=False, max_length=255, empty_value=None
    )
    rc_payment_terms = forms.CharField(
        label="RC Payment Terms", required=False, max_length=255, empty_value=None
    )

    profile = forms.FileField(
        label="Profile",
        required=False,
        widget=forms.ClearableFileInput(),
    )
    attachments = MultiFileField(label="Attachments")

    def build_payload(self, include_nulls: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for field_name, value in self.cleaned_data.items():
            if field_name in {"attachments", "profile"}:
                continue
            if value in (None, ""):
                if include_nulls:
                    payload[field_name] = None
                continue
            if field_name in INTEGER_FIELDS and value is not None:
                payload[field_name] = int(value)
                continue
            if field_name in NUMERIC_FIELDS and value is not None:
                payload[field_name] = float(value)
                continue
            # Normalise date/datetime objects to ISO-8601 strings for JSON payloads
            if isinstance(value, (date, datetime)):
                try:
                    payload[field_name] = value.isoformat()
                except Exception:
                    payload[field_name] = str(value)
                continue
            payload[field_name] = value
        return payload


class EnquiryCreateForm(EnquiryBaseForm):
    # Customer Reference is not mandatory for create in the new flow
    customer_reference = forms.CharField(label="Customer Reference", required=False, max_length=255, empty_value=None)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Make previously-required, now-removed fields optional for create
        for name in ("subject", "channel", "priority", "assigned_to", "material_customer", "description"):
            if name in self.fields:
                self.fields[name].required = False
        # Update conversion rate help text to align with spec note
        if "conversion_rate" in self.fields:
            self.fields["conversion_rate"].help_text = "Used to normalise pricing data (Currency = D7)."

    def build_payload(self, include_nulls: bool = False) -> Dict[str, Any]:
        # Start with the base payload (numeric/date normalization etc.)
        payload = super().build_payload(include_nulls=include_nulls)
        # Ensure required fields for the enquiry service are present even if not shown in UI
        customer_name = self.cleaned_data.get("customer_name") or payload.get("customer_name")
        project_name = self.cleaned_data.get("project_name") or payload.get("project_name")
        enq_no = self.cleaned_data.get("enq_number") or payload.get("enq_number")

        # Subject: derive from project or customer
        if not payload.get("subject"):
            base = project_name or customer_name or "Enquiry"
            payload["subject"] = str(base)[:255]

        # Channel: default to email
        payload.setdefault("channel", "email")

        # Description: ensure non-empty string
        if not payload.get("description"):
            payload["description"] = "—"

        # Customer Reference: use provided, else ENQ# value, else date-based ref
        if not payload.get("customer_reference"):
            ref = enq_no or (project_name or customer_name)
            if not ref:
                try:
                    from datetime import datetime
                    ref = f"AUTO-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                except Exception:
                    ref = "AUTO"
            payload["customer_reference"] = str(ref)[:255]

        # Priority: default to medium if absent
        payload.setdefault("priority", "medium")
        return payload


class EnquiryUpdateForm(EnquiryBaseForm):
    status = forms.ChoiceField(label="Status", choices=STATUS_CHOICES)
    customer_reference = forms.CharField(label="Customer Reference", required=False, max_length=255, empty_value=None)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not hasattr(self.fields, "move_to_end"):
            self.fields = OrderedDict(self.fields)
        self.fields.move_to_end("status", last=False)
        # Make previously-required, now-removed fields optional for update
        for name in ("subject", "channel", "priority", "assigned_to", "material_customer", "description"):
            if name in self.fields:
                self.fields[name].required = False

    def cleaned_payload(self, include_nulls: bool = True) -> Dict[str, Any]:
        payload = self.build_payload(include_nulls=include_nulls)
        payload["status"] = self.cleaned_data.get("status")
        if payload.get("assigned_to") is None:
            payload["assigned_to"] = None
        return payload


class SubdivisionForm(EnquiryBaseForm):
    parent_enquiry_id = forms.CharField(widget=forms.HiddenInput())
    subdivision_id = forms.CharField(widget=forms.HiddenInput(), required=False)
    code = forms.CharField(label="SubEnquiry Code", required=False, widget=forms.TextInput(attrs={"readonly": "readonly"}))
    status = forms.ChoiceField(label="Status", choices=STATUS_CHOICES, required=False)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for extra in ("extrusion_die_count", "mold_count", "extrusion_die_columns", "mold_columns"):
            self.fields.pop(extra, None)
        optional_fields = {
            "customer_name",
            "subject",
            "channel",
            "priority",
            "description",
            "material_customer",
            "feasibility",
            "required_trials_meters",
            "die_development_charge",
            "die_tool_development_charge_usd",
            "supplier_mold_cost",
            "supplier_cost_per_piece",
            "mold_development_cost",
            "mold_development_charge_usd",
            "manufacturing_process",
            "manufacturing_process_internal",
            "surface_treatment",
            "area_or_volume",
            "gasket_thickness_length_mm",
            "die_number",
            "hardness_or_material",
            "weight_per_meter_g",
            "weight_per_meter_pricing",
            "weight_per_piece_g",
            "price_per_kg",
            "minimum_order_quantity",
            "price_per_piece",
            "customer_reference",
            "test_standard",
            "specific_gravity",
            "project_name",
            "digital_cutting_price",
            "width_of_tape_required",
            "dsa_tape_application_charge",
            "secondary_operation_charge",
            "total_price_per_piece",
            "x20_percent",
            "scrap_percent",
            "final_price_per_piece",
            "remarks",
            "currency",
            "conversion_rate",
            "assigned_to",
        }
        for name, field in self.fields.items():
            if name in optional_fields:
                field.required = False
        self.fields["parent_enquiry_id"].widget.attrs.setdefault("readonly", "readonly")
        self.fields["parent_enquiry_id"].widget.attrs.setdefault("class", "form-control-plaintext")
        self.fields["parent_enquiry_id"].widget.attrs.setdefault("tabindex", "-1")
        self.fields["parent_enquiry_id"].widget.attrs.setdefault("style", "pointer-events: none;")
        self.fields["subdivision_id"].widget.attrs.setdefault("readonly", "readonly")
        self.fields["subdivision_id"].widget.attrs.setdefault("class", "form-control-plaintext")
        self.fields["subdivision_id"].widget.attrs.setdefault("tabindex", "-1")
        self.fields["subdivision_id"].widget.attrs.setdefault("style", "pointer-events: none;")
        self.fields["code"].required = False
        self.fields["code"].widget.attrs.setdefault("readonly", "readonly")
        self.fields["code"].widget.attrs.setdefault("class", "form-control-plaintext")
        self.fields["code"].widget.attrs.setdefault("tabindex", "-1")
        self.fields["code"].widget.attrs.setdefault("style", "pointer-events: none;")
        self.fields["status"].required = False
        self.fields["status"].widget.attrs.setdefault("class", "form-select")
        self.fields["attachments"].label = "Attachments"
        self.fields["profile"].label = "Profile"

    def cleaned_payload(self, include_nulls: bool = True) -> Dict[str, Any]:
        payload = self.build_payload(include_nulls=include_nulls)
        if "status" in self.cleaned_data and self.cleaned_data["status"]:
            payload["status"] = self.cleaned_data["status"]
        payload.pop("parent_enquiry_id", None)
        payload.pop("subdivision_id", None)
        payload.pop("code", None)
        return payload


class GoogleSheetImportForm(forms.Form):
    sheet_url = forms.URLField(
        label="Google Sheet link",
        widget=forms.URLInput(
            attrs={
                "class": "form-control",
                "placeholder": "https://docs.google.com/spreadsheets/d/…",
                "required": "required",
            }
        ),
    )
    status = forms.ChoiceField(
        label="Initial status",
        choices=STATUS_CHOICES,
        initial="open",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class PDFUploadForm(forms.Form):
    title = forms.CharField(
        label="Enquiry title",
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional title"}),
    )
    pdf = forms.FileField(
        label="Upload PDF",
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf"}),
    )
    customer_name = forms.CharField(label="Customer name", max_length=255, required=False)
    customer_email = forms.EmailField(label="Customer email", required=False)

    def clean_pdf(self):
        pdf_file = self.cleaned_data["pdf"]
        content_type = getattr(pdf_file, "content_type", "") or ""
        if "pdf" not in content_type.lower():
            raise forms.ValidationError("Please upload a PDF file.")
        return pdf_file

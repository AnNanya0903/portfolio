"""Utilities for generating preview PDFs using the enquiry microservice renderer."""

from __future__ import annotations

import numbers
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, Iterable, List, Optional
from types import SimpleNamespace

from .constants import DETAIL_FIELD_LABELS, INTEGER_FIELDS, NUMERIC_FIELDS


_ROOT_DIR = Path(__file__).resolve().parents[1]
_PDF_EXPORT_PATH = _ROOT_DIR / "microservices" / "enquiry-service" / "pdf_export.py"
_MODELS_PATH = _ROOT_DIR / "microservices" / "enquiry-service" / "models.py"


def _load_models_module():
    spec = spec_from_file_location("enquiry_pdf_models", _MODELS_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


models_module = _load_models_module()
models_module.__name__ = "models"
sys.modules.setdefault("models", models_module)


def _load_pdf_module():
    spec = spec_from_file_location("enquiry_pdf_export", _PDF_EXPORT_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


pdf_export = _load_pdf_module()
build_pdf = pdf_export.build_pdf


@dataclass
class AttachmentData:
    attachment_id: str
    file_name: str
    content_type: Optional[str]
    size_bytes: int
    uploaded_at: datetime
    download_url: str
    storage_path: Optional[str] = None
    category: str = "general"


def _ensure_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    if isinstance(value, str):
        cleaned = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:
            pass
    return datetime.utcnow()


def _stringify_value(value: Any) -> str:
    if value in (None, "", [], {}, ()):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        decimal_value = Decimal(str(value))
        normalized = decimal_value.normalize()
        text = format(normalized, "f").rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (list, tuple, set)):
        parts = [item for item in (str(x).strip() for x in value) if item]
        return ", ".join(parts)
    return str(value)


def generate_preview_pdf(
    payload: Dict[str, Any],
    *,
    profile_file: Optional[Any],
    general_files: Iterable[Any],
    existing: Optional[Dict[str, Any]],
    download_attachment: Callable[[str], tuple[bytes, str, str]],
    included_fields: Optional[Iterable[str]] = None,
    displayed_fields: Optional[Iterable[str]] = None,
) -> bytes:
    """
    Build a PDF preview using the enquiry PDF renderer.

    Args:
        payload: Cleaned form payload containing updated field values.
        profile_file: Optional uploaded profile file to include in the preview.
        general_files: Iterable of uploaded general attachments for the preview.
        existing: Existing enquiry payload from the API (if editing).
        download_attachment: Callback used to download stored attachments for preview.
        included_fields: Optional iterable of field names that should appear in the PDF.
        displayed_fields: Optional iterable of all field names rendered in the preview form.
    """

    temp_paths: List[str] = []
    payload_data: Dict[str, Any] = dict(payload)
    included_set = set(included_fields) if included_fields is not None else None
    displayed_order: Optional[List[str]] = list(displayed_fields) if displayed_fields is not None else None
    if displayed_order is not None:
        displayed_set = set(displayed_order)
    else:
        displayed_set = set(payload_data.keys())
        displayed_order = list(displayed_set)
    existing_data = existing or {}
    numeric_field_names = set(NUMERIC_FIELDS) | set(INTEGER_FIELDS)

    def _cleanup() -> None:
        for path in temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        temp_paths.clear()

    try:
        attachments: List[AttachmentData] = []

        if existing_data:
            for attachment in existing_data.get("attachments", []):
                category = attachment.get("category", "general")
                storage_path: Optional[str] = None
                size_bytes = attachment.get("size_bytes") or 0
                file_name = attachment.get("file_name") or "attachment"
                download_url = attachment.get("download_url") or ""
                content_type = attachment.get("content_type")

                if category == "profile" and download_url:
                    content, filename, content_type_dl = download_attachment(download_url)
                    suffix = Path(filename or file_name).suffix or ".bin"
                    temp_file = NamedTemporaryFile(delete=False, suffix=suffix)
                    temp_file.write(content)
                    temp_file.flush()
                    temp_file.close()
                    storage_path = temp_file.name
                    size_bytes = len(content)
                    temp_paths.append(storage_path)
                    if content_type_dl:
                        content_type = content_type_dl

                attachments.append(
                    AttachmentData(
                        attachment_id=attachment.get("attachment_id") or f"existing-{len(attachments)}",
                        file_name=file_name,
                        content_type=content_type,
                        size_bytes=size_bytes,
                        uploaded_at=_ensure_datetime(attachment.get("uploaded_at")),
                        download_url=download_url,
                        storage_path=storage_path,
                        category=category,
                    )
                )

        if profile_file:
            profile_file.seek(0)
            suffix = Path(profile_file.name).suffix or ".bin"
            temp = NamedTemporaryFile(delete=False, suffix=suffix)
            temp.write(profile_file.read())
            temp.flush()
            temp.close()
            temp_paths.append(temp.name)
            attachments = [att for att in attachments if att.category != "profile"]
            attachments.insert(
                0,
                AttachmentData(
                    attachment_id="preview-profile",
                    file_name=profile_file.name,
                    content_type=getattr(profile_file, "content_type", None),
                    size_bytes=getattr(profile_file, "size", 0),
                    uploaded_at=datetime.utcnow(),
                    download_url="/preview/profile",
                    storage_path=temp.name,
                    category="profile",
                ),
            )
            profile_file.seek(0)

        for idx, upload in enumerate(general_files):
            if not upload:
                continue
            attachments.append(
                AttachmentData(
                    attachment_id=f"preview-attachment-{idx}",
                    file_name=upload.name,
                    content_type=getattr(upload, "content_type", None),
                    size_bytes=getattr(upload, "size", 0),
                    uploaded_at=datetime.utcnow(),
                    download_url="/preview/attachment",
                    storage_path=None,
                    category="general",
                )
            )

        base: Dict[str, Any] = {}
        if existing_data:
            base = {k: v for k, v in existing_data.items() if k not in {"attachments", "profile_attachments", "general_attachments", "detail_items", "received_at_dt"}}

        merged: Dict[str, Any] = {}
        merged.update(base)
        for key, value in payload_data.items():
            merged[key] = value

        defaults: Dict[str, Any] = {
            "enquiry_id": existing_data.get("enquiry_id") if existing_data else "PREVIEW",
            "customer_name": merged.get("customer_name") or "",
            "subject": merged.get("subject") or "",
            "channel": merged.get("channel") or existing_data.get("channel") or "email",
            "status": (merged.get("status") or existing_data.get("status") or "open").lower(),
            "priority": (merged.get("priority") or existing_data.get("priority") or "medium").lower(),
            "description": merged.get("description") or existing_data.get("description") or "",
            "project_name": merged.get("project_name") or existing_data.get("project_name"),
            "customer_reference": merged.get("customer_reference") or existing_data.get("customer_reference") or "",
            "assigned_to": merged.get("assigned_to") or existing_data.get("assigned_to"),
            "received_at": _ensure_datetime(existing_data.get("received_at_dt") or existing_data.get("received_at")) if existing_data else datetime.utcnow(),
        }
        merged.update(defaults)

        for key in (
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
            "specific_gravity",
            "weight_per_meter_pricing",
            "minimum_order_quantity",
            "price_per_piece",
            "total_price_per_piece",
            "scrap_percent",
            "final_price_per_piece",
            "remarks",
        ):
            merged.setdefault(key, None)

        selected_entries: List[Dict[str, str]] = []
        if included_set:
            seen: set[str] = set()
            ordered_fields: List[str] = []
            for name in displayed_order:
                if name in seen:
                    continue
                seen.add(name)
                ordered_fields.append(name)
            for name in included_set:
                if name not in seen:
                    ordered_fields.append(name)
            for field_name in ordered_fields:
                if field_name not in included_set:
                    continue
                if field_name not in merged:
                    continue
                value_text = _stringify_value(merged[field_name])
                if value_text == "":
                    continue
                label = DETAIL_FIELD_LABELS.get(field_name) or field_name.replace("_", " ").title()
                selected_entries.append(
                    {
                        "name": field_name,
                        "label": label,
                        "value": value_text,
                    }
                )
            if selected_entries:
                merged["_preview_selected_entries"] = selected_entries

        if displayed_set:
            allowed_fields = included_set if included_set is not None else displayed_set
            excluded_fields = {name for name in displayed_set if name not in allowed_fields}

            def _blank(field_name: str, value: Any) -> Any:
                if field_name in numeric_field_names:
                    return None
                if isinstance(value, bool):
                    return False
                if isinstance(value, numbers.Number):
                    return None
                if isinstance(value, (list, tuple, set)):
                    return []
                return ""

            for field_name in excluded_fields:
                if field_name not in merged:
                    continue
                merged[field_name] = _blank(field_name, merged[field_name])

        if displayed_set:
            merged["_preview_displayed_fields"] = frozenset(displayed_set)
        if included_set is not None:
            merged["_preview_included_fields"] = frozenset(included_set)

        merged["attachments"] = attachments

        enquiry_obj = SimpleNamespace(**merged)

        return build_pdf(enquiry_obj)
    finally:
        _cleanup()

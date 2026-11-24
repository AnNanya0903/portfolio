"""Generate PDF exports for enquiry records.

The module renders a rich, brand-aligned PDF when ``fpdf2`` is available. In
sandboxed environments where the dependency cannot be installed, it falls back
to a lightweight text-based PDF that still captures the key enquiry details.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

try:  # pragma: no cover - exercised indirectly during runtime
    from fpdf import FPDF  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - pure-Python fallback below
    FPDF = None  # type: ignore[assignment]

from models import AttachmentMeta, Enquiry


BRANDING_DIR = Path(__file__).resolve().parents[2] / "static" / "branding"
LOGO_PATH = BRANDING_DIR / "avigiri-logo.jpg"
IATF_BADGE_PATH = BRANDING_DIR / "iatf-badge.png"
ISO_BADGE_PATH = BRANDING_DIR / "iso-badge.png"
CERTIFICATION_STRIP_PATH = BRANDING_DIR / "certification-strip.png"
TABLE_OVERLAY_PATH = BRANDING_DIR / "tables.png"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "sample_enquiry_template.pdf"


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: float
    header_fill: tuple[int, int, int]
    body_fill: tuple[int, int, int]
    align: str = "C"


TABLE_COLUMNS: Iterable[Column] = (
    Column("sl_no", "Enquiry No.", 14, (216, 226, 248), (255, 255, 255)),
    Column("profile", "Profile", 28, (216, 226, 248), (255, 255, 255)),
    Column("customer_reference", "Customer Ref.", 36, (242, 208, 138), (255, 255, 255), align="L"),
    Column("description", "Description", 30, (219, 229, 241), (255, 255, 255), align="L"),
    Column("tool_charge", "Tool Development Charge", 40, (255, 213, 128), (255, 247, 232), align="L"),
    Column("manufacturing_process", "Manufacturing Process", 34, (210, 238, 242), (242, 252, 255), align="L"),
    Column("surface_treatment", "Surface Treatment", 26, (229, 239, 220), (255, 255, 255), align="C"),
    Column("die_number", "Die No.", 20, (255, 241, 209), (255, 255, 255)),
    Column("hardness_material", "Hardness / Material", 44, (234, 210, 242), (255, 255, 255), align="L"),
    Column("moq", "MOQ", 20, (255, 234, 167), (248, 238, 199)),
    Column("price", "Final Price/Pcs", 26, (211, 231, 193), (226, 240, 217)),
    Column("remarks", "Remarks", 28, (248, 205, 204), (255, 231, 230), align="L"),
)


def _format_currency(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"Rs. {value:,.2f}"


def _format_number(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return f"{int(value)}"
    return f"{value:,.2f}"


def _clean(text: Optional[str]) -> str:
    return (text or "-").strip() or "-"


def _get_preview_visibility(enquiry: Enquiry) -> tuple[Optional[set[str]], Optional[set[str]]]:
    displayed = getattr(enquiry, "_preview_displayed_fields", None)
    included = getattr(enquiry, "_preview_included_fields", None)
    if displayed is not None and not isinstance(displayed, set):
        displayed = set(displayed)
        setattr(enquiry, "_preview_displayed_fields", displayed)
    if included is not None and not isinstance(included, set):
        included = set(included)
        setattr(enquiry, "_preview_included_fields", included)
    return displayed, included


def _field_visible(enquiry: Enquiry, field_name: str) -> bool:
    displayed, included = _get_preview_visibility(enquiry)
    if not displayed or field_name not in displayed:
        return True
    if included is None:
        return True
    return field_name in included


def _any_field_visible(enquiry: Enquiry, field_names: Iterable[str]) -> bool:
    for name in field_names:
        if _field_visible(enquiry, name):
            return True
    return False


def _get_selected_entries(enquiry: Enquiry) -> Optional[List[Dict[str, str]]]:
    entries = getattr(enquiry, "_preview_selected_entries", None)
    if not entries:
        return None
    return list(entries)


def _find_profile_image(attachments: Iterable[AttachmentMeta]) -> Optional[Path]:
    preferred: list[AttachmentMeta] = []
    others: list[AttachmentMeta] = []
    for attachment in attachments:
        if getattr(attachment, "category", None) == "profile":
            preferred.append(attachment)
        else:
            others.append(attachment)
    for attachment in preferred + others:
        storage_path = getattr(attachment, "storage_path", None)
        if storage_path and Path(storage_path).exists():
            return Path(storage_path)
    return None


def _build_tooling_text(enquiry: Enquiry) -> str:
    parts: list[str] = []
    if enquiry.die_development_charge is not None and _field_visible(enquiry, "die_development_charge"):
        parts.append(f"Die Development Charge: Rs. {enquiry.die_development_charge:,.2f}")
    if enquiry.die_tool_development_charge_usd is not None and _field_visible(enquiry, "die_tool_development_charge_usd"):
        parts.append(f"Tool Charge (USD): $ {enquiry.die_tool_development_charge_usd:,.2f}")
    if enquiry.mold_development_cost is not None and _field_visible(enquiry, "mold_development_cost"):
        parts.append(f"Mold Development Cost: Rs. {enquiry.mold_development_cost:,.2f}")
    if parts:
        return "\n".join(parts)
    if _any_field_visible(
        enquiry,
        ("die_development_charge", "die_tool_development_charge_usd", "mold_development_cost"),
    ):
        return "NA"
    return ""


def _build_process_text(enquiry: Enquiry) -> str:
    items = [
        _clean(enquiry.manufacturing_process) if _field_visible(enquiry, "manufacturing_process") else "",
        _clean(enquiry.manufacturing_process_internal) if _field_visible(enquiry, "manufacturing_process_internal") else "",
        _clean(enquiry.surface_treatment) if _field_visible(enquiry, "surface_treatment") else "",
    ]
    deduped = [item for item in items if item != "-"]
    if deduped:
        return "\n".join(deduped)
    if _any_field_visible(
        enquiry,
        ("manufacturing_process", "manufacturing_process_internal", "surface_treatment"),
    ):
        return "-"
    return ""


def _build_material_text(enquiry: Enquiry) -> str:
    lines: list[str] = []
    if enquiry.material_customer and _field_visible(enquiry, "material_customer"):
        lines.append(enquiry.material_customer)
    if enquiry.hardness_or_material and _field_visible(enquiry, "hardness_or_material"):
        lines.append(enquiry.hardness_or_material)
    if enquiry.specific_gravity is not None and _field_visible(enquiry, "specific_gravity"):
        lines.append(f"Specific Gravity: {enquiry.specific_gravity:,.2f}")
    if lines:
        return "\n".join(lines)
    if _any_field_visible(enquiry, ("material_customer", "hardness_or_material", "specific_gravity")):
        return "-"
    return ""


def _augment_remarks(enquiry: Enquiry) -> str:
    lines: list[str] = []
    if _field_visible(enquiry, "remarks"):
        remarks = _clean(enquiry.remarks)
        if remarks != "-":
            lines.append(remarks)
    price_line: Optional[str] = None
    price_fields = (
        ("final_price_per_piece", enquiry.final_price_per_piece),
        ("total_price_per_piece", enquiry.total_price_per_piece),
        ("price_per_piece", enquiry.price_per_piece),
    )
    for field_name, value in price_fields:
        if not _field_visible(enquiry, field_name):
            continue
        if value is None:
            continue
        price_line = f"Price / Pcs: {value:,.2f}"
        break
    if price_line:
        lines.append(price_line)
    elif _any_field_visible(enquiry, (name for name, _ in price_fields)):
        lines.append("Price / Pcs: -")
    if not lines:
        return ""
    return "\n".join(lines)


def _format_datetime(value: datetime) -> str:
    return value.strftime("%d %b %Y %H:%M")


# The PDF fonts used here only know Latin-1. A single character outside it (an arrow or a bullet typed in a
# subject, a curly quote pasted from Word or Gmail) made the whole PDF fail with a server error.
_LATIN1_SUBSTITUTES = {
    "→": "->", "←": "<-", "⇒": "=>", "•": "-", "‣": "-", "●": "-", "▪": "-",
    "–": "-", "—": "-", "−": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", "₹": "Rs.", "€": "EUR", " ": " ", "​": "", "✓": "v", "✔": "v",
}


def _to_latin1(text: str) -> str:
    """The text with look-alike replacements for common symbols, and "?" for anything else Latin-1 cannot hold."""
    text = str(text)
    for char, replacement in _LATIN1_SUBSTITUTES.items():
        if char in text:
            text = text.replace(char, replacement)
    return text.encode("latin-1", "replace").decode("latin-1")


if FPDF is not None:  # Rich PDF rendering ------------------------------

    class EnquiryPDF(FPDF):
        def __init__(self) -> None:
            super().__init__(orientation="L", unit="mm", format="A4")
            self.set_auto_page_break(False)
            self.set_margins(12, 12, 12)

        def normalize_text(self, text):  # every cell and multi-cell passes its text through here
            return super().normalize_text(_to_latin1(text))


    def _draw_header(pdf: EnquiryPDF, enquiry: Enquiry, *, branding: bool = True) -> None:
        if branding and LOGO_PATH.exists():
            pdf.image(str(LOGO_PATH), x=12, y=9, h=22, link="https://www.avigiri.com")

        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(10, 70, 135)
        title_area_width = pdf.w - pdf.l_margin - pdf.r_margin - 120
        pdf.set_xy(pdf.l_margin + 48, 12)
        if branding:
            pdf.cell(title_area_width, 11, "AVIGIRI Urethane & Rubber Industries Pvt.Ltd.", align="L", ln=1)
        else:
            pdf.ln(11)

        pdf.set_font("Helvetica", "", 12)
        pdf.set_xy(pdf.l_margin + 48, pdf.get_y())
        if branding:
            pdf.cell(title_area_width, 7, "( Feasibility & Quotation Report )", align="L", ln=1)
        else:
            pdf.ln(7)

        strip_rendered = _draw_certification_strip(pdf, title_area_width) if branding else False

        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

        if branding and not strip_rendered:
            current_y = pdf.get_y()
            _draw_certification_badges(pdf)
            pdf.set_y(current_y)

        info_rows = []

        customer_text = f"Customer Name: {_clean(enquiry.customer_name)}" if _field_visible(enquiry, "customer_name") else ""
        info_rows.append(
            (
                customer_text,
                f"Date: {(enquiry.received_at or datetime.utcnow()):%d.%m.%Y}",
            )
        )

        project_text = f"Project: {_clean(enquiry.project_name)}" if _field_visible(enquiry, "project_name") else ""
        info_rows.append((project_text, f"Enquiry ID: {enquiry.enquiry_id}"))

        status_bits: list[str] = []
        if _field_visible(enquiry, "status"):
            status_bits.append(f"Status: {enquiry.status.upper()}")
        if _field_visible(enquiry, "channel"):
            status_bits.append(f"Channel: {enquiry.channel.title()}")
        status_line = " | ".join(status_bits)
        if status_line:
            info_rows.append(("", status_line))

        pdf.set_draw_color(90, 90, 90)
        pdf.set_line_width(0.2)

        for left, right in info_rows:
            if not left and not right:
                continue
            pdf.set_x(12)
            pdf.set_fill_color(240, 240, 240)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(190, 8, left, border=1, fill=True)
            pdf.set_fill_color(255, 255, 255)
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(0, 8, right, border=1, align="R", fill=True)
            pdf.ln(8)

        pdf.ln(4)


    def _draw_table(pdf: EnquiryPDF, enquiry: Enquiry, *, branding_enabled: bool = True) -> None:
        selected_entries = _get_selected_entries(enquiry)
        if selected_entries:
            _draw_selected_fields_table(pdf, selected_entries)
            return

        profile_image = _find_profile_image(enquiry.attachments)

        table_left = pdf.l_margin
        table_top = pdf.get_y()
        header_height = 12
        base_row_height = 34
        line_height = 6

        pdf.set_font("Helvetica", "B", 9)
        scale = (pdf.w - pdf.l_margin - pdf.r_margin) / sum(column.width for column in TABLE_COLUMNS)
        scaled_columns = [replace(column, width=round(column.width * scale, 2)) for column in TABLE_COLUMNS]
        remaining = (pdf.w - pdf.l_margin - pdf.r_margin) - sum(column.width for column in scaled_columns)
        if scaled_columns:
            scaled_columns[-1] = replace(scaled_columns[-1], width=scaled_columns[-1].width + remaining)

        if branding_enabled:
            _draw_table_branding(pdf, table_left, table_top, sum(column.width for column in scaled_columns))

        for column in scaled_columns:
            pdf.set_fill_color(*column.header_fill)
            pdf.set_text_color(35, 55, 92)
            pdf.multi_cell(
                column.width,
                header_height,
                column.label,
                border=1,
                align="C",
                fill=True,
                new_x="RIGHT",
                new_y="TOP",
            )
        pdf.set_xy(table_left, table_top + header_height)

        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)

        price_value: Optional[float] = None
        if _field_visible(enquiry, "final_price_per_piece") and enquiry.final_price_per_piece is not None:
            price_value = enquiry.final_price_per_piece
        elif _field_visible(enquiry, "price_per_piece") and enquiry.price_per_piece is not None:
            price_value = enquiry.price_per_piece

        price_text = ""
        if _any_field_visible(enquiry, ("final_price_per_piece", "price_per_piece")):
            price_text = _format_currency(price_value)

        remarks_text = _augment_remarks(enquiry)
        if not remarks_text and _field_visible(enquiry, "remarks"):
            remarks_text = "-"

        data_map = {
            "sl_no": enquiry.enquiry_id,
            "customer_reference": _clean(enquiry.customer_reference) if _field_visible(enquiry, "customer_reference") else "",
            "description": _clean(enquiry.description) if _field_visible(enquiry, "description") else "",
            "tool_charge": _build_tooling_text(enquiry),
            "manufacturing_process": _build_process_text(enquiry),
            "surface_treatment": _clean(enquiry.surface_treatment) if _field_visible(enquiry, "surface_treatment") else "",
            "die_number": _clean(enquiry.die_number) if _field_visible(enquiry, "die_number") else "",
            "hardness_material": _build_material_text(enquiry),
            "moq": _format_number(enquiry.minimum_order_quantity) if _field_visible(enquiry, "minimum_order_quantity") else "",
            "price": price_text,
            "remarks": remarks_text,
        }

        cell_layout: list[tuple[Column, str, float]] = []
        for column in scaled_columns:
            if column.key == "profile":
                cell_layout.append((column, "", base_row_height))
                continue
            text = data_map.get(column.key, "-")
            content_width = max(column.width - 2.0, column.width)
            lines = pdf.multi_cell(content_width, line_height, text, split_only=True)
            line_count = max(1, len(lines))
            needed_height = (line_count * line_height) + 4.0
            cell_layout.append((column, text, needed_height))

        row_height = max(base_row_height, max(height for _, _, height in cell_layout))

        start_y = pdf.get_y()
        current_x = table_left
        for column, text, _height in cell_layout:
            pdf.set_fill_color(*column.body_fill)
            pdf.rect(current_x, start_y, column.width, row_height, style="F")
            pdf.rect(current_x, start_y, column.width, row_height)

            if column.key == "profile":
                if profile_image:
                    margin = 2.0
                    try:
                        pdf.image(
                            str(profile_image),
                            x=current_x + margin,
                            y=start_y + margin,
                            w=column.width - (margin * 2),
                            h=row_height - (margin * 2),
                            keep_aspect_ratio=True,
                        )
                    except Exception:  # pragma: no cover - best effort rendering
                        pass
            else:
                pdf.set_xy(current_x + 1.0, start_y + 1.5)
                pdf.multi_cell(
                    column.width - 2.0,
                    line_height,
                    text,
                    border=0,
                    align=column.align,
                    new_x="LEFT",
                    new_y="TOP",
                    max_line_height=row_height - 3.0,
                )
            current_x += column.width
            pdf.set_xy(current_x, start_y)

        pdf.set_xy(table_left, start_y + row_height + 10)


    def _draw_selected_fields_table(pdf: EnquiryPDF, entries: List[Dict[str, str]]) -> None:
        table_left = pdf.l_margin
        table_width = pdf.w - pdf.l_margin - pdf.r_margin
        pair_width = table_width / 2
        label_width = pair_width * 0.38
        value_width = pair_width - label_width
        line_height = 6

        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(35, 55, 92)
        pdf.cell(table_width, 8, "Selected Preview Fields", ln=1)
        pdf.ln(2)
        pdf.set_text_color(0, 0, 0)

        chunk_size = 2
        for idx in range(0, len(entries), chunk_size):
            chunk = entries[idx : idx + chunk_size]
            layout_info: List[tuple[Dict[str, str], float]] = []
            for entry in chunk:
                label_lines = pdf.multi_cell(label_width - 3, line_height, entry["label"], split_only=True)
                value_lines = pdf.multi_cell(value_width - 3, line_height, entry["value"], split_only=True)
                needed_height = max(len(label_lines), len(value_lines)) * line_height + 4
                layout_info.append((entry, max(needed_height, 12)))
            if not layout_info:
                continue
            row_height = max(item[1] for item in layout_info)
            start_y = pdf.get_y()

            for col in range(chunk_size):
                x_base = table_left + col * pair_width
                pdf.rect(x_base, start_y, pair_width, row_height)

            for col, (entry, _height) in enumerate(layout_info):
                x_base = table_left + col * pair_width
                pdf.set_fill_color(240, 240, 240)
                pdf.rect(x_base, start_y, label_width, row_height, style="F")
                pdf.rect(x_base, start_y, label_width, row_height)
                pdf.rect(x_base + label_width, start_y, value_width, row_height)

                pdf.set_xy(x_base + 1.5, start_y + 1.5)
                pdf.set_font("Helvetica", "B", 9)
                pdf.multi_cell(label_width - 3, line_height, entry["label"], border=0)

                pdf.set_xy(x_base + label_width + 1.5, start_y + 1.5)
                pdf.set_font("Helvetica", "", 9)
                pdf.multi_cell(value_width - 3, line_height, entry["value"], border=0)

            pdf.set_y(start_y + row_height + 3)


    def _draw_footer(pdf: EnquiryPDF) -> None:
        footer_y = pdf.get_y()
        if footer_y < 155:
            footer_y = 155

        col_width = 85
        sections = [
            [
                ("Material Specifications", "As per EN54554-H13 Compliant", (0, 0, 0)),
                ("Dimensional Tolerances", "As per ISO 3302-1 E2 / E1 Standards", (0, 0, 0)),
                ("Warranty Period", "10-Years against Invoice", (0, 0, 0)),
            ],
            [
                ("Die Development Charges (If Required)", "As per Chart", (0, 0, 0)),
                (
                    "Production Lead Time",
                    "Die Development Lead time - 20 - 25 days after confirmation\nProduction Lead time - 15 - 25 days for MOQ",
                    (0, 0, 0),
                ),
            ],
            [
                ("Incoterms", "Ex-Factory", (0, 0, 0)),
                ("Payment Terms", "100% Advance for die development\n100% Advance for Production Material", (180, 0, 0)),
            ],
        ]

        for idx, section in enumerate(sections):
            x = 12 + (col_width + 6) * idx
            current_y = footer_y
            for title, body, color in section:
                pdf.set_xy(x, current_y)
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(0, 0, 0)
                pdf.multi_cell(col_width, 5, title, border=0)
                current_y = pdf.get_y()

                pdf.set_xy(x, current_y)
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(*color)
                pdf.multi_cell(col_width, 5, body, border=0)
                current_y = pdf.get_y() + 2

        pdf.set_text_color(0, 0, 0)


    def _draw_certification_strip(pdf: EnquiryPDF, title_area_width: float) -> bool:
        x = pdf.l_margin + 48 + title_area_width + 4
        y = 10
        max_width = pdf.w - pdf.r_margin - x
        height = 20

        if max_width <= 0:
            return False

        if CERTIFICATION_STRIP_PATH.exists():
            try:
                pdf.image(str(CERTIFICATION_STRIP_PATH), x=x, y=y, h=height, keep_aspect_ratio=True)
                return True
            except Exception:
                pass

        # Fallback vector rendering resembling the certification logos.
        circle_diameter = min(20, max_width / 2.5)
        spacing = min(8, max_width * 0.12)
        text_offset = circle_diameter * 0.28

        pdf.set_draw_color(25, 73, 160)
        pdf.set_text_color(25, 73, 160)
        pdf.set_line_width(0.6)

        for idx, (label, lines) in enumerate(
            (
                ("IATF", ("16949", "2016")),
                ("ISO", ("9001", "2015")),
            )
        ):
            cx = x + idx * (circle_diameter + spacing)
            pdf.ellipse(cx, y, circle_diameter, circle_diameter)

            pdf.set_xy(cx, y + circle_diameter / 2 - text_offset)
            pdf.set_font("Helvetica", "B", circle_diameter * 0.32)
            pdf.cell(circle_diameter, 5, label, align="C")

            pdf.set_font("Helvetica", "B", circle_diameter * 0.24)
            line_y = y + circle_diameter + 1
            for line in lines:
                pdf.set_xy(cx, line_y)
                pdf.cell(circle_diameter, 4, line, align="C")
                line_y += 4

        return False


    def _draw_certification_badges(pdf: EnquiryPDF) -> None:
        top = 12
        badge_width = 18
        badge_height = 28
        spacing = 6
        box_width = 42
        box_height = 24

        pdf.set_draw_color(25, 73, 160)
        pdf.set_text_color(25, 73, 160)

        badge_paths = [IATF_BADGE_PATH, ISO_BADGE_PATH]
        badges_width = len(badge_paths) * badge_width + (len(badge_paths) - 1) * spacing
        base_x = pdf.w - pdf.r_margin - box_width - spacing - badges_width

        x = base_x
        for badge_path in badge_paths:
            if badge_path.exists():
                pdf.image(str(badge_path), x=x, y=top, w=badge_width, h=badge_height)
            x += badge_width + spacing

        box_x = x
        pdf.set_draw_color(0, 0, 0)
        pdf.rect(box_x, top, box_width, box_height)
        pdf.set_font("Helvetica", "", 7.5)
        info_rows = [
            "IATF - 8.3.3.1",
            "Doc. No.: FA/MK/04",
            "Issue No./Date : 01/01.01.2013",
            "Rev. No./Date : 00",
        ]
        pdf.set_text_color(0, 0, 0)
        row_height = box_height / len(info_rows)
        current_y = top
        for text in info_rows:
            pdf.set_xy(box_x + 2, current_y + (row_height / 2) - 1.8)
            pdf.cell(box_width - 4, 3.6, text)
            current_y += row_height
            pdf.line(box_x, current_y, box_x + box_width, current_y)

        pdf.set_draw_color(0, 0, 0)
        pdf.set_text_color(0, 0, 0)


    def _draw_table_branding(pdf: EnquiryPDF, table_left: float, table_top: float, table_width: float) -> None:
        if not TABLE_OVERLAY_PATH.exists():
            return

        max_width = min(60.0, table_width)
        if max_width <= 0:
            return

        x = table_left + table_width - max_width - 6
        y = max(table_top - 26, 20)
        try:
            pdf.image(str(TABLE_OVERLAY_PATH), x=x, y=y, w=max_width, keep_aspect_ratio=True)
        except Exception:
            pass


    def _apply_template(pdf: EnquiryPDF) -> bool:
        if not TEMPLATE_PATH.exists() or not hasattr(pdf, "import_page"):
            return False
        try:
            template = pdf.import_page(str(TEMPLATE_PATH), page_number=1)
            pdf.use_template(template, x=0, y=0, w=pdf.w, h=pdf.h)
            return True
        except Exception:
            return False


    def build_pdf(enquiry: Enquiry) -> bytes:  # pragma: no cover - relies on fpdf2
        pdf = EnquiryPDF()
        pdf.add_page()
        pdf.set_title(f"Enquiry {enquiry.enquiry_id}")
        pdf.set_author("AVIGIRI Urethane & Rubber Industries Pvt.Ltd.")

        template_applied = _apply_template(pdf)
        if template_applied:
            _draw_header(pdf, enquiry, branding=False)
        else:
            _draw_header(pdf, enquiry)

        _draw_table(pdf, enquiry, branding_enabled=not template_applied)
        _draw_footer(pdf)

        content = pdf.output(dest="S")
        if isinstance(content, str):
            return content.encode("latin-1")
        return bytes(content)

else:  # Lightweight fallback -------------------------------------------------

    _DETAIL_LABELS: Sequence[tuple[str, str]] = (
        ("material_customer", "Material (As per customer)"),
        ("feasibility", "Feasibility"),
        ("required_trials_meters", "Req Trials (1 Trial = 10 m)"),
        ("die_development_charge", "Die Development Charge"),
        ("die_tool_development_charge_usd", "Die Tool Charge (USD)"),
        ("supplier_mold_cost", "Supplier Mold Cost"),
        ("supplier_cost_per_piece", "Supplier Cost / Piece"),
        ("mold_development_cost", "Mold Development Cost"),
        ("mold_development_charge_usd", "Mold Development Charge (USD)"),
        ("manufacturing_process", "Manufacturing Process"),
        ("manufacturing_process_internal", "Manufacturing Process (Internal)"),
        ("surface_treatment", "Surface Treatment"),
        ("area_or_volume", "Area / Volume"),
        ("gasket_thickness_length_mm", "Gasket Thickness / Length (mm)"),
        ("die_number", "Die Number"),
        ("hardness_or_material", "Hardness / Material"),
        ("specific_gravity", "Specific Gravity"),
        ("weight_per_meter_g", "Weight / Meter (g)"),
        ("weight_per_piece_g", "Weight / Piece (g)"),
        ("price_per_kg", "Price / Kg"),
        ("minimum_order_quantity", "Minimum Order Quantity"),
        ("price_per_piece", "Price / Piece"),
        ("total_price_per_piece", "Total Price / Piece"),
        ("scrap_percent", "Scrap %"),
        ("final_price_per_piece", "Final Price / Piece"),
        ("remarks", "Remarks"),
    )

    def build_pdf(enquiry: Enquiry) -> bytes:  # pragma: no cover - thin wrapper
        lines = _build_plaintext_lines(enquiry)
        return _render_basic_pdf(lines)

    def _build_plaintext_lines(enquiry: Enquiry) -> list[str]:
        lines: list[str] = []
        add = lines.append

        add("AVIGIRI Urethane & Rubber Industries Pvt. Ltd.")
        add("Enquiry Export (lightweight)")
        add("" )

        _add_wrapped_line(lines, "Enquiry ID", enquiry.enquiry_id)
        status_parts: list[str] = []
        if _field_visible(enquiry, "status"):
            status_parts.append(enquiry.status.upper())
        if _field_visible(enquiry, "priority"):
            status_parts.append(f"Priority {enquiry.priority.upper()}")
        if _field_visible(enquiry, "channel"):
            status_parts.append(f"Channel {enquiry.channel.title()}")
        if status_parts:
            _add_wrapped_line(lines, "Status", " • ".join(status_parts))
        if _field_visible(enquiry, "customer_name"):
            _add_wrapped_line(lines, "Customer", enquiry.customer_name)
        if enquiry.assigned_to and _field_visible(enquiry, "assigned_to"):
            _add_wrapped_line(lines, "Owner", enquiry.assigned_to)
        if _field_visible(enquiry, "subject"):
            _add_wrapped_line(lines, "Subject", enquiry.subject)
        _add_wrapped_line(lines, "Received", _format_datetime(enquiry.received_at))
        if enquiry.project_name and _field_visible(enquiry, "project_name"):
            _add_wrapped_line(lines, "Project", enquiry.project_name)
        if enquiry.customer_reference and _field_visible(enquiry, "customer_reference"):
            _add_wrapped_line(lines, "Customer Ref", enquiry.customer_reference)
        lines.append("")

        if enquiry.description and _field_visible(enquiry, "description"):
            add("Description:")
            for wrapped in _wrap_text(enquiry.description):
                add(f"  {wrapped}")
            add("")

        selected_entries = _get_selected_entries(enquiry)
        if selected_entries:
            add("Selected Fields:")
            for entry in selected_entries:
                _add_wrapped_line(lines, entry["label"], entry["value"], indent="  ")
            add("")
        else:
            add("Commercial Details:")
            for field, label in _DETAIL_LABELS:
                value = getattr(enquiry, field, None)
                if not _field_visible(enquiry, field):
                    continue
                if value in (None, "", []):
                    continue
                if isinstance(value, float):
                    value_str = f"{value:,.2f}"
                elif isinstance(value, int):
                    value_str = f"{value:,}"
                else:
                    value_str = str(value)
                _add_wrapped_line(lines, label, value_str, indent="  ")
            add("")

        profiles = [att for att in enquiry.attachments if att.category == "profile"]
        others = [att for att in enquiry.attachments if att.category != "profile"]
        if profiles:
            add("Profile:")
            for attachment in profiles:
                meta = f"{attachment.file_name} ({attachment.size_bytes:,} bytes)"
                _add_wrapped_line(lines, "-", meta, indent="  ")
            add("")
        if others:
            add("Attachments:")
            for attachment in others:
                meta = f"{attachment.file_name} ({attachment.size_bytes:,} bytes)"
                _add_wrapped_line(lines, "-", meta, indent="  ")
            add("")

        add("Generated: " + datetime.utcnow().strftime("%d %b %Y %H:%M UTC"))
        add("Note: Install 'fpdf2' in the virtualenv for the full branded layout.")

        return lines

    def _add_wrapped_line(lines: list[str], label: str, value: str, *, indent: str = "") -> None:
        wrapped = _wrap_text(value)
        if not wrapped:
            return
        prefix = f"{indent}{label}: "
        lines.append(prefix + wrapped[0])
        continuation = " " * len(prefix)
        for item in wrapped[1:]:
            lines.append(continuation + item)

    def _wrap_text(text: str, width: int = 92) -> list[str]:
        normalized = " ".join(text.split())
        return textwrap.wrap(normalized, width=width) or [""]

    def _render_basic_pdf(lines: Sequence[str]) -> bytes:
        header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        font_size = 11
        leading = 14
        start_x = 72  # 1 inch margin
        start_y = 792 - 72  # top margin

        def _escape(text: str) -> str:
            return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

        content_lines = [
            "BT",
            f"/F1 {font_size} Tf",
            f"{start_x} {start_y} Td",
            f"{leading} TL",
        ]
        first = True
        for line in lines:
            safe = _escape(line)
            if first:
                content_lines.append(f"({safe}) Tj")
                first = False
            else:
                content_lines.append("T*")
                content_lines.append(f"({safe}) Tj")
        content_lines.append("ET")
        content = "\n".join(content_lines) + "\n"
        content_bytes = _to_latin1(content).encode("latin-1")

        objects: list[bytes] = []
        objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
        objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
        objects.append(
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        )
        objects.append(b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
        objects.append(
            f"5 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode("latin-1")
            + content_bytes
            + b"endstream\nendobj\n"
        )

        buffer = bytearray()
        buffer.extend(header)

        xref_positions = [0]
        for obj in objects:
            xref_positions.append(len(buffer))
            buffer.extend(obj)

        xref_start = len(buffer)
        buffer.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        buffer.extend(b"0000000000 65535 f \n")
        for pos in xref_positions[1:]:
            buffer.extend(f"{pos:010d} 00000 n \n".encode("latin-1"))
        buffer.extend(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1))
        buffer.extend(b"startxref\n")
        buffer.extend(f"{xref_start}\n".encode("latin-1"))
        buffer.extend(b"%%EOF\n")
        return bytes(buffer)

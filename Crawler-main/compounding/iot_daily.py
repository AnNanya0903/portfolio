"""The daily batch-variation view: which compounds were mixed, how many batches, and how each batch compares
with its compound's standard. Also the day timeline, the live band for a running batch, and the daily report
(CSV / PDF)."""
from __future__ import annotations

import csv
import io
from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from django.utils import timezone

from . import iot_config, iot_samples, iot_segments, iot_standards
from .models import IST_TIMEZONE, CompoundingIoTRun, DetectedBatch, MachineSegment

RESULT_LABELS = dict(DetectedBatch.RESULT_CHOICES)
RESULT_TONES = {
    DetectedBatch.RESULT_OK: "success",
    DetectedBatch.RESULT_VARIATION: "warning",
    DetectedBatch.RESULT_MAJOR: "danger",
    DetectedBatch.RESULT_NO_STANDARD: "secondary",
    "": "secondary",
}
# Line colours on the overlay chart: OK grey-green, Variation amber, Major red.
RESULT_COLOURS = {
    DetectedBatch.RESULT_OK: "#6c9a7b",
    DetectedBatch.RESULT_VARIATION: "#f0a500",
    DetectedBatch.RESULT_MAJOR: "#dc3545",
    DetectedBatch.RESULT_NO_STANDARD: "#8a94a6",
    "": "#8a94a6",
}
# Compound colours on the day timeline, in order of first appearance.
COMPOUND_PALETTE = ["#0d6efd", "#6f42c1", "#20c997", "#d63384", "#fd7e14", "#0dcaf0", "#198754", "#6610f2"]
STATE_COLOURS = {
    MachineSegment.STATE_OFF: "#343a40",
    MachineSegment.STATE_IDLE: "#ced4da",
    MachineSegment.STATE_NO_DATA: "#f8f9fa",
}


def _local(value: Optional[datetime]) -> str:
    return value.astimezone(IST_TIMEZONE).strftime("%H:%M") if value else ""


def _day_range(day: date):
    start_ts, end_ts = iot_segments.day_bounds_ms(day)
    return iot_segments.ms_to_datetime(start_ts), iot_segments.ms_to_datetime(end_ts)


def day_batches(machine_id: str, day: date) -> List[DetectedBatch]:
    start, end = _day_range(day)
    return list(
        DetectedBatch.objects.filter(machine_id=machine_id, start_at__gte=start, start_at__lt=end)
        .select_related("standard", "log_run")
        .order_by("start_at")
    )


def batch_row(batch: DetectedBatch, number: int) -> Dict[str, Any]:
    result = "running" if batch.is_open else batch.result
    return {
        "number": number,
        "id": batch.pk,
        "batch": batch,
        "compound": batch.compound_label,
        "start": _local(batch.start_at),
        "end": "now" if batch.is_open else _local(batch.end_at),
        "minutes": round(batch.duration_s / 60, 1),
        "kwh": round(batch.energy_kwh, 1) if batch.energy_kwh is not None else None,
        "match_pct": batch.match_pct,
        "result": result,
        "result_label": (
            "Running" if batch.is_open else "Partial – start unknown" if batch.is_partial
            else RESULT_LABELS.get(batch.result, "—")
        ),
        "tone": "info" if batch.is_open else RESULT_TONES.get(batch.result, "secondary"),
        "reasons": batch.reasons or [],
        "excluded": batch.excluded,
    }


def build_day(machine_id: str, day: date) -> Dict[str, Any]:
    batches = day_batches(machine_id, day)
    sections: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for index, batch in enumerate(batches, start=1):
        compound = batch.compound_label
        section = sections.setdefault(compound, {
            "compound": compound,
            "logged": bool(batch.compound_code),
            "rows": [],
            "lines": [],
            "counts": {DetectedBatch.RESULT_OK: 0, DetectedBatch.RESULT_VARIATION: 0, DetectedBatch.RESULT_MAJOR: 0},
            "minutes": [],
            "kwh": [],
        })
        row = batch_row(batch, index)
        section["rows"].append(row)
        if batch.result in section["counts"]:
            section["counts"][batch.result] += 1
        if not batch.is_open:
            section["minutes"].append(batch.duration_s / 60)
            if batch.energy_kwh is not None:
                section["kwh"].append(batch.energy_kwh)
        if batch.curve:
            section["lines"].append({
                "id": batch.pk,
                "label": f"#{index} {row['start']}–{row['end']}",
                "step": batch.curve_step_s,
                "curve": batch.curve,
                "result": row["result"],
                "colour": RESULT_COLOURS.get(batch.result, "#8a94a6") if not batch.is_open else "#0dcaf0",
            })

    compound_order = list(sections)
    for compound, section in sections.items():
        standard = iot_standards.active_standard(section["compound"]) if section["logged"] else None
        count, needed = iot_standards.readiness(section["compound"]) if section["logged"] else (0, 0)
        section.update({
            "standard": standard,
            "standard_points": standard.points if standard else [],
            "ready_count": min(count, needed),
            "needed": needed,
            "batch_count": len(section["rows"]),
            "avg_minutes": round(sum(section["minutes"]) / len(section["minutes"]), 1) if section["minutes"] else None,
            "avg_kwh": round(sum(section["kwh"]) / len(section["kwh"]), 1) if section["kwh"] else None,
            "colour": COMPOUND_PALETTE[compound_order.index(compound) % len(COMPOUND_PALETTE)],
            "chart_id": f"daily-chart-{compound_order.index(compound) + 1}",
            "chart": {
                "standard": standard.points if standard else [],
                "lines": section["lines"],
            },
        })
    total = len(batches)
    return {
        "day": day,
        "batches": batches,
        "total": total,
        "variations": sum(1 for batch in batches if batch.result in (DetectedBatch.RESULT_VARIATION, DetectedBatch.RESULT_MAJOR)),
        "sections": list(sections.values()),
        "timeline": timeline(machine_id, day, {name: section["colour"] for name, section in sections.items()}),
    }


def timeline(machine_id: str, day: date, compound_colours: Dict[str, str]) -> Dict[str, Any]:
    """The day's segments as bar pieces (percent left/width over the span with data)."""
    start, end = _day_range(day)
    segments = list(
        MachineSegment.objects.filter(machine_id=machine_id, start_at__gte=start, start_at__lt=end)
        .select_related("detected_batch")
        .order_by("start_at")
    )
    if not segments:
        return {"pieces": [], "start": "", "end": ""}
    span_start, span_end = segments[0].start_at, segments[-1].end_at
    span = max((span_end - span_start).total_seconds(), 1.0)
    pieces = []
    for segment in segments:
        batch = segment.detected_batch
        if segment.state == MachineSegment.STATE_BATCH and batch is not None:
            colour = compound_colours.get(batch.compound_label, "#8a94a6")
            result = "running" if batch.is_open else batch.result
            title = (f"{batch.compound_label} {_local(segment.start_at)}–{_local(segment.end_at)} · "
                     f"{'Running' if batch.is_open else RESULT_LABELS.get(batch.result, '')}")
        else:
            colour = STATE_COLOURS.get(segment.state, "#ced4da")
            result = ""
            title = f"{segment.get_state_display()} {_local(segment.start_at)}–{_local(segment.end_at)}"
        pieces.append({
            "left": round((segment.start_at - span_start).total_seconds() / span * 100, 3),
            "width": round(max(segment.duration_s, 1) / span * 100, 3),
            "state": segment.state,
            "colour": colour,
            "result": result,
            "title": title,
            "batch_id": batch.pk if batch is not None and segment.state == MachineSegment.STATE_BATCH else None,
        })
    return {"pieces": pieces, "start": _local(span_start), "end": _local(span_end)}


# Live band ------------------------------------------------------------------------------------------------
def live_status(machine_id: str) -> Optional[Dict[str, Any]]:
    """For the batch on the mill now: its compound's standard band in clock time, and a warning when the
    current has left the band."""
    batch = (
        DetectedBatch.objects.filter(machine_id=machine_id, is_open=True).order_by("-start_at").first()
    )
    if batch is None:
        return None
    open_entry = (
        CompoundingIoTRun.objects.filter(machine_id=machine_id, ended_at__isnull=True).order_by("-started_at").first()
    )
    compound = (open_entry.compound_code if open_entry else "") or batch.compound_code
    standard = iot_standards.active_standard(compound)
    status: Dict[str, Any] = {"batchId": batch.pk, "compound": compound or DetectedBatch.NOT_LOGGED,
                              "standardReady": standard is not None, "band": [], "warning": ""}
    if standard is None:
        if compound:
            count, needed = iot_standards.readiness(compound)
            status["warning"] = ""
            status["note"] = f"Standard not ready ({min(count, needed)}/{needed})"
        return status
    load_ts = iot_segments.datetime_to_ms(batch.load_at) if batch.load_at else None
    if load_ts is None:
        status["note"] = "Waiting for the loading peak"
        return status
    status["version"] = standard.version
    status["loadTs"] = load_ts
    status["band"] = [{"ts": load_ts + point["t"] * 1000, "lower": point["lower"], "upper": point["upper"],
                       "median": point["median"]} for point in standard.points]
    # How the current has done since loading, from the stored samples (fresher than the batch's curve).
    now_ms = int(timezone.now().timestamp() * 1000)
    samples = iot_samples.samples_between(machine_id, load_ts, now_ms)
    curve = iot_segments.resample_curve(samples, load_ts, now_ms, standard.step_s)
    outside_since = None
    for index, point in enumerate(standard.points):
        if index >= len(curve):
            break
        inside = point["lower"] <= curve[index] <= point["upper"]
        outside_since = None if inside else (outside_since if outside_since is not None else point["t"])
    if outside_since is not None and len(curve) * standard.step_s - outside_since >= 2 * standard.step_s:
        minutes, seconds = divmod(int(outside_since), 60)
        status["warning"] = f"Current left the {compound} band at {minutes}:{seconds:02d} after loading"
    return status


# Daily report ---------------------------------------------------------------------------------------------
def report_csv(day_data: Dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([f"Batch variation report {day_data['day'].isoformat()}"])
    writer.writerow([])
    writer.writerow(["Compound", "Batches", "OK", "Variation", "Major variation", "Avg min", "Avg kWh", "Standard"])
    for section in day_data["sections"]:
        counts = section["counts"]
        standard = section["standard"]
        writer.writerow([
            section["compound"], section["batch_count"], counts["ok"], counts["variation"], counts["major"],
            section["avg_minutes"] if section["avg_minutes"] is not None else "",
            section["avg_kwh"] if section["avg_kwh"] is not None else "",
            f"v{standard.version}" if standard else f"not ready ({section['ready_count']}/{section['needed']})",
        ])
    writer.writerow([])
    writer.writerow(["#", "Compound", "Start", "End", "Min", "kWh", "Match %", "Result", "Reasons"])
    for section in day_data["sections"]:
        for row in section["rows"]:
            writer.writerow([
                row["number"], row["compound"], row["start"], row["end"], row["minutes"],
                "" if row["kwh"] is None else f"{row['kwh']:.1f}",
                "" if row["match_pct"] is None else f"{row['match_pct']:.0f}",
                row["result_label"], "; ".join(row["reasons"]),
            ])
    return output.getvalue()


def report_pdf(day_data: Dict[str, Any], machine_id: str) -> bytes:
    """A one-page-per-day summary PDF: totals per compound, then every batch with its result and reasons."""
    from fpdf import FPDF

    def text(value: Any) -> str:
        # The core PDF fonts are Latin-1: replace the few typographic characters we use.
        return str(value).replace("–", "-").replace("—", "-").replace("±", "+/-").encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, text(f"Batch variation report - {day_data['day']:%d %b %Y}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, text(f"Machine {machine_id} - {day_data['total']} batches, {day_data['variations']} with a variation"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 10)
    widths = [44, 18, 14, 20, 24, 18, 18, 30]
    for header, width in zip(["Compound", "Batches", "OK", "Variation", "Major", "Avg min", "Avg kWh", "Standard"], widths):
        pdf.cell(width, 7, header, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    for section in day_data["sections"]:
        counts = section["counts"]
        standard = section["standard"]
        values = [section["compound"], section["batch_count"], counts["ok"], counts["variation"], counts["major"],
                  section["avg_minutes"] if section["avg_minutes"] is not None else "-",
                  section["avg_kwh"] if section["avg_kwh"] is not None else "-",
                  f"v{standard.version}" if standard else f"not ready {section['ready_count']}/{section['needed']}"]
        for value, width in zip(values, widths):
            pdf.cell(width, 7, text(value), border=1)
        pdf.ln()
    pdf.ln(4)

    for section in day_data["sections"]:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, text(section["compound"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for row in section["rows"]:
            match = "-" if row["match_pct"] is None else f"{row['match_pct']:.0f} %"
            kwh = "-" if row["kwh"] is None else f"{row['kwh']:.1f} kWh"
            line = f"#{row['number']}  {row['start']}-{row['end']}  {row['minutes']} min  {kwh}  match {match}  {row['result_label']}"
            pdf.cell(0, 5, text(line), new_x="LMARGIN", new_y="NEXT")
            if row["reasons"]:
                pdf.set_x(pdf.l_margin + 6)
                pdf.multi_cell(0, 5, text("; ".join(row["reasons"])), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    return bytes(pdf.output())


def end_of_day_summary(day_data: Dict[str, Any]) -> List[str]:
    """Plain lines: count per compound, then every variation batch with its reasons."""
    lines = [f"{day_data['day']:%d %b %Y}: {day_data['total']} batches."]
    for section in day_data["sections"]:
        counts = section["counts"]
        lines.append(
            f"{section['compound']}: {section['batch_count']} batches "
            f"({counts['ok']} OK, {counts['variation']} variation, {counts['major']} major)."
        )
    for section in day_data["sections"]:
        for row in section["rows"]:
            if row["result"] in (DetectedBatch.RESULT_VARIATION, DetectedBatch.RESULT_MAJOR):
                lines.append(f"#{row['number']} {row['compound']} {row['start']}–{row['end']}: "
                             f"{row['result_label']} - {'; '.join(row['reasons'])}")
    return lines


def timeline_min_state_s() -> int:
    return iot_config.timeline_min_state_s()


def summary_facts(machine_id: str, day: date) -> List[str]:
    """The day's computed numbers for the AI summary: per compound, then every variation batch with its
    numbers and reasons. The results here are final; the AI only explains them."""
    data = build_day(machine_id, day)
    lines = [f"Date {day.isoformat()}: {data['total']} batches, {data['variations']} with a variation."]
    for section in data["sections"]:
        counts = section["counts"]
        standard = section["standard"]
        std = (f"standard v{standard.version} from {standard.built_from_n} batches" if standard
               else f"standard not ready ({section['ready_count']}/{section['needed']})" if section["logged"]
               else "not logged by the operator")
        lines.append(
            f"{section['compound']}: {section['batch_count']} batches; {counts['ok']} OK, {counts['variation']} "
            f"Variation, {counts['major']} Major variation; avg {section['avg_minutes']} min, "
            f"{section['avg_kwh']} kWh; {std}."
        )
    for section in data["sections"]:
        standard = section["standard"]
        stats = (standard.stats or {}) if standard else {}
        for row in section["rows"]:
            if row["result"] not in (DetectedBatch.RESULT_VARIATION, DetectedBatch.RESULT_MAJOR):
                continue
            batch = row["batch"]
            parts = [f"{row['minutes']} min", f"{row['kwh']} kWh" if row["kwh"] is not None else "",
                     f"match {row['match_pct']:.0f} %" if row["match_pct"] is not None else ""]
            if stats.get("duration_s") and stats.get("energy_kwh"):
                parts.append(f"standard {stats['duration_s']['median'] / 60:.1f} min / {stats['energy_kwh']['median']:.1f} kWh")
            if batch.steady_current is not None:
                parts.append(f"steady {batch.steady_current:.0f} A")
            lines.append(
                f"Batch #{row['number']} {row['compound']} {row['start']}-{row['end']}: RESULT {row['result_label']} "
                f"({', '.join(part for part in parts if part)}). Reasons: {'; '.join(row['reasons']) or 'none'}."
            )
    return lines

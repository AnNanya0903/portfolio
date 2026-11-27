"""Machine vs log reconciliation for one machine and day.

Rows are the machine's segments in time order (every BATCH, plus IDLE / OFF / No-data stretches longer than
IOT_RECON_MIN_IDLE_S), with operator logs that overlap no batch added as "No machine run" rows. Each log is
matched to the detected batch it overlaps most.

Verdicts (thresholds in settings):
    Mis-logged         logged duration < 50 % of the detected batch
    Time mismatch      start or end more than 3 min from the machine's
    Compound mismatch  operator's compound differs from the AI's, at AI confidence >= 80 %
    Matches            none of the above
    Not logged         a batch no log overlaps
    No machine run     a log that overlaps no batch
    Idle / Off / No data
"""
from __future__ import annotations

import csv
import io
from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.utils import timezone

from . import iot_config, iot_patterns, iot_segments, iot_standards
from .models import IST_TIMEZONE, CompoundingIoTRun, DetectedBatch, IoTRunCorrection, MachineSegment

MATCHES = "matches"
MISLOGGED = "mislogged"
TIME_MISMATCH = "time_mismatch"
COMPOUND_MISMATCH = "compound_mismatch"
NOT_LOGGED = "not_logged"
NO_MACHINE_RUN = "no_machine_run"
IDLE = "idle"
OFF = "off"
NO_DATA = "no_data"

VERDICT_LABELS = {
    MATCHES: "Matches",
    MISLOGGED: "Mis-logged",
    TIME_MISMATCH: "Time mismatch",
    COMPOUND_MISMATCH: "Compound mismatch",
    NOT_LOGGED: "Not logged",
    NO_MACHINE_RUN: "No machine run",
    IDLE: "Idle",
    OFF: "Off",
    NO_DATA: "No data",
}
# Bootstrap badge colour per verdict.
VERDICT_TONES = {
    MATCHES: "success",
    MISLOGGED: "danger",
    TIME_MISMATCH: "warning",
    COMPOUND_MISMATCH: "danger",
    NOT_LOGGED: "warning",
    NO_MACHINE_RUN: "secondary",
    IDLE: "light",
    OFF: "dark",
    NO_DATA: "light",
}
# Rows a supervisor can fix by accepting the machine's times.
CORRECTABLE = {MISLOGGED, TIME_MISMATCH}


def _local(value: Optional[datetime]) -> str:
    return value.astimezone(IST_TIMEZONE).strftime("%H:%M") if value else ""


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _signed_minutes(seconds: float) -> str:
    return f"{seconds / 60:+.0f} min"


def _overlap_s(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    return max((min(a_end, b_end) - max(a_start, b_start)).total_seconds(), 0.0)


def batch_compound(batch: DetectedBatch) -> str:
    """The compound a batch counts under in summaries: the tag/log label, else the AI's prediction."""
    return batch.label_compound or (batch.log_run.compound_code if batch.log_run_id else "") or batch.predicted_label


def verdict_for(batch: DetectedBatch, log: CompoundingIoTRun) -> Dict[str, Any]:
    tolerance = iot_config.recon_time_tolerance_s()
    log_end = iot_standards.log_end(log)
    detected_s = max((batch.end_at - batch.start_at).total_seconds(), 1.0)
    logged_s = max((log_end - log.started_at).total_seconds(), 0.0)
    delta_start = (log.started_at - batch.start_at).total_seconds()
    delta_end = (log_end - batch.end_at).total_seconds() if not batch.is_open or log.ended_at else 0.0

    if not batch.is_open and logged_s < iot_config.recon_mislog_ratio() * detected_s:
        return {"verdict": MISLOGGED,
                "reason": f"logged {_minutes(logged_s):g} min of {_minutes(detected_s):g} min"}
    if abs(delta_start) > tolerance or abs(delta_end) > tolerance:
        parts = []
        if abs(delta_start) > tolerance:
            parts.append(f"start {_signed_minutes(delta_start)}")
        if abs(delta_end) > tolerance:
            parts.append(f"end {_signed_minutes(delta_end)}")
        return {"verdict": TIME_MISMATCH, "reason": ", ".join(parts)}
    operator_key = iot_standards.key_for(log.compound_code)[0]
    if (batch.predicted_key and batch.predicted_key != operator_key
            and (batch.prediction_confidence or 0) >= iot_config.recon_compound_confidence()):
        return {"verdict": COMPOUND_MISMATCH,
                "reason": f"AI: {batch.predicted_label} ({batch.prediction_confidence:.0f} %)"}
    return {"verdict": MATCHES, "reason": ""}


def _ai_pattern(batch: DetectedBatch) -> str:
    if batch.predicted_label:
        confidence = f" ({batch.prediction_confidence:.0f} %)" if batch.prediction_confidence is not None else ""
        return f"{batch.predicted_label}{confidence}"
    return "Unknown pattern"


def _match_text(batch: DetectedBatch) -> str:
    if batch.match_result == DetectedBatch.MATCH_NO_STANDARD or not batch.match_result:
        return "No standard yet"
    label = "Matches standard" if batch.match_result == DetectedBatch.MATCH_OK else "Doesn't match"
    return f"{batch.match_pct:.0f} % · {label}" if batch.match_pct is not None else label


def _log_text(log: Optional[CompoundingIoTRun]) -> str:
    if log is None:
        return ""
    end = _local(log.ended_at) if log.ended_at else "running"
    return f"{log.compound_code} {_local(log.started_at)}–{end}"


def build_rows(machine_id: str, day: date) -> List[Dict[str, Any]]:
    start_ts, end_ts = iot_segments.day_bounds_ms(day)
    day_start, day_end = iot_segments.ms_to_datetime(start_ts), iot_segments.ms_to_datetime(end_ts)
    segments = list(
        MachineSegment.objects.filter(machine_id=machine_id, start_at__gte=day_start, start_at__lt=day_end)
        .select_related("detected_batch", "detected_batch__log_run", "detected_batch__standard")
        .order_by("start_at")
    )
    batches = [segment.detected_batch for segment in segments
               if segment.state == MachineSegment.STATE_BATCH and segment.detected_batch_id]
    logs = list(
        CompoundingIoTRun.objects.filter(machine_id=machine_id, started_at__lt=day_end)
        .exclude(ended_at__lt=day_start)
        .order_by("started_at")
    )

    # Each log goes to the detected batch it overlaps most; the batch keeps its best-overlapping log.
    logs_by_batch: Dict[int, List[CompoundingIoTRun]] = {}
    unmatched_logs: List[CompoundingIoTRun] = []
    for log in logs:
        end = iot_standards.log_end(log)
        best, best_s = None, 0.0
        for batch in batches:
            seconds = _overlap_s(log.started_at, end, batch.start_at, batch.end_at)
            if seconds > best_s:
                best, best_s = batch, seconds
        if best is None:
            if day_start <= log.started_at < day_end:
                unmatched_logs.append(log)
        else:
            logs_by_batch.setdefault(best.pk, []).append(log)

    min_idle = iot_config.recon_min_idle_s()
    rows: List[Dict[str, Any]] = []
    for segment in segments:
        if segment.state == MachineSegment.STATE_BATCH and segment.detected_batch_id:
            batch = segment.detected_batch
            matched = sorted(
                logs_by_batch.get(batch.pk, []),
                key=lambda log: _overlap_s(log.started_at, iot_standards.log_end(log), batch.start_at, batch.end_at),
                reverse=True,
            )
            log = matched[0] if matched else None
            outcome = verdict_for(batch, log) if log else {"verdict": NOT_LOGGED, "reason": "no operator log"}
            rows.append({
                "kind": "batch",
                "start": batch.start_at,
                "end": batch.end_at,
                "is_open": batch.is_open,
                "duration_min": _minutes(batch.duration_s),
                "energy_kwh": round(batch.energy_kwh, 1) if batch.energy_kwh is not None else None,
                "ai_pattern": _ai_pattern(batch),
                "match": _match_text(batch),
                "match_result": batch.match_result,
                "log": log,
                "log_text": _log_text(log),
                "extra_logs": len(matched) - 1 if matched else 0,
                "batch": batch,
                **outcome,
            })
            continue
        if segment.duration_s <= min_idle and not segment.is_open:
            continue
        verdict = {MachineSegment.STATE_IDLE: IDLE, MachineSegment.STATE_OFF: OFF}.get(segment.state, NO_DATA)
        rows.append({
            "kind": "state",
            "start": segment.start_at,
            "end": segment.end_at,
            "is_open": segment.is_open,
            "duration_min": _minutes(segment.duration_s),
            "energy_kwh": None,
            "ai_pattern": VERDICT_LABELS[verdict],
            "match": "",
            "log": None,
            "log_text": "",
            "extra_logs": 0,
            "batch": None,
            "verdict": verdict,
            "reason": "",
        })
    for log in unmatched_logs:
        end = iot_standards.log_end(log)
        rows.append({
            "kind": "log",
            "start": log.started_at,
            "end": end,
            "is_open": log.ended_at is None,
            "duration_min": _minutes((end - log.started_at).total_seconds()),
            "energy_kwh": None,
            "ai_pattern": "",
            "match": "",
            "log": log,
            "log_text": _log_text(log),
            "extra_logs": 0,
            "batch": None,
            "verdict": NO_MACHINE_RUN,
            "reason": "the machine ran no batch then",
        })
    rows.sort(key=lambda row: (row["start"], 0 if row["kind"] != "log" else 1))
    for row in rows:
        row["verdict_label"] = VERDICT_LABELS[row["verdict"]]
        row["tone"] = VERDICT_TONES[row["verdict"]]
        row["start_text"] = _local(row["start"])
        row["end_text"] = "now" if row["is_open"] and row["kind"] != "log" else _local(row["end"])
        row["correctable"] = row["verdict"] in CORRECTABLE and row["log"] is not None and row["batch"] is not None \
            and not row["batch"].is_open
    return rows


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    batch_rows = [row for row in rows if row["kind"] == "batch"]
    logged = [row for row in batch_rows if row["log"] is not None]
    per_compound: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for row in batch_rows:
        if row["is_open"]:
            continue
        compound = batch_compound(row["batch"]) or "Unknown pattern"
        entry = per_compound.setdefault(compound, {"compound": compound, "count": 0, "minutes": 0.0, "kwh": [], })
        entry["count"] += 1
        entry["minutes"] += row["duration_min"]
        if row["energy_kwh"] is not None:
            entry["kwh"].append(row["batch"].energy_kwh)
    compounds = [
        {
            "compound": entry["compound"],
            "count": entry["count"],
            "avg_minutes": round(entry["minutes"] / entry["count"], 1),
            "avg_kwh": round(sum(entry["kwh"]) / len(entry["kwh"]), 1) if entry["kwh"] else None,
        }
        for entry in sorted(per_compound.values(), key=lambda item: -item["count"])
    ]
    return {
        "batches": len(batch_rows),
        "log_matches": sum(1 for row in batch_rows if row["verdict"] == MATCHES),
        "logged": len(logged),
        "issues": sum(1 for row in rows if row["verdict"] in {MISLOGGED, TIME_MISMATCH, COMPOUND_MISMATCH,
                                                              NOT_LOGGED, NO_MACHINE_RUN}),
        "compounds": compounds,
    }


def accept_machine_times(batch: DetectedBatch, log: CompoundingIoTRun, user, verdict: str = "") -> IoTRunCorrection:
    """Correct an operator log to the detected batch's times, keeping an audit record."""
    if batch.is_open:
        raise ValueError("The batch is still running; accept its times once it has ended.")
    if log.machine_id != batch.machine_id:
        raise ValueError("The log and the batch are on different machines.")
    with transaction.atomic():
        log = CompoundingIoTRun.objects.select_for_update().get(pk=log.pk)
        correction = IoTRunCorrection.objects.create(
            run=log,
            detected_batch=batch,
            corrected_by=user,
            corrected_at=timezone.now(),
            verdict=verdict,
            old_started_at=log.started_at,
            old_ended_at=log.ended_at,
            new_started_at=batch.start_at,
            new_ended_at=batch.end_at,
        )
        log.started_at = batch.start_at
        log.ended_at = batch.end_at
        log.save(update_fields=["started_at", "ended_at", "updated_at"])
    iot_patterns.analyze_batch(batch)  # the corrected log may now label the batch
    return correction


CSV_HEADERS = ["Start", "End", "Duration (min)", "Energy (kWh)", "AI pattern", "Match", "Operator log",
               "Verdict", "Reason"]


def rows_csv(rows: List[Dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow([
            row["start_text"], row["end_text"], row["duration_min"],
            "" if row["energy_kwh"] is None else f"{row['energy_kwh']:.1f}",
            row["ai_pattern"], row["match"], row["log_text"], row["verdict_label"], row["reason"],
        ])
    return output.getvalue()

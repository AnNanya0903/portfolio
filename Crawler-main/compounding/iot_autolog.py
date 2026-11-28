"""Keep the batch log in step with the machine: every finished detected batch has exactly one log entry.

Order of work in sync_logs (safe to run any number of times):
  1. drop links that no longer overlap;
  2. split a manual entry that covers several batches into one entry per batch;
  3. pair the remaining entries and batches one to one, largest overlap first;
  4. give each paired entry the machine's times (audited when it was more than 3 min off or a mis-tap);
  5. create an "IoT auto" entry for every batch still without one;
  6. flag manual entries with no machine run, and remove untouched auto entries that lost their batch.

The compound is the operator's, else the single compound with batches remaining on that day's board
(to be confirmed), else "Unassigned". It is never guessed from the curve.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils import timezone

from . import iot_config
from .models import (
    CompoundingCRIN,
    CompoundingIoTRun as Run,
    CompoundingLine,
    CompoundingPlan,
    DetectedBatch,
    IoTRunAudit,
    compounding_business_date,
    next_compounding_cjc_number,
)

AUTO_NOTE = "Logged from IoT graph"
SNAPSHOT_FIELDS = (
    "compound_code", "crin_id", "batch_type", "batch_weight_kg", "stock_quality", "rejection_reason", "notes",
    "started_at", "ended_at", "source", "flag",
)


def snapshot(run: Run) -> Dict[str, Any]:
    data = {}
    for name in SNAPSHOT_FIELDS:
        value = getattr(run, name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        data[name] = value
    return data


def audit(run: Run, action: str, before: Dict[str, Any], user=None) -> None:
    IoTRunAudit.objects.create(
        run=None if action == "deleted" else run,
        run_pk=run.pk,
        action=action,
        before=before,
        after={} if action == "deleted" else snapshot(run),
        changed_by=user,
    )


def _end_of(run: Run) -> datetime:
    if run.ended_at:
        return run.ended_at
    cap = run.started_at + timedelta(seconds=iot_config.open_entry_max_s())
    return max(min(timezone.now(), cap), run.started_at)


def _overlap(run: Run, batch: DetectedBatch) -> float:
    return max((min(_end_of(run), batch.end_at) - max(run.started_at, batch.start_at)).total_seconds(), 0.0)


def plan_for(moment: datetime) -> CompoundingPlan:
    day = compounding_business_date(moment)
    plan = CompoundingPlan.objects.filter(plan_date=day).first()
    if plan is None:
        plan = CompoundingPlan.objects.create(plan_date=day, cjc_number=next_compounding_cjc_number(year=day.year))
    return plan


def confirmed_runs(plan: CompoundingPlan) -> List[Run]:
    return [run for run in Run.objects.filter(plan=plan, ended_at__isnull=False) if run.is_confirmed]


def board_suggestion(plan: CompoundingPlan) -> Tuple[str, Optional[CompoundingCRIN]]:
    """(compound, CRIN) when exactly one compound on the day's board still has FB batches to mix."""
    done: Dict[str, int] = {}
    for run in confirmed_runs(plan):
        done[run.compound_code] = done.get(run.compound_code, 0) + 1
    planned: Dict[str, int] = {}
    saved: Dict[str, int] = {}
    lines = list(CompoundingLine.objects.filter(plan=plan).exclude(compound_code="").select_related("crin_request"))
    for line in lines:
        code = line.compound_code.strip()
        fb = sum(int(v or 0) for v in (line.shift1_fb, line.shift2_fb, line.shift3_fb))
        planned[code] = planned.get(code, 0) + fb - int(line.returned_to_planning_fb or 0)
        saved[code] = saved.get(code, 0) + int(line.actual_fb or 0)
    remaining = [code for code, total in planned.items() if total - max(saved[code], done.get(code, 0)) > 0]
    if len(remaining) != 1:
        return "", None
    code = remaining[0]
    crins = {
        line.crin_request.pk: line.crin_request
        for line in lines
        if line.compound_code.strip() == code and line.crin_request
        and line.crin_request.status == CompoundingCRIN.STATUS_OPEN
    }
    return code, next(iter(crins.values())) if len(crins) == 1 else None


def _split(run: Run, covered: List[DetectedBatch]) -> None:
    """One entry per covered batch; the original keeps the first and its old values go to the audit trail."""
    before = snapshot(run)
    covered = sorted(covered, key=lambda batch: batch.start_at)
    for index, batch in enumerate(covered):
        entry = run if index == 0 else Run.objects.get(pk=run.pk)
        if index:
            entry.pk = None
            entry._state.adding = True
        entry.started_at, entry.ended_at = batch.start_at, batch.end_at
        entry.source, entry.flag = Run.SOURCE_SPLIT, ""
        entry.plan = plan_for(batch.start_at)
        entry.save()
        batch.log_run = entry
        batch.save(update_fields=["log_run", "updated_at"])
        audit(entry, "split", before)


def _apply_machine_times(run: Run, batch: DetectedBatch) -> None:
    if run.started_at == batch.start_at and run.ended_at == batch.end_at and not run.flag:
        return
    tolerance = iot_config.log_time_tolerance_s()
    wrong = False
    if run.source == Run.SOURCE_OPERATOR and run.ended_at:
        wrong = (
            abs((run.started_at - batch.start_at).total_seconds()) > tolerance
            or abs((run.ended_at - batch.end_at).total_seconds()) > tolerance
            or (run.ended_at - run.started_at).total_seconds() < iot_config.min_batch_seconds()
        )
    before = snapshot(run)
    run.started_at, run.ended_at, run.flag = batch.start_at, batch.end_at, ""
    if wrong:
        run.source = Run.SOURCE_CORRECTED
    run.save()
    if wrong:
        audit(run, "corrected", before)


def _create_auto(batch: DetectedBatch) -> Run:
    plan = plan_for(batch.start_at)
    compound, crin = board_suggestion(plan)
    run = Run.objects.create(
        plan=plan,
        machine_id=batch.machine_id,
        compound_code=compound or Run.UNASSIGNED,
        started_at=batch.start_at,
        ended_at=batch.end_at,
        stock_quality="",
        batch_type=Run.BATCH_FB,
        crin=crin,
        source=Run.SOURCE_AUTO,
        compound_needs_confirm=bool(compound),
        crin_needs_confirm=crin is not None,
        notes=AUTO_NOTE,
    )
    batch.log_run = run
    batch.save(update_fields=["log_run", "updated_at"])
    return run


def _untouched_auto(run: Run) -> bool:
    return run.source == Run.SOURCE_AUTO and not run.stock_quality and not run.audits.exists()


@transaction.atomic
def sync_logs(machine_id: str, start: datetime, end: datetime) -> Dict[str, int]:
    """Bring the log up to date for the detected batches starting in [start, end)."""
    batches = list(
        DetectedBatch.objects.select_for_update()
        .filter(machine_id=machine_id, start_at__gte=start, start_at__lt=end)
        .order_by("start_at")
    )
    runs = list(
        Run.objects.select_for_update()
        .filter(machine_id=machine_id, started_at__lt=end, started_at__gte=start - timedelta(hours=12))
        .order_by("started_at", "id")
    )
    runs = [run for run in runs if _end_of(run) > start or run.started_at >= start]
    by_id = {run.pk: run for run in runs}

    # 1. Links that no longer make sense (re-detection moved or trimmed the batch).
    for batch in batches:
        if batch.log_run_id is None:
            continue
        run = by_id.get(batch.log_run_id) or batch.log_run
        stale = batch.is_partial or (run.source != Run.SOURCE_AUTO and _overlap(run, batch) <= 0)
        if stale:
            batch.log_run = None
            batch.save(update_fields=["log_run", "updated_at"])

    loggable = [b for b in batches if not b.is_partial and not b.log_suppressed]
    finished = [b for b in loggable if not b.is_open]

    def taken() -> set:
        return set(
            DetectedBatch.objects.filter(machine_id=machine_id, log_run__isnull=False).values_list("log_run_id", flat=True)
        )

    # 2. One manual entry covering several batches.
    linked = taken()
    for run in runs:
        if run.pk in linked or run.source == Run.SOURCE_AUTO or not run.ended_at:
            continue
        covered = [
            batch for batch in finished
            if batch.log_run_id is None
            and _overlap(run, batch) >= iot_config.log_cover_ratio() * max(batch.duration_s, 1)
        ]
        if len(covered) > 1:
            _split(run, covered)

    # 3. One to one, largest overlap first. Running batches take part so an open entry goes to the right one.
    linked = taken()
    pairs = sorted(
        ((_overlap(run, batch), run.pk, batch.pk, run, batch)
         for run in runs if run.pk not in linked
         for batch in loggable if batch.log_run_id is None),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    used_runs: set = set()
    used_batches: set = set()
    for seconds, run_id, batch_id, run, batch in pairs:
        if seconds <= 0 or run_id in used_runs or batch_id in used_batches:
            continue
        batch.log_run = run
        batch.save(update_fields=["log_run", "updated_at"])
        used_runs.add(run_id)
        used_batches.add(batch_id)

    # 4 and 5. Machine times on linked entries; new entries for the rest.
    created = 0
    for batch in finished:
        if batch.log_run_id is None:
            _create_auto(batch)
            created += 1
        else:
            _apply_machine_times(Run.objects.get(pk=batch.log_run_id), batch)

    # 6. Entries left without a batch.
    linked = taken()
    has_data = list(
        DetectedBatch.objects.filter(machine_id=machine_id, start_at__gte=start, start_at__lt=end)
    )
    removed = 0
    for run in Run.objects.filter(machine_id=machine_id, started_at__gte=start, started_at__lt=end):
        if run.pk in linked or run.ended_at is None:
            continue
        if _untouched_auto(run):
            run.delete()
            removed += 1
            continue
        if run.source == Run.SOURCE_AUTO:
            flag = Run.FLAG_NO_MACHINE_RUN
        elif any(_overlap(run, batch) > 0 and batch.is_partial for batch in has_data):
            flag = ""  # the machine was running, its start just was not seen
        elif any(_overlap(run, batch) > 0 and batch.log_run_id for batch in has_data):
            flag = Run.FLAG_DUPLICATE
        else:
            flag = Run.FLAG_NO_MACHINE_RUN
        if run.flag != flag:
            run.flag = flag
            run.save(update_fields=["flag", "updated_at"])

    # The batch's compound follows its entry.
    for batch in batches:
        run = Run.objects.filter(pk=batch.log_run_id).first() if batch.log_run_id else None
        code = run.compound_code if run and run.compound_code != Run.UNASSIGNED else ""
        if batch.compound_code != code:
            batch.compound_code = code
            batch.save(update_fields=["compound_code", "updated_at"])
    return {"created": created, "removed": removed}

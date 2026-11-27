"""Run batch detection over stored IoT samples.

    python manage.py iot_detect_batches                              # continue from the last detected batch
    python manage.py iot_detect_batches --date 2026-11-27            # replay one Asia/Kolkata day
    python manage.py iot_detect_batches --date 2026-11-27 --dry-run  # list the runs without saving anything

Replays are safe to repeat: a run already saved as a batch is matched by its start time, not saved again.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from compounding import iot_batches
from compounding.models import IST_TIMEZONE


def _fmt(ts_ms):
    if ts_ms is None:
        return "running"
    return iot_batches.ms_to_datetime(ts_ms).astimezone(IST_TIMEZONE).strftime("%H:%M:%S")


class Command(BaseCommand):
    help = "Detect mixing-mill batches in the stored IoT samples."

    def add_arguments(self, parser):
        parser.add_argument("--machine", default="", help="Public machine id (default IOT_MACHINE_ID_DEFAULT)")
        parser.add_argument("--date", default="", help="Replay this Asia/Kolkata day (YYYY-MM-DD)")
        parser.add_argument("--dry-run", action="store_true", help="Only list the runs found")

    def handle(self, *args, **options):
        machine_id = (options["machine"] or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        if not machine_id:
            raise CommandError("Pass --machine or set IOT_MACHINE_ID_DEFAULT")
        start_ts = end_ts = None
        if options["date"]:
            try:
                day = date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"{options['date']!r} is not a YYYY-MM-DD date") from exc
            start = datetime.combine(day, datetime.min.time(), tzinfo=IST_TIMEZONE)
            start_ts = int(start.timestamp() * 1000)
            end_ts = int((start + timedelta(days=1)).timestamp() * 1000) - 1

        report = iot_batches.process_machine(
            machine_id, start_ts=start_ts, end_ts=end_ts, dry_run=options["dry_run"]
        )
        if not report:
            self.stdout.write("No runs found.")
            return
        self.stdout.write(f"{'start':>8}  {'end':>8}  {'min':>5}  {'kWh':>6}  {'avg A':>6}  {'peak A':>6}  batch")
        for entry in report:
            minutes = f"{entry['duration_s'] / 60:.1f}" if entry.get("duration_s") is not None else "-"
            energy = f"{entry['energy_kwh']:.2f}" if entry.get("energy_kwh") is not None else "-"
            avg = f"{entry['avg_current']:.1f}" if entry.get("avg_current") is not None else "-"
            peak = f"{entry['peak_current']:.1f}" if entry.get("peak_current") is not None else "-"
            batch = (
                f"#{entry['batch_id']} {entry['compound_code']} ({entry['source']}, {entry['status']})"
                if entry.get("batch_id")
                else ("(dry run)" if options["dry_run"] else "-")
            )
            self.stdout.write(
                f"{_fmt(entry['start_ts']):>8}  {_fmt(entry['end_ts']):>8}  {minutes:>5}  {energy:>6}  "
                f"{avg:>6}  {peak:>6}  {batch}"
            )

"""Re-run machine-state segmentation and batch detection over stored IoT samples.

    python manage.py iot_detect_batches                                  # today (and yesterday just after midnight)
    python manage.py iot_detect_batches --date 2026-11-27                # one Asia/Kolkata day
    python manage.py iot_detect_batches --since 2026-11-20 --until 2026-11-27

Safe to repeat. Run `iot_sync_samples` first when the samples are not stored yet.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from compounding import iot_segments
from compounding.models import IST_TIMEZONE, DetectedBatch


def _parse(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a YYYY-MM-DD date") from exc


class Command(BaseCommand):
    help = "Segment the mixing mill's day (off/idle/batch) and store the detected batches."

    def add_arguments(self, parser):
        parser.add_argument("--machine", default="", help="Public machine id (default IOT_MACHINE_ID_DEFAULT)")
        parser.add_argument("--date", default="", help="One Asia/Kolkata day (YYYY-MM-DD)")
        parser.add_argument("--since", default="", help="First day of a range (YYYY-MM-DD)")
        parser.add_argument("--until", default="", help="Last day of a range (default today)")

    def handle(self, *args, **options):
        machine_id = (options["machine"] or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        if not machine_id:
            raise CommandError("Pass --machine or set IOT_MACHINE_ID_DEFAULT")
        if options["date"]:
            days = [_parse(options["date"])]
        elif options["since"]:
            first = _parse(options["since"])
            last = _parse(options["until"]) if options["until"] else iot_segments.today_ist()
            if last < first:
                raise CommandError("--until is before --since")
            days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
        else:
            days = [iot_segments.today_ist()]

        for day in days:
            result = iot_segments.detect_day(machine_id, day)
            self.stdout.write(f"{day}: {result['segments']} segments, {result['batches']} batches")
            start_ts, end_ts = iot_segments.day_bounds_ms(day)
            batches = DetectedBatch.objects.filter(
                machine_id=machine_id,
                start_at__gte=iot_segments.ms_to_datetime(start_ts),
                start_at__lt=iot_segments.ms_to_datetime(end_ts),
            ).order_by("start_at")
            for batch in batches:
                start = batch.start_at.astimezone(IST_TIMEZONE).strftime("%H:%M")
                end = batch.end_at.astimezone(IST_TIMEZONE).strftime("%H:%M")
                energy = f"{batch.energy_kwh:.1f} kWh" if batch.energy_kwh is not None else "- kWh"
                peak = f"peak {batch.peak_current:.0f} A" if batch.peak_current is not None else ""
                state = " (running)" if batch.is_open else ""
                self.stdout.write(f"  {start}-{end}  {batch.duration_s / 60:5.1f} min  {energy:>9}  {peak}{state}")

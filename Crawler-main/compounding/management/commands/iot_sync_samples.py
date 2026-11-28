"""Copy IoT samples from the bridge into Django's sample store.

    python manage.py iot_sync_samples                      # everything new since the last stored sample
    python manage.py iot_sync_samples --date 2026-11-27    # backfill one Asia/Kolkata day
    python manage.py iot_sync_samples --since 2026-11-20   # backfill from a day up to now
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from compounding import iot_samples
from compounding.models import IST_TIMEZONE


def _day_start_ms(value: str) -> int:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"{value!r} is not a YYYY-MM-DD date") from exc
    return int(datetime.combine(day, datetime.min.time(), tzinfo=IST_TIMEZONE).timestamp() * 1000)


class Command(BaseCommand):
    help = "Copy IoT samples from the bridge into the database (incremental, or a backfill by day)."

    def add_arguments(self, parser):
        parser.add_argument("--machine", default="", help="Public machine id (default IOT_MACHINE_ID_DEFAULT)")
        parser.add_argument("--date", default="", help="Backfill this Asia/Kolkata day (YYYY-MM-DD)")
        parser.add_argument("--since", default="", help="Backfill from this day (YYYY-MM-DD) up to now")

    def handle(self, *args, **options):
        machine_id = (options["machine"] or getattr(settings, "IOT_MACHINE_ID_DEFAULT", "") or "").strip()
        if not machine_id:
            raise CommandError("Pass --machine or set IOT_MACHINE_ID_DEFAULT")
        if not (getattr(settings, "IOT_PORTAL_URL", "") or "").strip():
            raise CommandError("IOT_PORTAL_URL is not set, so there is no bridge to copy from")

        start_ts = end_ts = None
        if options["date"]:
            start_ts = _day_start_ms(options["date"])
            end_ts = start_ts + int(timedelta(days=1).total_seconds() * 1000) - 1
        elif options["since"]:
            start_ts = _day_start_ms(options["since"])

        try:
            result = iot_samples.sync_from_bridge(machine_id, start_ts=start_ts, end_ts=end_ts)
        except iot_samples.IoTSyncError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"{machine_id}: fetched {result['fetched']} samples, stored {result['stored']} new ones."
            )
        )

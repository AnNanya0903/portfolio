"""Give existing IoT batches a status and duration; batches shorter than MIN_BATCH_SECONDS become invalid.

Their stored operator grade (stock_quality) is left as it was: it was never a lab result, and lab_result
starts as "pending" for every batch.
"""
from django.conf import settings
from django.db import migrations


def _min_batch_seconds() -> int:
    try:
        return int(float(getattr(settings, "MIN_BATCH_SECONDS", 480) or 480))
    except (TypeError, ValueError):
        return 480


def backfill(apps, schema_editor):
    Run = apps.get_model("compounding", "CompoundingIoTRun")
    minimum = _min_batch_seconds()
    for run in Run.objects.all().iterator():
        if run.ended_at is None:
            run.status = "running"
            run.duration_s = None
            run.invalid_reason = ""
        else:
            duration = max(int((run.ended_at - run.started_at).total_seconds()), 0)
            run.duration_s = duration
            if duration < minimum:
                run.status = "invalid"
                run.invalid_reason = f"Shorter than {minimum // 60} min ({duration} s)"
            else:
                run.status = "completed"
                run.invalid_reason = ""
        run.save(update_fields=["status", "duration_s", "invalid_reason"])


class Migration(migrations.Migration):
    dependencies = [
        ("compounding", "0022_iot_batch_fields"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

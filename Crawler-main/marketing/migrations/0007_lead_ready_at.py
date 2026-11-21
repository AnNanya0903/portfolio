from django.db import migrations, models
from django.utils import timezone


def waiting_to_ready(apps, schema_editor):
    """The waiting period is gone: every lead that has no Ready time yet is Ready now.

    Safe to run more than once: leads that already have ready_at are left alone.
    """
    Lead = apps.get_model("marketing", "MarketingLead")
    Lead.objects.filter(ready_at__isnull=True).update(ready_at=timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ("marketing", "0006_lost_reason_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketinglead",
            name="ready_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(waiting_to_ready, migrations.RunPython.noop),
    ]

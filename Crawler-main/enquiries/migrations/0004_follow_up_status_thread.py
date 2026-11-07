from django.db import migrations, models


def mark_existing_as_sent(apps, schema_editor):
    # Rows created before this migration were only ever written after a successful send.
    FollowUpSent = apps.get_model("enquiries", "FollowUpSent")
    FollowUpSent.objects.all().update(status="sent")


class Migration(migrations.Migration):

    dependencies = [
        ("enquiries", "0003_add_follow_up_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="followupsent",
            name="status",
            field=models.CharField(
                choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="followupsent",
            name="gmail_thread_id",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="followupsent",
            name="error_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="followupsent",
            name="submission_token",
            field=models.CharField(blank=True, max_length=64, null=True, unique=True),
        ),
        migrations.RunPython(mark_existing_as_sent, migrations.RunPython.noop),
    ]

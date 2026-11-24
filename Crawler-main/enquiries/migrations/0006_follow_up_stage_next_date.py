from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("enquiries", "0005_enquiry_contact"),
    ]

    operations = [
        migrations.AddField(
            model_name="followupsent",
            name="stage",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="followupsent",
            name="next_follow_up_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kyc", "0015_kyc_link_requests"),
    ]

    operations = [
        migrations.AddField(
            model_name="kycdetails",
            name="extra_contacts",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

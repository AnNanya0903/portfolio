from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("enquiries", "0004_follow_up_status_thread"),
    ]

    operations = [
        migrations.CreateModel(
            name="EnquiryContact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enquiry_id", models.CharField(max_length=128, unique=True)),
                ("customer_email", models.EmailField(blank=True, max_length=254)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "enquiries_contact"},
        ),
    ]

"""Automatic customer codes (AVG-YY-NNNN).

Adds Customer.customer_code (unique), the per-year counter table, and gives every existing customer a code.
Existing customers are numbered from 1001 in order of when they were created (oldest first), separately for
each year they were created in, and each year's counter is set to continue after its last number.
The typed `code` field is not changed.
"""
from django.db import migrations, models
from zoneinfo import ZoneInfo

from django.utils import timezone

PREFIX = "AVG"
FIRST = 1001
IST = ZoneInfo("Asia/Kolkata")


def assign_existing_codes(apps, schema_editor):
    Customer = apps.get_model("kyc", "Customer")
    Counter = apps.get_model("kyc", "CustomerCodeCounter")

    by_year = {}
    for customer in Customer.objects.filter(customer_code__isnull=True).order_by("created_at", "pk"):
        year = timezone.localtime(customer.created_at, IST).year
        by_year.setdefault(year, []).append(customer)

    for year, customers in sorted(by_year.items()):
        number = FIRST - 1
        for customer in customers:
            number += 1
            customer.customer_code = f"{PREFIX}-{year % 100:02d}-{number}"
            customer.save(update_fields=["customer_code"])
        counter, _ = Counter.objects.get_or_create(year=year)
        counter.last_number = number
        counter.save(update_fields=["last_number"])


class Migration(migrations.Migration):

    dependencies = [
        ("kyc", "0010_remove_customer_handled_by"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomerCodeCounter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("year", models.PositiveSmallIntegerField(unique=True)),
                ("last_number", models.PositiveIntegerField(default=FIRST - 1)),
            ],
            options={"db_table": "kyc_customer_code_counter"},
        ),
        migrations.AddField(
            model_name="customer",
            name="customer_code",
            field=models.CharField(
                blank=True, editable=False, help_text="Generated automatically (AVG-YY-NNNN) when the customer is created.",
                max_length=20, null=True, unique=True, verbose_name="Customer code"),
        ),
        migrations.RunPython(assign_existing_codes, migrations.RunPython.noop),
    ]

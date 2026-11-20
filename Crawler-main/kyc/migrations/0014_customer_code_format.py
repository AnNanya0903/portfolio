"""Customer codes become AVGNNNNXX (for example AVG1001TA): one running number for every customer, and the two
letters are the first two letters of the company name.

- Customer.customer_number holds the running number (for sorting).
- Existing customers are numbered from 1001 in order of creation (oldest first) and get the new code.
- The counter becomes one row (pk=1) that continues after the highest number in use, and never goes back
  below an earlier value, so a number is not reused after a deletion.
"""
from django.db import migrations, models

PREFIX = "AVG"
FIRST = 1001


def _letters(name):
    import re

    rest = re.sub(r"^\s*(?:(?:m\s*/\s*s|messrs|the)\.?(?=\s|$)\s*)+", "", str(name or ""), count=1, flags=re.I)
    return re.sub(r"[^A-Za-z]", "", rest)[:2].upper().ljust(2, "X")


def renumber_customers(apps, schema_editor):
    Customer = apps.get_model("kyc", "Customer")
    Counter = apps.get_model("kyc", "CustomerCodeCounter")

    number = FIRST - 1
    for customer in Customer.objects.order_by("created_at", "pk"):
        number += 1
        customer.customer_number = number
        customer.customer_code = f"{PREFIX}{number}{_letters(customer.name)}"
        customer.save(update_fields=["customer_number", "customer_code"])

    # Keep the highest value any earlier counter reached (a deleted customer's number is never reused).
    highest = max([number] + [c.last_number for c in Counter.objects.all()])
    rows = list(Counter.objects.order_by("pk"))
    if rows:
        keep = rows[0]
        keep.last_number = highest
        keep.save(update_fields=["last_number"])
        Counter.objects.exclude(pk=keep.pk).delete()
    else:
        Counter.objects.create(year=0, last_number=highest)


def make_single_counter(apps, schema_editor):
    Counter = apps.get_model("kyc", "CustomerCodeCounter")
    row = Counter.objects.order_by("pk").first()
    value = row.last_number if row else FIRST - 1
    Counter.objects.all().delete()
    Counter.objects.create(pk=1, last_number=value)


class Migration(migrations.Migration):

    dependencies = [
        ("kyc", "0013_email_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="customer_number",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="customer",
            name="customer_code",
            field=models.CharField(blank=True, editable=False, help_text="Generated automatically (AVGNNNNXX) when the customer is created.",
                                   max_length=20, null=True, unique=True, verbose_name="Customer code"),
        ),
        migrations.RunPython(renumber_customers, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="customercodecounter",
            name="year",
        ),
        migrations.RunPython(make_single_counter, migrations.RunPython.noop),
    ]

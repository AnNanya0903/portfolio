# Migration to allow multiple comma-separated emails on a contact.
# EmailField and CharField(max_length=254) share the same database column
# (varchar(254)), so this keeps the migration state in sync with the model.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kyc", "0005_customer_delivery_type_customer_msme_applicable_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customercontact",
            name="email",
            field=models.CharField(blank=True, default="", max_length=254),
        ),
    ]

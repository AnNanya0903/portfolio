from django.db import migrations, models

SEED_NAMES = ("Sales", "Marketing", "Accounts", "Technical/Design", "Top Management")


def give_every_customer_every_department(apps, schema_editor):
    """Each customer keeps the department it was handled by and gets all the other active ones too,
    so every existing customer shows under every team."""
    Customer = apps.get_model("kyc", "Customer")
    Department = apps.get_model("kyc", "Department")
    if not Customer.objects.exists():
        return
    if not Department.objects.exists():  # the departments were never seeded: create the standard five
        for order, name in enumerate(SEED_NAMES, start=1):
            Department.objects.get_or_create(name=name, defaults={"sort_order": order})
    everyone = list(Department.objects.filter(is_active=True))
    for customer in Customer.objects.all().iterator():
        keep = [customer.handled_by_id] if customer.handled_by_id else []
        customer.departments.add(*everyone, *keep)


def restore_handled_by(apps, schema_editor):
    Customer = apps.get_model("kyc", "Customer")
    for customer in Customer.objects.all().iterator():
        first = customer.departments.order_by("sort_order", "name").first()
        if first:
            Customer.objects.filter(pk=customer.pk).update(handled_by=first)


class Migration(migrations.Migration):
    # Data only, in its own migration so PostgreSQL has no pending trigger events when the old column is dropped.

    dependencies = [
        ('kyc', '0008_customer_departments'),
    ]

    operations = [
        migrations.RunPython(give_every_customer_every_department, restore_handled_by),
    ]

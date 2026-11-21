from django.db import migrations

PRIMARY = ("sagar@avigiri.com", "yathin@avigiri.com")


def set_technical_primaries(apps, schema_editor):
    """Technical/Design: Sagar and Yathin are the primary contacts. Does nothing where the department is not set up."""
    Department = apps.get_model("kyc", "Department")
    DepartmentContact = apps.get_model("kyc", "DepartmentContact")
    department = Department.objects.filter(name="Technical/Design").first()
    if department is None:
        return
    for email in PRIMARY:
        DepartmentContact.objects.get_or_create(department=department, email=email)
    department.contacts.update(is_primary=False)
    department.contacts.filter(email__in=PRIMARY).update(is_primary=True)


class Migration(migrations.Migration):

    dependencies = [
        ("kyc", "0016_kycdetails_extra_contacts"),
    ]

    operations = [
        migrations.RunPython(set_technical_primaries, migrations.RunPython.noop),
    ]

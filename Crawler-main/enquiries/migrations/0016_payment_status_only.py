# Payment Status becomes the only payment value shown. Nothing is dropped: the amount, due date, paid-on date and
# reference stay in their columns, they are just no longer shown or asked for.

from django.db import migrations, models


def set_payment_status(apps, schema_editor):
    """Records with no status become Payment Pending; records that have a paid-on date are Paid.

    Orders that are Converted already show as Paid whatever is stored here (the enquiry service owns that
    status), so they need nothing.
    """
    Contact = apps.get_model("enquiries", "EnquiryContact")
    Contact.objects.filter(payment_status="", payment_paid_on__isnull=False).update(payment_status="paid")
    Contact.objects.filter(payment_status="").update(payment_status="pending")


class Migration(migrations.Migration):

    dependencies = [
        ('enquiries', '0015_followups_attachments'),
    ]

    operations = [
        migrations.AlterField(
            model_name='enquirycontact',
            name='payment_status',
            field=models.CharField(choices=[('pending', 'Payment Pending'), ('partial', 'Partially Paid'), ('credit', 'Credit Period'), ('paid', 'Paid')], default='pending', max_length=16),
        ),
        migrations.RunPython(set_payment_status, migrations.RunPython.noop),
    ]

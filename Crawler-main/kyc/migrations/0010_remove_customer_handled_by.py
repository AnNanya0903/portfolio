from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('kyc', '0009_customers_in_every_department'),
    ]

    operations = [
        migrations.RemoveField(model_name='customer', name='handled_by'),
    ]

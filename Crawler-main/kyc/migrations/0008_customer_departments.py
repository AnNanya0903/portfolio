from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('kyc', '0007_departments_audit'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='departments',
            field=models.ManyToManyField(db_table='kyc_customer_departments', related_name='customers', to='kyc.department', verbose_name='Team'),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('enquiries', '0019_work_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='enquirycontact',
            name='follow_up_share_url',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='enquirycontact',
            name='follow_up_share_password',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]

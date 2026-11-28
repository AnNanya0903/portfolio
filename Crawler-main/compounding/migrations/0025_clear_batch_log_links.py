"""Clear batch-to-log links before they become one-to-one (one entry could cover several batches). The
auto-log recreates them."""
from django.db import migrations


def clear(apps, schema_editor):
    apps.get_model("compounding", "DetectedBatch").objects.update(log_run=None)


class Migration(migrations.Migration):
    dependencies = [("compounding", "0024_standards_and_comparison")]
    operations = [migrations.RunPython(clear, migrations.RunPython.noop)]

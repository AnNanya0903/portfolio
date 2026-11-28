"""Quotations used to keep whatever was typed in the enquiry box (often the customer's "AG-QR-..." reference).
Only real ENQ-NNNN numbers stay in enquiry_id; any other text moves to enquiry_reference, so nothing is lost.
The tracker lines of those quotations stop pointing at the made-up enquiry. Linking them to their real enquiry is
done afterwards, with review (manage.py suggest_quotation_enquiries)."""
import re

from django.db import migrations

ENQ_RE = re.compile(r"^ENQ-\d+$", re.IGNORECASE)


def forwards(apps, schema_editor):
    Quotation = apps.get_model("quotations", "Quotation")
    QuotationEvent = apps.get_model("quotations", "QuotationEvent")
    for quotation in Quotation.objects.exclude(enquiry_id=""):
        value = " ".join((quotation.enquiry_id or "").split())
        if ENQ_RE.match(value):
            if value != value.upper():
                quotation.enquiry_id = value.upper()
                quotation.save(update_fields=["enquiry_id"])
                QuotationEvent.objects.filter(quotation=quotation).update(enquiry_id=value.upper())
            continue
        reference = quotation.enquiry_reference.strip()
        if value and value.lower() not in reference.lower():
            reference = f"{reference} {value}".strip() if reference else value
        quotation.enquiry_reference = reference[:120]
        quotation.enquiry_id = ""
        quotation.save(update_fields=["enquiry_id", "enquiry_reference"])
        QuotationEvent.objects.filter(quotation=quotation).update(enquiry_id="")


def backwards(apps, schema_editor):
    Quotation = apps.get_model("quotations", "Quotation")
    for quotation in Quotation.objects.filter(enquiry_id="").exclude(enquiry_reference=""):
        quotation.enquiry_id = quotation.enquiry_reference[:64]
        quotation.save(update_fields=["enquiry_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("quotations", "0003_enquiry_link"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]

"""Suggest the enquiry (ENQ-NNNN) of every quotation that has none, for review. Never links anything.

    python manage.py suggest_quotation_enquiries

Match: the same customer (name, or email domain) AND the same subject once Re:/Fw:/Fwd:, AG-QR/AG-RQ/AG-QF codes,
dates and punctuation are removed (exact, or one contains the other); among several, the enquiry received closest
to the quotation's date. The suggestions are saved for review on /quotations/enquiry-suggestions/, where an
administrator confirms each one or picks the right enquiry. Safe to run again.
"""
from django.core.management.base import BaseCommand, CommandError

from quotations import enquiry_link


class Command(BaseCommand):
    help = "Suggest an enquiry for each quotation without one (review list; nothing is linked automatically)."

    def handle(self, *args, **options):
        try:
            enquiries = enquiry_link.all_enquiries(fresh=True)
        except enquiry_link.EnquiryUnavailable as exc:
            raise CommandError(str(exc)) from exc
        rows = enquiry_link.refresh_suggestions(enquiries)
        if not rows:
            self.stdout.write("Every quotation is linked to an enquiry. Nothing to suggest.")
            return
        self.stdout.write(f"{'QT':<10} {'Suggested ENQ':<14} {'Confidence':<10} {'Status':<10} Reason")
        for row in rows:
            self.stdout.write(f"{row.quotation.number:<10} {row.enquiry_id or '-':<14} {row.confidence or '-':<10} "
                              f"{row.status:<10} {row.reason}")
        found = sum(1 for r in rows if r.enquiry_id)
        self.stdout.write(self.style.SUCCESS(
            f"{len(rows)} quotation(s) without an enquiry, {found} with a suggestion. Nothing was linked: review "
            "them on /quotations/enquiry-suggestions/."))

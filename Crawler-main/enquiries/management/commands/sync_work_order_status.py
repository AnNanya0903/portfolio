"""Set the status to Converted for enquiries already marked "Converted to Work Order" whose status was not changed
(they were marked before the button changed the status). Shows what it would do unless --apply is given."""
from django.core.management.base import BaseCommand

from enquiries import negotiation, services
from enquiries.models import WorkOrder
from enquiries.views import CONVERTED_STATUS, _get_api_root


class Command(BaseCommand):
    help = "Make the status Converted for enquiries marked as Work Order. Add --apply to save it."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the change (otherwise only list).")

    def handle(self, *args, **options):
        api_root = _get_api_root()
        changed = skipped = failed = 0
        for order in WorkOrder.objects.select_related("converted_by").order_by("converted_at"):
            try:
                enquiry = services.fetch_enquiry(api_root, order.enquiry_id)
                old = str(enquiry.get("status") or "").strip().lower()
                if old == CONVERTED_STATUS:
                    skipped += 1
                    continue
                self.stdout.write(f"{order.enquiry_id}: {old or '-'} -> {CONVERTED_STATUS}")
                if options["apply"]:
                    services.update_enquiry(api_root, order.enquiry_id, {"status": CONVERTED_STATUS})
                    negotiation.log_status_change(order.converted_by, order.enquiry_id, old, CONVERTED_STATUS,
                                                  note="Converted to Work Order")
                changed += 1
            except services.EnquiryServiceError as exc:
                failed += 1
                self.stderr.write(f"{order.enquiry_id}: {exc}")
        verb = "changed" if options["apply"] else "would change"
        self.stdout.write(f"{verb} {changed}, already converted {skipped}, failed {failed}.")

"""An enquiry sales rejected with the reason "No response" (after following up in person) is a No response lead."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from enquiries.models import EnquiryStatusLog
from marketing import lost

SETTINGS = dict(FOLLOW_UP_MAX_COUNT=4, FOLLOW_UP_CLOSE_AFTER_DAYS=7,
                STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


def _rejected(enquiry_id):
    return {"enquiry_id": enquiry_id, "status": "rejected", "customer_name": f"Customer {enquiry_id}",
            "subject": "Gaskets", "customer_reference": "", "remarks": "",
            "received_at_dt": timezone.now() - timedelta(days=30)}


def _log(enquiry_id, reason, days_ago=1, new="rejected"):
    EnquiryStatusLog.objects.create(enquiry_id=enquiry_id, old_status="open", new_status=new, lost_reason=reason,
                                    created_at=timezone.now() - timedelta(days=days_ago))


@override_settings(**SETTINGS)
class SalesNoResponseTests(TestCase):
    def kinds(self, *ids):
        return {r["enquiry_id"]: (r["kind"], r["source_badge"]) for r in lost.build_lost_leads([_rejected(i) for i in ids])}

    def test_rejected_for_no_response_counts_as_no_response(self):
        _log("E1", "No response")
        _log("E2", "Price too high")
        rows = lost.build_lost_leads([_rejected("E1"), _rejected("E2"), _rejected("E3")])
        by_id = {r["enquiry_id"]: r for r in rows}
        self.assertEqual((by_id["E1"]["kind"], by_id["E1"]["source_badge"], by_id["E1"]["reason_label"]),
                         (lost.NO_RESPONSE, lost.SALES_NO_RESPONSE_BADGE, "Never replied"))
        self.assertEqual(by_id["E2"]["kind"], lost.REJECTED)
        self.assertEqual(by_id["E3"]["kind"], lost.REJECTED)  # no reason logged
        summary = lost.summarise(rows)
        self.assertEqual((summary["rejected"], summary["no_response"]), (2, 1))
        only, _ = lost.filter_leads(rows, {"kind": lost.NO_RESPONSE})
        self.assertEqual([r["enquiry_id"] for r in only], ["E1"])

    def test_the_latest_rejection_reason_wins(self):
        _log("E1", "No response", days_ago=5)
        _log("E1", "Went with competitor", days_ago=1)
        _log("E2", "Price too high", days_ago=5)
        _log("E2", "No response", days_ago=1)
        self.assertEqual(self.kinds("E1", "E2"), {"E1": (lost.REJECTED, "Rejected"),
                                                  "E2": (lost.NO_RESPONSE, lost.SALES_NO_RESPONSE_BADGE)})

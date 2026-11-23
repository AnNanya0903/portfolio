from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingCRINRequest,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingStockEntry,
)
from compounding.views import _mb_availability
from dashboard.models import UserServiceAccess

PLAN_DATE = "2026-03-02"


class DeleteCRINTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="delete-user",
            email="delete@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "5000", "1000"]],
        )

    def _crin(self, number="CRIN-2026-101", quantity=1200, **extra):
        return CompoundingCRIN.objects.create(
            plan=self.plan,
            crin_number=number,
            compound_code="7020",
            quantity=quantity,
            created_by=self.user,
            **extra,
        )

    def _delete(self, crin, **data):
        return self.client.post(reverse("compounding:delete_crin", args=[crin.id]), data=data, follow=True)

    def _messages(self, response):
        return " ".join(str(message) for message in response.context["messages"])

    def test_untouched_crin_is_deleted_and_demand_clears(self):
        crin = self._crin()
        self.assertEqual(_mb_availability(self.plan, "7020")["open_fb"], 10)

        response = self._delete(crin)

        self.assertFalse(CompoundingCRIN.objects.filter(id=crin.id).exists())
        self.assertEqual(_mb_availability(self.plan, "7020")["open_fb"], 0)
        self.assertIn("Deleted CRIN-2026-101", self._messages(response))

    def test_delete_button_shows_for_untouched_crin_on_both_screens(self):
        crin = self._crin()
        url = reverse("compounding:delete_crin", args=[crin.id])

        for name in ("planning", "fb_planning"):
            self.assertContains(self.client.get(reverse(f"compounding:{name}"), {"date": PLAN_DATE}), url)

    def _send(self, crin):
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )

    def test_unstarted_crin_on_board_is_deleted_and_leaves_the_board(self):
        crin = self._crin()
        self._send(crin)
        line_id = crin.planned_lines.get().id
        url = reverse("compounding:delete_crin", args=[crin.id])
        page = self.client.get(reverse("compounding:planning"), {"date": PLAN_DATE})
        self.assertContains(page, url)
        self.assertContains(page, "removed from the Compounding Board")

        response = self._delete(crin)

        self.assertFalse(CompoundingCRIN.objects.filter(id=crin.id).exists())
        self.assertFalse(CompoundingLine.objects.filter(id=line_id).exists())
        self.assertEqual(_mb_availability(self.plan, "7020")["reserved"], Decimal("0"))
        self.assertIn("removed it from the board", self._messages(response))

    def test_crin_with_saved_actuals_cannot_be_deleted(self):
        crin = self._crin()
        self._send(crin)
        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "10"},
        )
        url = reverse("compounding:delete_crin", args=[crin.id])

        self.assertNotContains(self.client.get(reverse("compounding:planning"), {"date": PLAN_DATE}), url)
        response = self._delete(crin)

        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())
        self.assertTrue(CompoundingLine.objects.filter(id=line.id).exists())
        self.assertIn("cannot be deleted", self._messages(response))

    def test_saved_zero_actuals_still_block_delete(self):
        crin = self._crin()
        self._send(crin)
        line = crin.planned_lines.get()
        CompoundingLine.objects.filter(id=line.id).update(actuals_saved=True)

        self._delete(crin)

        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_separately_sent_mb_stays_on_the_board(self):
        crin = self._crin()
        self._send(crin)
        line = crin.planned_lines.get()
        CompoundingLine.objects.filter(id=line.id).update(shift2_mb=4)

        response = self._delete(crin)

        line.refresh_from_db()
        self.assertIsNone(line.crin_request)
        self.assertIsNone(line.shift1_fb)
        self.assertEqual(line.shift2_mb, 4)
        self.assertIn("still on the board", self._messages(response))

    def test_crin_own_mb_is_removed_with_it(self):
        crin = self._crin(mb_planning=3)
        self._send(crin)
        line = crin.planned_lines.get()
        CompoundingLine.objects.filter(id=line.id).update(shift2_mb=4)

        self._delete(crin)

        line.refresh_from_db()
        self.assertIsNone(line.shift1_mb)
        self.assertEqual(line.shift2_mb, 4)

    def test_crin_with_stock_movement_cannot_be_deleted(self):
        crin = self._crin()
        CompoundingStockEntry.objects.create(
            compound_key="7020",
            compound_code="7020",
            stock_type=CompoundingStockEntry.STOCK_FB,
            entry_type=CompoundingStockEntry.ENTRY_FB_ISSUED,
            quantity=Decimal("-10"),
            crin=crin,
        )

        self._delete(crin)

        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_crin_returned_from_board_cannot_be_deleted(self):
        crin = self._crin(quantity=240)
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )
        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "1"},
        )

        self._delete(crin)

        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_closed_crin_cannot_be_deleted(self):
        crin = self._crin(status=CompoundingCRIN.STATUS_CLOSED)

        self._delete(crin)

        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_deleting_one_compound_keeps_the_rest_of_the_request(self):
        header = CompoundingCRINRequest.objects.create(plan=self.plan, request_number="CRIN-2026-200", created_by=self.user)
        first = self._crin("CRIN-2026-200-1", request=header)
        second = self._crin("CRIN-2026-200-2", request=header)

        self._delete(first)

        self.assertTrue(CompoundingCRINRequest.objects.filter(id=header.id).exists())
        self._delete(second)
        self.assertFalse(CompoundingCRINRequest.objects.filter(id=header.id).exists())

    def test_mb_already_on_board_is_kept_and_reported(self):
        crin = self._crin()
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_mb=2)

        response = self._delete(crin)

        self.assertTrue(CompoundingLine.objects.filter(id=line.id).exists())
        self.assertIn("still on the board", self._messages(response))

    def test_delete_needs_planning_access(self):
        crin = self._crin()
        for services in (["compounding_production"], ["compounding_board"]):
            access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
            access.restrict = True
            access.allowed_services = services
            access.save(update_fields=["restrict", "allowed_services", "updated_at"])

            response = self.client.post(reverse("compounding:delete_crin", args=[crin.id]))

            self.assertEqual(response.status_code, 302)
            self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_delete_rejects_get(self):
        crin = self._crin()

        response = self.client.get(reverse("compounding:delete_crin", args=[crin.id]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(CompoundingCRIN.objects.filter(id=crin.id).exists())

    def test_return_to_fb_page(self):
        crin = self._crin()

        response = self.client.post(reverse("compounding:delete_crin", args=[crin.id]), data={"return_to": "fb"})

        self.assertIn("/compounding/planning/fb/", response["Location"])

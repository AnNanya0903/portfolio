from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import CompoundingCRIN, CompoundingLine, CompoundingMasterSheet, CompoundingPlan
from compounding.views import _mb_availability


PLAN_DATE = "2026-02-03"


class MBAvailabilityTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="mb-availability",
            email="mb-availability@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)

    def _sheet(self, *, mb_stock: str, code: str = "7020", mb_weight: str = "100", fb_weight: str = "120") -> None:
        CompoundingMasterSheet.objects.update_or_create(
            plan=self.plan,
            defaults={
                "created_by": self.user,
                "headers": [
                    "Compound Code",
                    "MB Weight",
                    "Chemicals Weight",
                    "MB & FB Total Weight",
                    "MB Correct Stock Lev",
                    "FB Correct Stock Lev",
                ],
                "rows": [[code, mb_weight, "20", fb_weight, mb_stock, "0"]],
            },
        )

    def _crin(self, number: str, quantity: int, code: str = "7020", **extra) -> CompoundingCRIN:
        return CompoundingCRIN.objects.create(
            plan=self.plan,
            crin_number=number,
            compound_code=code,
            quantity=quantity,
            created_by=self.user,
            **extra,
        )

    def _send(self, crin: CompoundingCRIN):
        return self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
            follow=True,
        )

    def _mb_row(self):
        response = self.client.get(reverse("compounding:mb_planning"), {"date": PLAN_DATE, "compound": "7020"})
        self.assertEqual(response.status_code, 200)
        return response.context["rows"][0]

    def test_mb_required_comes_from_fb_batches_not_crin_quantity(self):
        self._sheet(mb_stock="2800")
        self._crin("CRIN-1", 3000)  # 25 FB batches x 100 MB = 2500

        availability = _mb_availability(self.plan, "7020")

        self.assertEqual(availability["open_fb"], 25)
        self.assertEqual(availability["required"], Decimal("2500"))
        self.assertEqual(availability["shortage"], Decimal("0"))
        self.assertEqual(availability["suggested_batches"], 0)
        row = self._mb_row()
        self.assertEqual(row["planning_quantity"], 0)
        self.assertEqual(row["mb_batches"], 0)

    def test_shortage_suggests_only_the_missing_mb(self):
        self._sheet(mb_stock="2300")
        self._crin("CRIN-1", 3000)  # needs 2500, short by 200

        row = self._mb_row()

        self.assertTrue(row["availability"]["has_shortage"])
        self.assertEqual(row["availability"]["shortage"], "200")
        self.assertEqual(row["planning_quantity"], 200)
        self.assertEqual(row["mb_batches"], 2)

    def test_part_batch_shortage_rounds_up_to_one_full_batch(self):
        self._sheet(mb_stock="2450")
        self._crin("CRIN-1", 3000)

        row = self._mb_row()

        self.assertEqual(row["availability"]["suggested_batches"], 1)
        self.assertEqual(row["planning_quantity"], 50)
        self.assertEqual(row["mb_batches"], 1)

    def test_mb_already_on_board_reduces_shortage(self):
        self._sheet(mb_stock="2300")
        self._crin("CRIN-1", 3000)
        CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_mb=1)

        availability = _mb_availability(self.plan, "7020")

        self.assertEqual(availability["incoming"], Decimal("100"))
        self.assertEqual(availability["shortage"], Decimal("100"))
        self.assertEqual(availability["suggested_batches"], 1)

    def test_blank_mb_send_uses_shortage(self):
        self._sheet(mb_stock="2300")
        self._crin("CRIN-1", 3000)

        self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={"plan_date": PLAN_DATE, "compound_code": "7020", "shift": "1"},
        )

        line = CompoundingLine.objects.get(plan=self.plan, compound_code="7020")
        self.assertEqual(line.shift1_mb, 2)

    def test_same_mb_cannot_be_reserved_by_two_crins(self):
        self._sheet(mb_stock="3000")
        first = self._crin("CRIN-1", 3000)  # uses 2500
        second = self._crin("CRIN-2", 1200)  # needs 1000, only 500 free

        self._send(first)
        response = self._send(second)

        self.assertEqual(first.planned_lines.count(), 1)
        self.assertEqual(second.planned_lines.count(), 0)
        text = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("already reserved", text)
        self.assertIn("Create MB first", text)

    def test_second_crin_is_allowed_when_free_stock_covers_it(self):
        self._sheet(mb_stock="3500")
        first = self._crin("CRIN-1", 3000)
        second = self._crin("CRIN-2", 1200)

        self._send(first)
        self._send(second)

        self.assertEqual(second.planned_lines.count(), 1)
        self.assertEqual(_mb_availability(self.plan, "7020")["free"], Decimal("0"))

    def test_saving_actuals_releases_reservation_into_stock_usage(self):
        self._sheet(mb_stock="3000")
        crin = self._crin("CRIN-1", 1200)  # 10 FB, 1000 MB
        self._send(crin)
        self.assertEqual(_mb_availability(self.plan, "7020")["reserved"], Decimal("1000"))

        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "10"},
        )

        availability = _mb_availability(self.plan, "7020")
        self.assertEqual(availability["reserved"], Decimal("0"))
        self.assertEqual(availability["mb_stock"], Decimal("2000"))
        self.assertEqual(availability["free"], Decimal("2000"))

    def test_returned_crin_with_no_fb_left_does_not_create_fb_demand(self):
        self._sheet(mb_stock="3000")
        crin = self._crin("CRIN-1", 240, mb_planning=5)  # 2 FB, 5 MB
        self._send(crin)
        line = crin.planned_lines.get()

        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "2"},
        )

        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_OPEN)
        self.assertEqual(crin.fb_planning, 0)
        self.assertEqual(_mb_availability(self.plan, "7020")["open_fb"], 0)

    def test_fb_page_shows_mb_needed_and_free(self):
        self._sheet(mb_stock="400")
        self._crin("CRIN-1", 1200)

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        row = response.context["crins"][0]
        self.assertEqual(row["mb_needed"], "1000")
        self.assertEqual(row["mb_free"], "400")
        self.assertFalse(row["mb_ready"])
        self.assertContains(response, "Plan MB")

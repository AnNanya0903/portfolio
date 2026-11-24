from decimal import Decimal
from typing import Optional

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMBPlan,
    CompoundingMasterSheet,
    CompoundingPlan,
)


class CompoundingCRINFlowTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="crin-user",
            email="crin@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _seed_master_sheet(
        self,
        *,
        plan_date: str,
        compound_code: str = "AG-FRC-70",
        mb_weight: str = "4",
        chemical_weight: str = "0",
        fb_total_weight: Optional[str] = None,
        msl: str = "6",
        mb_stock: str = "100",
        fb_stock: str = "10",
    ) -> None:
        plan, _created = CompoundingPlan.objects.get_or_create(
            plan_date=plan_date,
            defaults={"cjc_number": 500, "created_by": self.user},
        )
        mb_dec = Decimal(str(mb_weight))
        chem_dec = Decimal(str(chemical_weight))
        total_dec = Decimal(str(fb_total_weight)) if fb_total_weight is not None else (mb_dec + chem_dec)
        CompoundingMasterSheet.objects.update_or_create(
            plan=plan,
            defaults={
                "created_by": self.user,
                "headers": [
                    "Compound Code",
                    "MB Weight",
                    "Chemicals Weight",
                    "MB & FB Total Weight",
                    "MB Min Stock Lavel",
                    "MB Correct Stock Lev",
                    "FB Correct Stock Lev",
                ],
                "rows": [
                    [
                        compound_code,
                        str(mb_dec),
                        str(chem_dec),
                        str(total_dec),
                        str(msl),
                        str(mb_stock),
                        str(fb_stock),
                    ]
                ],
            },
        )

    def _raise_crin(self, *, plan_date: str = "2026-02-03", compound_code: str = "AG-FRC-70", quantity: str = "10", mb_planning: str = "", fb_planning: str = ""):
        payload = {
            "plan_date": plan_date,
            "compound_code": compound_code,
            "quantity": quantity,
        }
        if mb_planning != "":
            payload["mb_planning"] = mb_planning
        if fb_planning != "":
            payload["fb_planning"] = fb_planning
        return self.client.post(reverse("compounding:create_crin"), data=payload)

    def test_fb_planning_prefers_total_weight_column(self):
        # MB+chemical would be 5, but total column is 8 and should be used.
        self._seed_master_sheet(
            plan_date="2026-02-03",
            mb_weight="4",
            chemical_weight="1",
            fb_total_weight="8",
        )
        self._raise_crin(quantity="100")
        crin = CompoundingCRIN.objects.first()

        response = self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        crin.refresh_from_db()
        # ceil(100/8) == 13; this verifies MB+chemical (5) was not used.
        self.assertEqual(crin.fb_batch_size, Decimal("8"))
        self.assertEqual(crin.fb_planning, 13)

    def test_raise_crin_creates_request(self):
        self._seed_master_sheet(plan_date="2026-02-03")
        response = self._raise_crin(quantity="10", mb_planning="2", fb_planning="1")
        self.assertEqual(response.status_code, 302)
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        self.assertEqual(plan.crins.count(), 1)
        crin = plan.crins.first()
        self.assertTrue(crin.crin_number.startswith("CRIN-2026-"))
        self.assertEqual(crin.compound_code, "AG-FRC-70")
        self.assertEqual(crin.quantity, 10)
        self.assertEqual(crin.mb_planning, 2)
        self.assertEqual(crin.fb_planning, 1)

    def test_raise_crin_normalizes_to_master_sheet_compound_code(self):
        self._seed_master_sheet(plan_date="2026-02-03", compound_code="AG-NF 60")
        response = self._raise_crin(compound_code="AG-NF-60", quantity="10")
        self.assertEqual(response.status_code, 302)
        crin = CompoundingCRIN.objects.get(plan__plan_date="2026-02-03")
        self.assertEqual(crin.compound_code, "AG-NF 60")

    def test_apply_crin_adds_plan_line(self):
        self._seed_master_sheet(plan_date="2026-02-03")
        self._raise_crin(quantity="10", mb_planning="3", fb_planning="0")
        crin = CompoundingCRIN.objects.first()
        response = self.client.post(
            reverse("compounding:apply_crin", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        crin.refresh_from_db()
        self.assertEqual(crin.planned_lines.count(), 1)
        line = crin.planned_lines.first()
        self.assertEqual(line.compound_code, "AG-FRC-70")
        self.assertEqual(line.shift1_mb, 3)
        self.assertEqual(line.shift1_fb, 0)

    def test_fb_planning_computes_batches_and_updates_board(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        response = self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        crin.refresh_from_db()
        # ceil(10/4) == 3
        self.assertEqual(crin.fb_planning, 3)
        self.assertEqual(crin.fb_batch_size, Decimal("4"))
        # Planning updates the CRIN only; it should not push to the board until explicitly sent.
        self.assertEqual(crin.planned_lines.count(), 0)

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        crin.refresh_from_db()
        self.assertEqual(crin.planned_lines.count(), 1)
        self.assertEqual(crin.planned_lines.first().shift1_fb, 3)

    def test_send_to_board_auto_saves_fb_planning_when_missing(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()
        self.assertIsNone(crin.fb_planning)
        self.assertIsNone(crin.fb_batch_size)

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        crin.refresh_from_db()
        self.assertEqual(crin.fb_batch_size, Decimal("4"))
        self.assertEqual(crin.fb_planning, 3)
        self.assertEqual(crin.planned_lines.count(), 1)
        self.assertEqual(crin.planned_lines.first().shift1_fb, 3)

    def test_send_to_board_blocks_fb_when_mb_stock_is_insufficient(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0", mb_stock="8")
        self._raise_crin(quantity="12")
        crin = CompoundingCRIN.objects.first()

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=True,
        )

        crin.refresh_from_db()
        self.assertEqual(crin.fb_planning, 3)
        self.assertEqual(crin.planned_lines.count(), 0)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("Create MB first" in message for message in messages))

    def test_set_actuals_returns_remaining_to_same_crin_without_new_crin(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="1")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        crin.refresh_from_db()
        self.assertEqual(crin.fb_planning, 2)

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        crin.refresh_from_db()
        line = crin.planned_lines.first()
        self.assertIsNotNone(line)
        self.assertEqual(line.shift1_fb, 2)
        self.assertEqual(CompoundingCRIN.objects.filter(plan__plan_date="2026-02-03").count(), 1)

        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "0",
                "actual_fb": "1",
            },
            follow=False,
        )

        line.refresh_from_db()
        crin.refresh_from_db()
        self.assertEqual(CompoundingCRIN.objects.filter(plan__plan_date="2026-02-03").count(), 1)
        self.assertEqual(crin.crin_number, "CRIN-2026-100")
        self.assertEqual(crin.fb_planning, 1)
        self.assertEqual(crin.quantity, 5)
        self.assertIsNone(crin.mb_planning)
        self.assertEqual(line.returned_to_planning_fb, 1)
        self.assertIsNone(line.crin_request)

        response = self.client.get(reverse("compounding:fb_planning"), {"date": "2026-02-03"})
        self.assertEqual(response.status_code, 200)
        fb_row = next(item for item in response.context["crins"] if item["crin_number"] == "CRIN-2026-100")
        self.assertFalse(fb_row["has_line"])
        self.assertEqual(fb_row["fb_planning"], 1)

    def test_set_actuals_complete_crin_is_not_active_on_board(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        line = crin.planned_lines.first()
        self.assertIsNotNone(line)

        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "0",
                "actual_fb": "3",
            },
            follow=False,
        )

        crin.refresh_from_db()
        # Making the batches does not close it; the CRIN closes when FB is fully issued.
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_OPEN)

        board_response = self.client.get(reverse("compounding:display"), {"date": "2026-02-03"})
        self.assertEqual(board_response.status_code, 200)
        board_compounds = [row["compound_code"] for row in board_response.context["lines"]]
        self.assertNotIn("AG-FRC-70", board_compounds)

        fb_response = self.client.get(reverse("compounding:fb_planning"), {"date": "2026-02-03"})
        self.assertEqual(fb_response.status_code, 200)
        fb_row = next(item for item in fb_response.context["crins"] if item["crin_number"] == crin.crin_number)
        self.assertFalse(fb_row["has_line"])
        self.assertTrue(fb_row["has_done_line"])

    def test_set_actuals_returns_to_same_crin_and_same_mb_plan(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            compound_code="AG-FRC-70",
            mb_weight="4",
            chemical_weight="1",
            fb_total_weight="5",
            mb_stock="20",
            fb_stock="10",
        )
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        mb_plan = CompoundingMBPlan.objects.create(
            plan=plan,
            compound_code="AG-FRC-70",
            msl=2,
            batch_size=Decimal("4"),
            total_quantity=10,
            mb_quantity=10,
            mb_batches=3,
            created_by=self.user,
        )

        self._raise_crin(quantity="10", mb_planning="3")
        crin = CompoundingCRIN.objects.first()
        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        crin.refresh_from_db()
        self.assertEqual(crin.fb_planning, 2)
        self.assertEqual(crin.mb_planning, 3)

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
            follow=False,
        )
        line = crin.planned_lines.first()
        self.assertIsNotNone(line)
        self.assertEqual(line.shift1_mb, 3)
        self.assertEqual(line.shift1_fb, 2)

        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "1",
                "actual_fb": "1",
            },
            follow=False,
        )

        line.refresh_from_db()
        crin.refresh_from_db()
        mb_plan.refresh_from_db()
        self.assertEqual(line.returned_to_planning_mb, 1)
        self.assertEqual(line.returned_to_planning_fb, 1)
        self.assertEqual(crin.crin_number, "CRIN-2026-100")
        self.assertEqual(crin.mb_planning, 1)
        self.assertEqual(crin.fb_planning, 1)
        self.assertEqual(mb_plan.mb_batches, 1)

        mb_page = self.client.get(reverse("compounding:mb_planning"), {"date": "2026-02-03", "compound": "AG-FRC-70"})
        self.assertEqual(mb_page.status_code, 200)
        self.assertEqual(len(mb_page.context["rows"]), 1)
        self.assertEqual(mb_page.context["rows"][0]["mb_plan_number"], f"MBP-500-{mb_plan.id:03d}")

    def test_mb_planning_computes_mb_and_updates_board(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0", mb_stock="2")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        response = self.client.post(
            reverse("compounding:mb_planning_set"),
            data={"plan_date": "2026-02-03", "compound_code": "AG-FRC-70", "planning_quantity": "10"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        mb_plan = CompoundingMBPlan.objects.get(plan__plan_date="2026-02-03", compound_code="AG-FRC-70")
        self.assertEqual(mb_plan.msl, 2)
        self.assertEqual(mb_plan.batch_size, Decimal("4"))
        self.assertEqual(mb_plan.total_quantity, 10)
        self.assertEqual(mb_plan.mb_quantity, 10)
        self.assertEqual(mb_plan.mb_batches, 3)  # ceil(10/4) == 3

        response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={"plan_date": "2026-02-03", "compound_code": "AG-FRC-70", "shift": "1"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        # MB send is compound-level and does not need to link to a CRIN row.
        line = CompoundingLine.objects.filter(plan__plan_date="2026-02-03", compound_code="AG-FRC-70").first()
        self.assertIsNotNone(line)
        self.assertEqual(line.shift1_mb, 3)
        crin.refresh_from_db()
        self.assertEqual(crin.planned_lines.count(), 0)

    def test_board_remaining_mb_counts_fb_actual_as_mb_consumed(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="1")
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        CompoundingLine.objects.create(
            plan=plan,
            position=0,
            compound_code="AG-FRC-70",
            shift1_mb=3,
            shift1_fb=2,
            actual_mb=1,
            actual_fb=1,
        )

        response = self.client.get(reverse("compounding:display"), {"date": "2026-02-03"})

        self.assertEqual(response.status_code, 200)
        row = response.context["lines"][0]
        self.assertEqual(row["remaining_mb"], 1)
        self.assertEqual(row["remaining_fb"], 1)

    def test_mb_send_to_board_also_saves_planning(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0", mb_stock="2")

        response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={
                "plan_date": "2026-02-03",
                "compound_code": "AG-FRC-70",
                "planning_quantity": "10",
                "shift": "1",
            },
            follow=False,
        )
        self.assertEqual(response.status_code, 302)

        mb_plan = CompoundingMBPlan.objects.get(plan__plan_date="2026-02-03", compound_code="AG-FRC-70")
        self.assertEqual(mb_plan.total_quantity, 10)
        self.assertEqual(mb_plan.msl, 2)
        self.assertEqual(mb_plan.mb_quantity, 10)
        self.assertEqual(mb_plan.mb_batches, 3)

        line = CompoundingLine.objects.get(plan__plan_date="2026-02-03", compound_code="AG-FRC-70")
        self.assertEqual(line.shift1_mb, 3)

    def test_mb_send_to_board_rejects_invalid_shift(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0", mb_stock="2")

        response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={
                "plan_date": "2026-02-03",
                "compound_code": "AG-FRC-70",
                "planning_quantity": "10",
                "shift": "x",
            },
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CompoundingLine.objects.filter(plan__plan_date="2026-02-03", compound_code="AG-FRC-70").exists())

    def test_send_to_board_can_target_second_shift(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        crin.refresh_from_db()
        self.assertEqual(crin.fb_planning, 3)

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "2"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        line = crin.planned_lines.first()
        self.assertIsNotNone(line)
        self.assertEqual(line.shift2_fb, 3)
        self.assertIsNone(line.shift1_fb)

    def test_send_to_board_is_disabled_after_first_send(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        crin.refresh_from_db()
        self.assertEqual(crin.fb_planning, 3)

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "1"},
        )
        line = crin.planned_lines.first()
        self.assertEqual(line.shift1_fb, 3)

        # Attempt to send again should not update any other shift values.
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "2"},
        )
        line.refresh_from_db()
        self.assertIsNone(line.shift2_fb)

    def test_send_to_board_rejects_invalid_shift(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="4", chemical_weight="0")
        self._raise_crin(quantity="10")
        crin = CompoundingCRIN.objects.first()

        self.client.post(
            reverse("compounding:fb_planning_set"),
            data={"plan_date": "2026-02-03", "crin_id": str(crin.id)},
        )
        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": "2026-02-03", "shift": "bad"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(crin.planned_lines.count(), 0)

    def test_actuals_update_mb_fb_stock_levels(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            mb_weight="4",
            chemical_weight="1",
            mb_stock="100",
            fb_stock="10",
        )
        self._raise_crin(quantity="10")
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        line = CompoundingLine.objects.create(plan=plan, position=0, compound_code="AG-FRC-70")

        # Delta: MB +2 batches, FB +1 batch.
        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "2",
                "actual_fb": "1",
            },
        )

        sheet = CompoundingMasterSheet.objects.get(plan=plan)
        self.assertEqual(sheet.rows[0][5], "104")  # +2*4 -1*4
        self.assertEqual(sheet.rows[0][6], "15")   # +1*5

        # Second save should be blocked and must not mutate the saved values.
        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "3",
                "actual_fb": "2",
            },
        )
        line.refresh_from_db()
        sheet.refresh_from_db()
        self.assertEqual(line.actual_mb, 2)
        self.assertEqual(line.actual_fb, 1)
        self.assertTrue(line.actuals_saved)
        self.assertEqual(sheet.rows[0][5], "104")
        self.assertEqual(sheet.rows[0][6], "15")

    def test_actuals_save_is_allowed_only_once(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            mb_weight="4",
            chemical_weight="1",
            mb_stock="100",
            fb_stock="10",
        )
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        line = CompoundingLine.objects.create(plan=plan, position=0, compound_code="AG-FRC-70")

        # First save establishes actuals and stock.
        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "2",
                "actual_fb": "1",
            },
        )
        sheet = CompoundingMasterSheet.objects.get(plan=plan)
        self.assertEqual(sheet.rows[0][5], "104")
        self.assertEqual(sheet.rows[0][6], "15")

        # A second save with different values must be ignored.
        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "10",
                "actual_fb": "10",
            },
        )
        line.refresh_from_db()
        sheet.refresh_from_db()
        self.assertEqual(line.actual_mb, 2)
        self.assertEqual(line.actual_fb, 1)
        self.assertTrue(line.actuals_saved)
        self.assertEqual(sheet.rows[0][5], "104")
        self.assertEqual(sheet.rows[0][6], "15")

    def test_mb_planning_requires_mb_stock_from_master_sheet(self):
        self._seed_master_sheet(plan_date="2026-02-03")
        self._raise_crin(quantity="10")
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        CompoundingMasterSheet.objects.update_or_create(
            plan=plan,
            defaults={
                "created_by": self.user,
                "headers": ["Compound Code", "MB Weight"],
                "rows": [["AG-FRC-70", "4"]],
            },
        )

        response = self.client.post(
            reverse("compounding:mb_planning_set"),
            data={"plan_date": "2026-02-03", "compound_code": "AG-FRC-70", "planning_quantity": "10"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            CompoundingMBPlan.objects.filter(plan__plan_date="2026-02-03", compound_code="AG-FRC-70").exists()
        )

    def test_mb_planning_uses_manual_planning_quantity(self):
        self._seed_master_sheet(plan_date="2026-02-03", mb_weight="5", chemical_weight="0", mb_stock="3")

        response = self.client.post(
            reverse("compounding:mb_planning_set"),
            data={"plan_date": "2026-02-03", "compound_code": "AG-FRC-70", "planning_quantity": "23"},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)

        mb_plan = CompoundingMBPlan.objects.get(plan__plan_date="2026-02-03", compound_code="AG-FRC-70")
        self.assertEqual(mb_plan.total_quantity, 23)
        self.assertEqual(mb_plan.msl, 3)
        self.assertEqual(mb_plan.mb_quantity, 23)
        self.assertEqual(mb_plan.mb_batches, 5)

    def test_mb_planning_page_follows_master_sheet_row_order(self):
        plan, _created = CompoundingPlan.objects.get_or_create(
            plan_date="2026-02-03",
            defaults={"cjc_number": 500, "created_by": self.user},
        )
        CompoundingMasterSheet.objects.update_or_create(
            plan=plan,
            defaults={
                "created_by": self.user,
                "headers": [
                    "Compound Code",
                    "MB Weight",
                    "Chemicals Weight",
                    "MB & FB Total Weight",
                    "MB Min Stock Lavel",
                    "MB Correct Stock Lev",
                    "FB Correct Stock Lev",
                ],
                "rows": [
                    ["AG-7520", "35.670", "0", "35.670", "0", "2019", "0"],
                    ["AG-5520", "36.030", "0", "36.030", "0", "2053", "0"],
                    ["AG-6015", "35.915", "0", "35.915", "0", "1849", "0"],
                ],
            },
        )

        response = self.client.get(reverse("compounding:mb_planning"), {"date": "2026-02-03"})
        self.assertEqual(response.status_code, 200)
        options = [row["value"] for row in response.context["compound_options"]]
        self.assertEqual(options[:3], ["AG-7520", "AG-5520", "AG-6015"])
        self.assertEqual(response.context["rows"], [])

        selected_response = self.client.get(
            reverse("compounding:mb_planning"),
            {"date": "2026-02-03", "compound": "AG-5520"},
        )
        self.assertEqual(selected_response.status_code, 200)
        selected_rows = selected_response.context["rows"]
        self.assertEqual(len(selected_rows), 1)
        self.assertEqual(selected_rows[0]["compound_code"], "AG-5520")
        self.assertEqual(selected_rows[0]["mb_plan_number"], "MBP-500-002")

    def test_mb_planning_page_matches_codes_by_normalized_key(self):
        plan, _created = CompoundingPlan.objects.get_or_create(
            plan_date="2026-02-03",
            defaults={"cjc_number": 500, "created_by": self.user},
        )
        CompoundingMasterSheet.objects.update_or_create(
            plan=plan,
            defaults={
                "created_by": self.user,
                "headers": [
                    "Compound Code",
                    "MB Weight",
                    "Chemicals Weight",
                    "MB & FB Total Weight",
                    "MB Min Stock Lavel",
                    "MB Correct Stock Lev",
                    "FB Correct Stock Lev",
                ],
                "rows": [
                    ["AG-7520", "35.670", "0", "35.670", "0", "2019", "0"],
                    ["AG-5520", "36.030", "0", "36.030", "0", "2053", "0"],
                ],
            },
        )
        CompoundingCRIN.objects.create(
            plan=plan,
            crin_number="CRIN-2026-0001",
            compound_code="AG 5520",
            quantity=12,
            created_by=self.user,
        )
        CompoundingLine.objects.create(
            plan=plan,
            position=0,
            compound_code="AG7520",
            shift1_mb=3,
        )
        CompoundingMBPlan.objects.create(
            plan=plan,
            compound_code="AG 7520",
            msl=2019,
            batch_size=Decimal("35.670"),
            total_quantity=25,
            mb_quantity=0,
            mb_batches=0,
            created_by=self.user,
        )

        response = self.client.get(reverse("compounding:mb_planning"), {"date": "2026-02-03"})
        self.assertEqual(response.status_code, 200)
        options = [row["value"] for row in response.context["compound_options"]]
        self.assertIn("AG-7520", options)
        self.assertIn("AG-5520", options)

        selected = self.client.get(
            reverse("compounding:mb_planning"),
            {"date": "2026-02-03", "compound": "AG 7520"},
        )
        self.assertEqual(selected.status_code, 200)
        rows = selected.context["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["compound_code"], "AG-7520")
        self.assertEqual(row["planning_quantity"], 25)
        self.assertTrue(row["mb_already_sent"])

    def test_send_remaining_to_planning_creates_crin_once(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            compound_code="AG-6015",
            mb_weight="35.915",
            chemical_weight="1.08",
            fb_total_weight="36.995",
        )
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")
        line = CompoundingLine.objects.create(
            plan=plan,
            position=0,
            compound_code="AG-6015",
            shift1_mb=404,
            shift1_fb=50,
            actual_mb=150,
            actual_fb=25,
        )

        response = self.client.post(
            reverse("compounding:send_remaining_to_planning"),
            data={"plan_date": "2026-02-03", "line_id": str(line.id)},
            follow=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/compounding/planning/?date=2026-02-03", response["Location"])

        line.refresh_from_db()
        self.assertEqual(line.returned_to_planning_mb, 229)
        self.assertEqual(line.returned_to_planning_fb, 25)

        crin = CompoundingCRIN.objects.filter(plan=plan, compound_code="AG-6015").order_by("-id").first()
        self.assertIsNotNone(crin)
        self.assertEqual(crin.mb_planning, 229)
        self.assertEqual(crin.fb_planning, 25)
        self.assertGreater(crin.quantity, 0)

        # Repeat send should not create duplicate CRIN when nothing remains unsent.
        self.client.post(
            reverse("compounding:send_remaining_to_planning"),
            data={"plan_date": "2026-02-03", "line_id": str(line.id)},
            follow=False,
        )
        self.assertEqual(CompoundingCRIN.objects.filter(plan=plan, compound_code="AG-6015").count(), 1)

    def test_mb_remaining_returns_to_same_mb_plan_number_after_board_save(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            compound_code="AG-6015",
            mb_weight="35.915",
            chemical_weight="1.08",
            fb_total_weight="36.995",
            mb_stock="0",
            fb_stock="0",
        )
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")

        send_response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={
                "plan_date": "2026-02-03",
                "compound_code": "AG-6015",
                "planning_quantity": "323",
                "shift": "1",
            },
            follow=False,
        )
        self.assertEqual(send_response.status_code, 302)

        mb_page_before = self.client.get(
            reverse("compounding:mb_planning"),
            {"date": "2026-02-03", "compound": "AG-6015"},
        )
        self.assertEqual(mb_page_before.status_code, 200)
        row_before = mb_page_before.context["rows"][0]
        plan_number_before = row_before["mb_plan_number"]
        self.assertEqual(row_before["mb_batches"], 9)

        line = CompoundingLine.objects.get(plan=plan, compound_code="AG-6015")
        save_response = self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "5",
                "actual_fb": "0",
            },
            follow=False,
        )
        self.assertEqual(save_response.status_code, 302)

        line.refresh_from_db()
        self.assertEqual(line.returned_to_planning_mb, 4)

        mb_plan = CompoundingMBPlan.objects.get(plan=plan, compound_code="AG-6015")
        self.assertEqual(mb_plan.mb_batches, 4)
        self.assertGreater(mb_plan.total_quantity, 0)

        mb_page_after = self.client.get(
            reverse("compounding:mb_planning"),
            {"date": "2026-02-03", "compound": "AG-6015"},
        )
        self.assertEqual(mb_page_after.status_code, 200)
        row_after = mb_page_after.context["rows"][0]
        self.assertEqual(row_after["mb_batches"], 4)
        self.assertEqual(row_after["mb_plan_number"], plan_number_before)

    def test_mb_planning_auto_selects_pending_return_when_compound_not_provided(self):
        self._seed_master_sheet(
            plan_date="2026-02-03",
            compound_code="AG-6015",
            mb_weight="35.915",
            chemical_weight="1.08",
            fb_total_weight="36.995",
            mb_stock="0",
            fb_stock="0",
        )
        plan = CompoundingPlan.objects.get(plan_date="2026-02-03")

        self.client.post(
            reverse("compounding:mb_send_to_board"),
            data={
                "plan_date": "2026-02-03",
                "compound_code": "AG-6015",
                "planning_quantity": "323",
                "shift": "1",
            },
            follow=False,
        )
        line = CompoundingLine.objects.get(plan=plan, compound_code="AG-6015")
        self.client.post(
            reverse("compounding:set_actuals"),
            data={
                "plan_date": "2026-02-03",
                "line_id": str(line.id),
                "actual_mb": "5",
                "actual_fb": "0",
            },
            follow=False,
        )

        mb_page = self.client.get(reverse("compounding:mb_planning"), {"date": "2026-02-03"})
        self.assertEqual(mb_page.status_code, 200)
        self.assertEqual(mb_page.context["selected_compound"], "AG-6015")
        self.assertEqual(len(mb_page.context["rows"]), 1)
        self.assertEqual(mb_page.context["rows"][0]["mb_batches"], 4)

    def test_next_day_carryover_excludes_sent_back_remaining(self):
        prev_plan = CompoundingPlan.objects.create(plan_date="2026-02-03", cjc_number=500, created_by=self.user)
        CompoundingLine.objects.create(
            plan=prev_plan,
            position=0,
            compound_code="AG-6520",
            shift1_mb=100,
            shift1_fb=20,
            actual_mb=30,
            actual_fb=10,
            returned_to_planning_mb=70,
            returned_to_planning_fb=10,
        )

        self.client.get(reverse("compounding:planning"), {"date": "2026-02-04"})
        next_plan = CompoundingPlan.objects.get(plan_date="2026-02-04")
        self.assertFalse(next_plan.lines.filter(compound_code="AG-6520").exists())

    def test_next_day_starts_with_empty_board_even_if_previous_day_has_unfinished(self):
        prev_plan = CompoundingPlan.objects.create(plan_date="2026-02-03", cjc_number=500, created_by=self.user)
        CompoundingLine.objects.create(
            plan=prev_plan,
            position=0,
            compound_code="AG-7020",
            shift1_mb=100,
            shift1_fb=20,
            actual_mb=30,
            actual_fb=10,
            returned_to_planning_mb=0,
            returned_to_planning_fb=0,
        )

        self.client.get(reverse("compounding:planning"), {"date": "2026-02-04"})
        next_plan = CompoundingPlan.objects.get(plan_date="2026-02-04")
        self.assertFalse(next_plan.lines.exists())

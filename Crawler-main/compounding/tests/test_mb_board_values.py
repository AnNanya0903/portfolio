from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingMBPlan,
    CompoundingPlan,
)

PLAN_DATE = "2026-03-02"
CODE = "AG-NF 70"


class MBBoardValueTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="mb-board-user",
            email="mb-board@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[[CODE, "36.466", "2", "38.466", "1000", "0"]],
        )

    def _send(self, *, shift="1", follow=True, **data):
        payload = {"plan_date": PLAN_DATE, "compound_code": CODE, "shift": shift}
        payload.update(data)
        return self.client.post(reverse("compounding:mb_send_to_board"), data=payload, follow=follow)

    def _messages(self, response):
        return " ".join(str(message) for message in response.context["messages"])

    def _board_row(self):
        response = self.client.get(reverse("compounding:display"), {"date": PLAN_DATE})
        return response, response.context["lines"][0]

    def test_batches_calculate_planning_quantity(self):
        self._send(planning_source="batches", mb_batches="2", planning_quantity="72.932")

        mb_plan = CompoundingMBPlan.objects.get(plan=self.plan)
        line = CompoundingLine.objects.get(plan=self.plan)
        self.assertEqual(mb_plan.mb_batches, 2)
        self.assertEqual(mb_plan.total_quantity, 72)
        self.assertEqual(line.shift1_mb, 2)

    def test_batches_win_over_a_stale_quantity(self):
        self._send(planning_source="batches", mb_batches="3", planning_quantity="1")

        self.assertEqual(CompoundingLine.objects.get(plan=self.plan).shift1_mb, 3)

    def test_quantity_still_calculates_batches(self):
        self._send(planning_source="quantity", mb_batches="99", planning_quantity="60.524")

        self.assertEqual(CompoundingLine.objects.get(plan=self.plan).shift1_mb, 2)

    def test_saved_plan_shows_full_batch_quantity(self):
        self._send(planning_source="batches", mb_batches="2")

        response = self.client.get(reverse("compounding:mb_planning"), {"date": PLAN_DATE, "compound": CODE})

        row = response.context["rows"][0]
        self.assertEqual(row["mb_batches"], 2)
        self.assertEqual(row["planning_quantity_display"], "72.932")

    def test_invalid_or_zero_batches_are_rejected(self):
        for value in ("0", "-1", "1.5", "abc", "99999999999"):
            response = self._send(planning_source="batches", mb_batches=value)
            self.assertRegex(self._messages(response), "MB batch")
        self.assertFalse(CompoundingLine.objects.filter(plan=self.plan).exists())
        self.assertFalse(CompoundingMBPlan.objects.filter(plan=self.plan).exists())

    def test_board_shows_batches_weight_and_zero(self):
        CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, shift1_mb=2, shift1_fb=0)

        response, row = self._board_row()

        self.assertEqual(row["planned_mb_weight"], "72.932")
        self.assertEqual(row["remaining_mb_weight"], "72.932")
        self.assertContains(response, "72.932 qty")
        self.assertEqual(response.context["mb_rows"][0]["shift1"], 2)
        self.assertEqual(response.context["mb_rows"][0]["remaining"], 2)

    def test_board_shows_mb_and_fb_in_separate_sections(self):
        crin = CompoundingCRIN.objects.create(plan=self.plan, crin_number="CRIN-1", compound_code=CODE, quantity=77)
        mb_line = CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, shift1_mb=2)
        fb_line = CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, shift2_fb=3, crin_request=crin)

        response, _row = self._board_row()

        self.assertEqual([row["id"] for row in response.context["mb_rows"]], [mb_line.id])
        self.assertEqual([row["id"] for row in response.context["fb_rows"]], [fb_line.id])
        fb_row = response.context["fb_rows"][0]
        self.assertEqual((fb_row["crin_number"], fb_row["shift2"], fb_row["planned_weight"]), ("CRIN-1", 3, "115.398"))
        self.assertContains(response, "MB (Master Batch)")
        self.assertContains(response, "FB (Final Batch)")
        self.assertContains(response, f'id="actuals-{mb_line.id}"', count=1)
        self.assertContains(response, f'id="actuals-{fb_line.id}"', count=1)

    def test_fb_row_saves_only_fb(self):
        crin = CompoundingCRIN.objects.create(plan=self.plan, crin_number="CRIN-1", compound_code=CODE, quantity=77)
        fb_line = CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, shift1_fb=2, crin_request=crin)

        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(fb_line.id), "actual_fb": "2"},
        )

        fb_line.refresh_from_db()
        self.assertEqual((fb_line.actual_mb, fb_line.actual_fb), (0, 2))

    def test_mixed_row_appears_in_both_sections_with_one_form(self):
        line = CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, shift1_mb=2, shift1_fb=1)

        response, _row = self._board_row()

        self.assertTrue(response.context["mb_rows"][0]["owns_form"])
        self.assertFalse(response.context["fb_rows"][0]["owns_form"])
        self.assertContains(response, f'id="actuals-{line.id}"', count=1)
        self.assertContains(response, f'form="actuals-{line.id}"', count=3)
        self.assertContains(response, "Saves MB and FB together", count=2)

    def test_mb_sent_after_a_saved_line_gets_a_new_row(self):
        self._send(planning_source="batches", mb_batches="2")
        first = CompoundingLine.objects.get(plan=self.plan)
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(first.id), "actual_mb": "2", "actual_fb": "0"},
        )

        self._send(planning_source="batches", mb_batches="3", shift="2")

        first.refresh_from_db()
        second = CompoundingLine.objects.exclude(id=first.id).get(plan=self.plan)
        self.assertIsNone(first.shift2_mb)
        self.assertEqual(second.shift2_mb, 3)
        self.assertFalse(second.actuals_saved)

    def test_mb_does_not_merge_into_a_crin_row(self):
        crin = CompoundingCRIN.objects.create(plan=self.plan, crin_number="CRIN-1", compound_code=CODE, quantity=77)
        crin_line = CompoundingLine.objects.create(plan=self.plan, compound_code=CODE, crin_request=crin, shift1_fb=2)

        self._send(planning_source="batches", mb_batches="2")

        crin_line.refresh_from_db()
        self.assertIsNone(crin_line.shift1_mb)
        mb_line = CompoundingLine.objects.exclude(id=crin_line.id).get(plan=self.plan)
        self.assertEqual(mb_line.shift1_mb, 2)

    def test_same_shift_does_not_change_plan_or_board(self):
        self._send(planning_source="batches", mb_batches="2")

        response = self._send(planning_source="batches", mb_batches="9")

        self.assertIn("already has 2 MB batches", self._messages(response))
        self.assertEqual(CompoundingLine.objects.get(plan=self.plan).shift1_mb, 2)
        self.assertEqual(CompoundingMBPlan.objects.get(plan=self.plan).mb_batches, 2)

    def test_other_shift_adds_to_the_same_open_row(self):
        self._send(planning_source="batches", mb_batches="2")

        self._send(planning_source="batches", mb_batches="3", shift="2")

        line = CompoundingLine.objects.get(plan=self.plan)
        self.assertEqual((line.shift1_mb, line.shift2_mb), (2, 3))
        _response, row = self._board_row()
        self.assertEqual(row["planned_mb"], 5)
        self.assertEqual(row["planned_mb_weight"], str(Decimal("36.466") * 5).rstrip("0"))

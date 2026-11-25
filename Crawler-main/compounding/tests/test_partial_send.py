from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import CompoundingCRIN, CompoundingLine, CompoundingMasterSheet, CompoundingPlan

PLAN_DATE = "2026-03-02"


class PartialSendTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="ps", email="ps@example.com", password="x")
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)

    def _setup(self, mb_stock):
        CompoundingMasterSheet.objects.create(
            plan=self.plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", mb_stock, "0"]],
        )
        return CompoundingCRIN.objects.create(
            plan=self.plan, crin_number="CRIN-2026-9", compound_code="7020", quantity=1200, created_by=self.user
        )

    def _send(self, crin, partial=True):
        data = {"plan_date": PLAN_DATE, "shift": "1"}
        if partial:
            data["partial"] = "1"
        return self.client.post(reverse("compounding:send_to_board", args=[crin.id]), data)

    def test_sends_only_what_stock_covers_and_keeps_the_rest(self):
        crin = self._setup("400")  # 10 FB batches need 1000 MB; stock covers 4
        self._send(crin)

        crin.refresh_from_db()
        part = CompoundingCRIN.objects.get(crin_number="CRIN-2026-9-1")
        self.assertEqual(part.fb_planning, 4)
        self.assertEqual(part.display_number, "CRIN-2026-9")
        self.assertEqual(crin.fb_planning, 6)
        self.assertEqual(crin.quantity + part.quantity, 1200)
        line = CompoundingLine.objects.get(crin_request=part)
        self.assertEqual(line.shift1_fb, 4)
        self.assertFalse(CompoundingLine.objects.filter(crin_request=crin).exists())

    def test_without_partial_flag_a_short_crin_is_still_blocked(self):
        crin = self._setup("400")
        self._send(crin, partial=False)
        self.assertFalse(CompoundingLine.objects.exists())
        self.assertEqual(CompoundingCRIN.objects.count(), 1)

    def test_no_stock_means_nothing_is_sent(self):
        crin = self._setup("50")
        self._send(crin)
        self.assertFalse(CompoundingLine.objects.exists())
        self.assertEqual(CompoundingCRIN.objects.count(), 1)

    def test_remaining_part_waits_and_cannot_use_the_reserved_stock(self):
        crin = self._setup("400")
        self._send(crin)
        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})
        rows = {row["id"]: row for row in response.context["crins"]}
        self.assertTrue(rows[crin.id]["mb_is_short"])
        self.assertEqual(rows[crin.id]["partial_batches"], 0)

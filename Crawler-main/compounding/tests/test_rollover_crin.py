from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    compounding_business_date,
)


class RolloverKeepsCRINTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="ro", email="ro@example.com", password="x")
        self.client.force_login(self.user)
        self.today = compounding_business_date()
        self.old_plan = CompoundingPlan.objects.create(plan_date=self.today - timedelta(days=1), cjc_number=500, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.old_plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "5000", "0"]],
        )
        self.crin = CompoundingCRIN.objects.create(
            plan=self.old_plan, crin_number="CRIN-X", compound_code="7020", quantity=2400, fb_planning=20, created_by=self.user
        )
        self.old_line = CompoundingLine.objects.create(
            plan=self.old_plan, position=0, compound_code="7020", shift3_fb=20, crin_request=self.crin
        )

    def test_unsaved_crin_line_moves_to_today_with_its_link(self):
        self.client.get(reverse("compounding:display"), {"date": self.today.isoformat()})
        self.old_line.refresh_from_db()
        self.assertEqual(self.old_line.plan.plan_date, self.today)
        self.assertEqual(self.old_line.crin_request_id, self.crin.id)
        self.assertEqual(CompoundingLine.objects.filter(compound_code="7020").count(), 1)

    def test_saving_part_of_carried_line_returns_rest_to_crin(self):
        self.client.get(reverse("compounding:display"), {"date": self.today.isoformat()})
        self.client.post(
            reverse("compounding:set_actuals"),
            {"plan_date": self.today.isoformat(), "line_id": self.old_line.id, "actual_mb": 0, "actual_fb": 10},
        )
        self.crin.refresh_from_db()
        self.old_line.refresh_from_db()
        self.assertEqual(self.crin.fb_planning, 10)
        self.assertEqual(self.crin.status, "open")
        self.assertEqual(self.old_line.returned_to_planning_fb, 10)

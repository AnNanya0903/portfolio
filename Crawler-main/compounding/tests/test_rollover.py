from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import CompoundingLine, CompoundingPlan


class CompoundingRolloverTests(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.user = User.objects.create_user(username="u", email="u@example.com", password="x")
        self.client.force_login(self.user)

    def test_rollover_carries_remaining_batches(self):
        prev = CompoundingPlan.objects.create(plan_date="2026-02-01", cjc_number=500)
        CompoundingLine.objects.create(
            plan=prev,
            position=0,
            compound_code="AG-7020",
            shift1_mb=10,
            shift1_fb=4,
            actual_mb=6,
            actual_fb=1,
        )

        # Hitting the board for the next day creates the plan and seeds remaining batches.
        response = self.client.get(reverse("compounding:display") + "?date=2026-02-02")
        self.assertEqual(response.status_code, 200)

        new_plan = CompoundingPlan.objects.get(plan_date="2026-02-02")
        seeded = new_plan.lines.filter(compound_code="AG-7020").first()
        self.assertIsNotNone(seeded)
        self.assertEqual(seeded.shift1_mb, 3)  # 10 - 6 - 1 FB consumed
        self.assertEqual(seeded.shift1_fb, 3)  # 4 - 1

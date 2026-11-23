from django.test import TestCase

from compounding.models import CompoundingPlan, next_compounding_cjc_number


class CompoundingCJCSequenceTests(TestCase):
    def test_starts_from_500(self):
        self.assertEqual(next_compounding_cjc_number(year=2026), 500)
        self.assertEqual(next_compounding_cjc_number(year=2026), 501)

    def test_bootstraps_from_existing_max(self):
        CompoundingPlan.objects.create(plan_date="2026-02-01", cjc_number=575)
        self.assertEqual(next_compounding_cjc_number(year=2026), 576)


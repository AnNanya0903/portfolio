from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import CompoundingLine, CompoundingPlan
from dashboard.models import UserServiceAccess


class CompoundingProductionReportTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="compound-report",
            email="compound-report@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])

    def _seed_report_data(self) -> None:
        plan_1 = CompoundingPlan.objects.create(plan_date=date(2026, 4, 6), cjc_number=700, created_by=self.user)
        plan_2 = CompoundingPlan.objects.create(plan_date=date(2026, 4, 7), cjc_number=701, created_by=self.user)
        CompoundingLine.objects.create(
            plan=plan_1,
            position=1,
            compound_code="AG-FRC-70",
            shift1_mb=2,
            shift1_fb=1,
            shift2_mb=1,
            shift2_fb=1,
            actual_mb=1,
            actual_fb=2,
            actuals_saved=True,
        )
        CompoundingLine.objects.create(
            plan=plan_2,
            position=1,
            compound_code="AG-FRC-70",
            shift3_mb=1,
            shift3_fb=0,
            actual_mb=0,
            actual_fb=0,
            actuals_saved=False,
        )
        CompoundingLine.objects.create(
            plan=plan_2,
            position=2,
            compound_code="AG-NF-60",
            shift2_mb=4,
            shift2_fb=1,
            actual_mb=2,
            actual_fb=0,
            actuals_saved=True,
            returned_to_planning_mb=1,
        )

    def test_weekly_report_aggregates_board_production(self) -> None:
        self._seed_report_data()

        response = self.client.get(
            reverse("compounding:production_report"),
            {"period": "weekly", "date": "2026-04-08"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compounding Production Report")
        report = response.context["report"]
        self.assertEqual(report["period"], "weekly")
        self.assertEqual(report["start_date"], date(2026, 4, 6))
        self.assertEqual(report["end_date"], date(2026, 4, 12))
        self.assertEqual(report["overall"]["plan_count"], 2)
        self.assertEqual(report["overall"]["line_count"], 3)
        self.assertEqual(report["overall"]["saved_line_count"], 2)
        self.assertEqual(report["overall"]["planned_mb"], 8)
        self.assertEqual(report["overall"]["planned_fb"], 3)
        self.assertEqual(report["overall"]["actual_mb"], 3)
        self.assertEqual(report["overall"]["actual_fb"], 2)
        self.assertEqual(report["overall"]["mb_consumed"], 5)
        self.assertEqual(report["overall"]["returned_mb"], 1)
        self.assertEqual(report["overall"]["remaining_mb"], 2)
        self.assertEqual(report["overall"]["remaining_fb"], 1)
        self.assertEqual(report["overall"]["completion_display"], "45.5%")

        compound_rows = {item["compound_code"]: item for item in report["compound_rows"]}
        self.assertEqual(compound_rows["AG-FRC-70"]["active_day_count"], 2)
        self.assertEqual(compound_rows["AG-FRC-70"]["remaining_mb"], 1)
        self.assertEqual(compound_rows["AG-NF-60"]["remaining_fb"], 1)

    def test_report_csv_exports_same_production_window(self) -> None:
        self._seed_report_data()

        response = self.client.get(
            reverse("compounding:production_report_csv"),
            {"period": "weekly", "date": "2026-04-08"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8")
        self.assertIn("Compounding Production Report", body)
        self.assertIn("Weekly", body)
        self.assertIn("AG-FRC-70", body)
        self.assertIn("45.5%", body)

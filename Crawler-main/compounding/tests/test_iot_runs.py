from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding.models import CompoundingIoTRun, CompoundingMBPlan, CompoundingPlan, IST_TIMEZONE
from dashboard.models import UserServiceAccess


class CompoundingIoTRunViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="iot-runner",
            email="iot-runner@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])

        self.plan = CompoundingPlan.objects.create(plan_date="2026-04-06", cjc_number=700, created_by=self.user)
        CompoundingMBPlan.objects.create(
            plan=self.plan,
            compound_code="AG-5520",
            total_quantity=100,
            mb_quantity=20,
            mb_batches=2,
            created_by=self.user,
        )

    @override_settings(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz", IOT_MACHINE_ID_DEFAULT="mixing-mill-2")
    def test_save_iot_run_creates_operator_context(self) -> None:
        response = self.client.post(
            reverse("compounding:save_iot_run"),
            {
                "plan_date": "2026-04-06",
                "compound_code": "AG-5520",
                "started_at": "2026-04-06T10:00",
                "ended_at": "2026-04-06T11:30",
                "stock_quality": "good",
                "notes": "Batch looked stable and clean.",
            },
        )

        self.assertEqual(response.status_code, 302)
        run = CompoundingIoTRun.objects.get(plan=self.plan, compound_code="AG-5520")
        self.assertEqual(run.machine_id, "mixing-mill-2")
        self.assertEqual(run.started_at.astimezone(IST_TIMEZONE).strftime("%Y-%m-%dT%H:%M"), "2026-04-06T10:00")
        self.assertEqual(run.ended_at.astimezone(IST_TIMEZONE).strftime("%Y-%m-%dT%H:%M"), "2026-04-06T11:30")
        self.assertEqual(run.stock_quality, "good")
        self.assertEqual(run.notes, "Batch looked stable and clean.")

    @override_settings(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz", IOT_MACHINE_ID_DEFAULT="mixing-mill-2")
    def test_iot_dashboard_renders_mb_compound_choices(self) -> None:
        response = self.client.get(reverse("compounding:iot_dashboard"), {"date": "2026-04-06"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production Context")
        self.assertContains(response, "AG-5520")

    @override_settings(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz", IOT_MACHINE_ID_DEFAULT="mixing-mill-2")
    def test_iot_dashboard_lists_saved_compounds_with_time_taken(self) -> None:
        self.client.post(
            reverse("compounding:save_iot_run"),
            {
                "plan_date": "2026-04-06",
                "compound_code": "AG-5520",
                "started_at": "2026-04-06T10:00",
                "ended_at": "2026-04-06T11:30",
                "stock_quality": "good",
            },
        )

        response = self.client.get(reverse("compounding:iot_dashboard"), {"date": "2026-04-06"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Time taken")
        self.assertContains(response, 'id="iotRunsTable"')
        self.assertContains(response, "1 h 30 min")

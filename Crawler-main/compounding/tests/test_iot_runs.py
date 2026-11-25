from __future__ import annotations

from django.contrib.auth import get_user_model
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from compounding.models import CompoundingCRIN, CompoundingIoTRun, CompoundingMBPlan, CompoundingPlan, IST_TIMEZONE
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


IOT_SETTINGS = dict(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz", IOT_MACHINE_ID_DEFAULT="mixing-mill-2")


@override_settings(**IOT_SETTINGS)
class CompoundingIoTBatchStartEndTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="batch-op", email="batch-op@example.com", password="secret123")
        self.client.force_login(self.user)
        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])
        self.plan = CompoundingPlan.objects.create(plan_date="2026-04-06", cjc_number=701, created_by=self.user)
        CompoundingMBPlan.objects.create(
            plan=self.plan, compound_code="AG-5520", total_quantity=100, mb_quantity=20, mb_batches=2, created_by=self.user
        )
        self.crin = CompoundingCRIN.objects.create(
            plan=self.plan, crin_number="CRIN-2026-900-1", compound_code="AG-5520", quantity=4, created_by=self.user
        )

    def _start(self, **overrides):
        data = {"plan_date": "2026-04-06", "compound_code": "AG-5520", "batch_type": "mb"}
        data.update(overrides)
        return self.client.post(reverse("compounding:start_iot_batch"), data)

    def _end(self, run, **overrides):
        data = {"plan_date": "2026-04-06", "run_id": str(run.pk), "stock_quality": "good"}
        data.update(overrides)
        return self.client.post(reverse("compounding:end_iot_batch"), data)

    def test_start_batch_opens_a_run_with_now_as_start(self) -> None:
        before = timezone.now()
        response = self._start(batch_weight_kg="52.5", crin_id=str(self.crin.pk))

        self.assertEqual(response.status_code, 302)
        run = CompoundingIoTRun.objects.get()
        self.assertEqual(run.machine_id, "mixing-mill-2")
        self.assertEqual(run.compound_code, "AG-5520")
        self.assertEqual(run.batch_type, "mb")
        self.assertEqual(run.batch_weight_kg, Decimal("52.500"))
        self.assertEqual(run.crin, self.crin)
        self.assertIsNone(run.ended_at)
        self.assertEqual(run.stock_quality, "")
        self.assertGreaterEqual(run.started_at, before)

    def test_dashboard_shows_running_batch_and_end_button(self) -> None:
        self._start()
        response = self.client.get(reverse("compounding:iot_dashboard"), {"date": "2026-04-06"})

        self.assertContains(response, "Batch running")
        self.assertContains(response, "End batch")
        self.assertNotContains(response, 'id="iotStartBatchForm"')
        self.assertContains(response, "Running")

    def test_dashboard_shows_start_button_when_machine_is_free(self) -> None:
        response = self.client.get(reverse("compounding:iot_dashboard"), {"date": "2026-04-06"})

        self.assertContains(response, 'id="iotStartBatchForm"')
        self.assertContains(response, "CRIN-2026-900")

    def test_cannot_start_second_batch_while_one_is_running(self) -> None:
        self._start()
        self._start(batch_type="fb")

        self.assertEqual(CompoundingIoTRun.objects.count(), 1)

    def test_start_rejects_missing_type_bad_weight_and_closed_crin(self) -> None:
        self._start(batch_type="")
        self._start(batch_weight_kg="-3")
        self._start(batch_weight_kg="abc")
        self.crin.status = CompoundingCRIN.STATUS_CLOSED
        self.crin.save(update_fields=["status"])
        self._start(crin_id=str(self.crin.pk))
        self._start(compound_code="NOT-PLANNED")

        self.assertFalse(CompoundingIoTRun.objects.exists())

    def test_end_batch_closes_run_with_quality(self) -> None:
        self._start()
        run = CompoundingIoTRun.objects.get()

        response = self._end(run, notes="Smooth")

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertIsNotNone(run.ended_at)
        self.assertGreaterEqual(run.ended_at, run.started_at)
        self.assertEqual(run.stock_quality, "good")
        self.assertEqual(run.rejection_reason, "")
        self.assertEqual(run.notes, "Smooth")
        # The machine is free again.
        self._start(batch_type="fb")
        self.assertEqual(CompoundingIoTRun.objects.filter(ended_at__isnull=True).count(), 1)

    def test_rejected_batch_needs_a_reason(self) -> None:
        self._start()
        run = CompoundingIoTRun.objects.get()

        self._end(run, stock_quality="rejected")
        run.refresh_from_db()
        self.assertIsNone(run.ended_at)

        self._end(run, stock_quality="rejected", rejection_reason="scorch")
        run.refresh_from_db()
        self.assertEqual(run.stock_quality, "rejected")
        self.assertEqual(run.rejection_reason, "scorch")

    def test_reason_is_dropped_when_batch_is_not_rejected(self) -> None:
        self._start()
        run = CompoundingIoTRun.objects.get()
        self._end(run, stock_quality="ok", rejection_reason="scorch")
        run.refresh_from_db()
        self.assertEqual(run.rejection_reason, "")

    def test_end_requires_quality_and_cannot_end_twice(self) -> None:
        self._start()
        run = CompoundingIoTRun.objects.get()

        self._end(run, stock_quality="")
        run.refresh_from_db()
        self.assertIsNone(run.ended_at)

        self._end(run)
        run.refresh_from_db()
        first_end = run.ended_at
        self._end(run, stock_quality="rejected", rejection_reason="other")
        run.refresh_from_db()
        self.assertEqual(run.ended_at, first_end)
        self.assertEqual(run.stock_quality, "good")

    def test_cannot_end_another_machines_batch(self) -> None:
        other = CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id="other-mill", compound_code="AG-5520", started_at=timezone.now(), ended_at=None, stock_quality=""
        )
        self._end(other)
        other.refresh_from_db()
        self.assertIsNone(other.ended_at)

    def test_get_is_not_allowed(self) -> None:
        self.assertEqual(self.client.get(reverse("compounding:start_iot_batch")).status_code, 405)
        self.assertEqual(self.client.get(reverse("compounding:end_iot_batch")).status_code, 405)

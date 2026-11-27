from __future__ import annotations

from datetime import datetime, timedelta

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from compounding import iot_batches, iot_curves, iot_samples
from compounding.models import (
    IST_TIMEZONE,
    CompoundingIoTRun,
    CompoundingPlan,
    ProcessGroup,
    ProcessGroupCompound,
)
from compounding.tests.test_iot_batches import SETTINGS, batch_profile

MACHINE = "mixing-mill-2"


def at(index: int) -> datetime:
    """Batch slots 20 minutes apart from 06:00 IST, so stored samples never overlap."""
    return datetime(2026, 11, 27, 6, 0, tzinfo=IST_TIMEZONE) + timedelta(minutes=20 * index)


@override_settings(**SETTINGS)
class StandardCurveTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.plan = CompoundingPlan.objects.create(plan_date="2026-11-27", cjc_number=910)
        self.group = ProcessGroup.objects.create(name="Two-stage NBR")
        ProcessGroupCompound.objects.create(process_group=self.group, compound_code="AG-7020")
        self.slot = 0

    def _batch(self, *, energy: float = 10.2, current_shift: float = 0.0, lab: str = CompoundingIoTRun.LAB_PASS,
               plateau_boost: float = 0.0) -> CompoundingIoTRun:
        start = at(self.slot)
        self.slot += 1
        raw = batch_profile(start, minutes=15, energy_kwh=energy, idle_before_s=60, idle_after_s=60)
        for index, item in enumerate(raw):
            current = item["raw"]["avg_current"]
            if current > 20:
                current += current_shift
                # The plateau sits 4-12.5 min into the batch (after the spike, before the end dips).
                if 60 + 230 <= index * 2 < 60 + 750:
                    current += plateau_boost
            item["raw"]["avg_current"] = round(current, 2)
        iot_samples.store_payloads(MACHINE, raw)
        run = CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id=MACHINE, compound_code="AG-7020", started_at=start,
            ended_at=start + timedelta(minutes=15), stock_quality="", lab_result=lab,
        )
        return iot_batches.finalize_batch(run)

    def _passing_batches(self, count: int) -> None:
        for index in range(count):
            # Small batch-to-batch spread: +/-1.5 A and 10.0-10.4 kWh.
            self._batch(energy=10.0 + (index % 5) * 0.1, current_shift=(index % 7) * 0.5 - 1.5)

    def test_needs_at_least_twenty_lab_passed_batches(self) -> None:
        self._passing_batches(19)
        self._batch(lab=CompoundingIoTRun.LAB_FAIL)
        self._batch(lab=CompoundingIoTRun.LAB_PENDING)
        with self.assertRaisesMessage(iot_curves.CurveBuildError, "19 lab-passed"):
            iot_curves.build_standard_curve(self.group)

    def test_invalid_batches_are_not_used(self) -> None:
        self._passing_batches(19)
        short = self._batch()
        short.ended_at = short.started_at + timedelta(minutes=1)
        short.save()
        iot_batches.finalize_batch(short)
        self.assertEqual(short.status, CompoundingIoTRun.STATUS_INVALID)
        with self.assertRaises(iot_curves.CurveBuildError):
            iot_curves.build_standard_curve(self.group)

    def test_build_aligns_on_load_start_and_stores_the_energy_band(self) -> None:
        self._passing_batches(20)

        curve = iot_curves.build_standard_curve(self.group)

        self.assertEqual(curve.version, 1)
        self.assertEqual(curve.built_from_n, 20)
        self.assertTrue(curve.is_active)
        self.assertEqual(curve.points[0]["t"], 0)
        self.assertEqual(curve.points[1]["t"] - curve.points[0]["t"], 10)
        # t=0 is the first sample above 90 A, so the first point is in the loading spike.
        self.assertGreater(curve.points[0]["mean"], 90)
        for point in curve.points:
            self.assertLessEqual(point["lower"], point["mean"])
            self.assertGreaterEqual(point["upper"], point["mean"])
        self.assertAlmostEqual(curve.energy_mean, 10.2, delta=0.05)
        self.assertGreater(curve.energy_std, 0)

    def test_rebuilding_makes_a_new_active_version(self) -> None:
        self._passing_batches(20)
        first = iot_curves.build_standard_curve(self.group)
        second = iot_curves.build_standard_curve(self.group)
        first.refresh_from_db()
        self.assertEqual(second.version, 2)
        self.assertFalse(first.is_active)
        self.assertEqual(iot_curves.active_curve(self.group), second)

    def test_batches_are_compared_with_the_active_curve(self) -> None:
        self._passing_batches(20)
        iot_curves.build_standard_curve(self.group)

        normal = self._batch(lab=CompoundingIoTRun.LAB_PENDING)
        heavy = self._batch(lab=CompoundingIoTRun.LAB_PENDING, plateau_boost=20.0, energy=12.0)

        self.assertEqual(normal.process_group, self.group)
        self.assertEqual(normal.curve_status, CompoundingIoTRun.CURVE_IN_BAND)
        self.assertLessEqual(normal.out_of_band_seconds, 60)
        self.assertFalse(normal.energy_out_of_band)

        self.assertEqual(heavy.curve_status, CompoundingIoTRun.CURVE_OUT_OF_BAND)
        self.assertGreater(heavy.out_of_band_seconds, 5 * 60)
        self.assertTrue(heavy.energy_out_of_band)
        self.assertAlmostEqual(heavy.energy_deviation_pct, (12.0 - 10.2) / 10.2 * 100, delta=1.0)

    def test_compound_without_a_group_has_no_standard(self) -> None:
        ProcessGroupCompound.objects.all().delete()
        run = self._batch()
        self.assertIsNone(run.process_group)
        self.assertEqual(run.curve_status, CompoundingIoTRun.CURVE_NO_STANDARD)

    def test_management_command(self) -> None:
        with self.assertRaises(CommandError):
            call_command("build_standard_curve", "Two-stage NBR")
        self._passing_batches(20)
        call_command("build_standard_curve", "Two-stage NBR")
        self.assertEqual(self.group.curves.count(), 1)

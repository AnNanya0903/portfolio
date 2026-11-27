from __future__ import annotations

from datetime import date, timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from compounding import iot_features, iot_standards
from compounding.models import (
    CompoundingIoTRun,
    CompoundingPlan,
    DetectedBatch,
    ProcessGroup,
    ProcessGroupCompound,
    StandardCurve,
)
from compounding.tests.iot_fixtures import DAY, agfrc70, ag7020, ist_ms, make_detected_batch
from compounding.iot_segments import ms_to_datetime

SETTINGS = dict(IOT_MACHINE_ID_DEFAULT="mixing-mill-2", IOT_MACHINE_ID_SOURCE="extrusion-mill-1",
                IOT_STANDARD_MIN_BATCHES=15)


def seed(profile, compound: str, count: int, *, minutes: str = "15:30", first_day: date = date(2026, 11, 1)):
    """`count` tagged batches of a profile, one a day, with +/-3 % spread in current."""
    batches = []
    for index in range(count):
        day = first_day + timedelta(days=index)
        end = f"10:{minutes}" if ":" in minutes else minutes
        batches.append(make_detected_batch(profile, "10:00", end, day=day, tag=compound,
                                           scale=0.97 + (index % 7) * 0.01))
    return batches


class FeatureTests(SimpleTestCase):
    def test_steady_stretch_and_dips(self) -> None:
        curve = [110, 108, 105] + [73, 74, 72, 73, 75, 73] * 6 + [50, 78, 46, 80, 47, 48]
        start, end = iot_features.steady_stretch(curve, 8.0)
        self.assertEqual((start, end), (3, 39))
        features = iot_features.compute_features(curve, 10, {"peak_current": 110, "duration_s": 450})
        self.assertAlmostEqual(features["steady_mean"], 73.3, delta=0.2)
        self.assertEqual(features["steady_len_s"], 360)
        self.assertEqual(features["dips"], 3)


@override_settings(**SETTINGS)
class FeatureShapeTests(TestCase):
    def test_ag7020_and_agfrc70_have_different_features(self) -> None:
        ag = make_detected_batch(ag7020, "11:01", "11:16:30")
        frc = make_detected_batch(agfrc70, "12:24", "12:44")
        self.assertGreater(ag.features["peak_current"], 100)
        self.assertLess(frc.features["peak_current"], 80)
        self.assertGreater(frc.features["steady_len_s"], ag.features["steady_len_s"] + 5 * 60)
        self.assertAlmostEqual(ag.features["steady_mean"], 73, delta=2)
        self.assertAlmostEqual(frc.features["steady_mean"], 69, delta=2)


@override_settings(**SETTINGS)
class LabelTests(TestCase):
    def setUp(self) -> None:
        self.plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=930)

    def _log(self, start: str, end: str, compound: str = "AG-7020") -> CompoundingIoTRun:
        return CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id="mixing-mill-2", compound_code=compound,
            started_at=ms_to_datetime(ist_ms(start)), ended_at=ms_to_datetime(ist_ms(end)), stock_quality="good",
        )

    def test_a_well_overlapping_log_labels_the_batch(self) -> None:
        self._log("11:01", "11:17")
        batch = make_detected_batch(ag7020, "11:01", "11:16:30")
        self.assertEqual(batch.label_compound, "AG-7020")
        self.assertGreaterEqual(batch.log_overlap, 0.7)

    def test_a_short_log_is_linked_but_does_not_label(self) -> None:
        log = self._log("11:55", "11:57")
        batch = make_detected_batch(ag7020, "11:41:30", "11:57")
        self.assertEqual(batch.log_run, log)
        self.assertLess(batch.log_overlap, 0.7)
        self.assertEqual(batch.label_compound, "")

    def test_a_tag_wins_over_the_log(self) -> None:
        self._log("12:24", "12:44", compound="AG-7020")
        batch = make_detected_batch(agfrc70, "12:24", "12:44", tag="AG-FRC 70")
        self.assertEqual(batch.label_compound, "AG-FRC 70")


@override_settings(**SETTINGS)
class StandardBuildTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    def test_needs_fifteen_normal_batches(self) -> None:
        batches = seed(ag7020, "AG-7020", 15)
        batches[0].excluded = True
        batches[0].save()
        with self.assertRaisesMessage(iot_standards.StandardBuildError, "14 normal labelled batches"):
            iot_standards.build_standard("compound:AG-7020")

    def test_build_median_percentile_band_and_stats(self) -> None:
        seed(ag7020, "AG-7020", 16)
        standard = iot_standards.build_standard("compound:AG-7020")

        self.assertEqual((standard.version, standard.built_from_n), (1, 16))
        self.assertEqual(standard.band_method, "percentile")
        self.assertEqual(standard.points[1]["t"] - standard.points[0]["t"], 10)
        self.assertGreater(standard.points[0]["median"], 90)  # aligned on the load point (spike)
        for point in standard.points:
            self.assertLessEqual(point["lower"], point["median"])
            self.assertLessEqual(point["median"], point["upper"])
        self.assertAlmostEqual(standard.stats["duration_s"]["median"], 930, delta=1)
        self.assertGreater(standard.stats["energy_kwh"]["median"], 9.5)
        self.assertIn("time_to_peak_s", standard.stats)
        self.assertIn("peak_current", standard.stats)

    def test_sigma_band_option(self) -> None:
        seed(ag7020, "AG-7020", 15)
        with self.settings(IOT_STANDARD_BAND="sigma"):
            standard = iot_standards.build_standard("compound:AG-7020")
        self.assertEqual(standard.band_method, "sigma")

    def test_versions_and_unchanged_sets(self) -> None:
        seed(ag7020, "AG-7020", 15)
        first = iot_standards.build_standard("compound:AG-7020")
        self.assertEqual(iot_standards.build_standard("compound:AG-7020").pk, first.pk)
        seed(ag7020, "AG-7020", 1, first_day=date(2026, 10, 1))
        second = iot_standards.build_standard("compound:AG-7020")
        first.refresh_from_db()
        self.assertEqual(second.version, 2)
        self.assertFalse(first.is_active)
        forced = iot_standards.build_standard("compound:AG-7020", force=True)
        self.assertEqual(forced.version, 3)

    def test_process_group_standard_pools_its_compounds(self) -> None:
        group = ProcessGroup.objects.create(name="NBR 15 min")
        for code in ("AG-7020", "AG-7021"):
            ProcessGroupCompound.objects.create(process_group=group, compound_code=code)
        seed(ag7020, "AG-7020", 8)
        seed(ag7020, "AG-7021", 8, first_day=date(2026, 10, 1))
        self.assertEqual(iot_standards.key_for("AG-7021")[0], f"group:{group.pk}")
        standard = iot_standards.build_standard(f"group:{group.pk}")
        self.assertEqual(standard.built_from_n, 16)
        self.assertEqual(standard.label, "NBR 15 min")

    def test_rebuild_all_skips_thin_compounds(self) -> None:
        seed(ag7020, "AG-7020", 15)
        seed(agfrc70, "AG-FRC 70", 3, minutes="20:00", first_day=date(2026, 10, 1))
        report = {item["label"]: item for item in iot_standards.rebuild_all()}
        self.assertTrue(report["AG-7020"]["built"])
        self.assertFalse(report["AG-FRC 70"]["built"])
        self.assertEqual(StandardCurve.objects.count(), 1)

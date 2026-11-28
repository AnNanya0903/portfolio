from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from compounding import iot_compare, iot_standards
from compounding.models import DetectedBatch, StandardCurve
from compounding.tests.iot_fixtures import (
    agfrc70, ag7020, ag7020_late, ag7020_long, make_detected_batch,
)

SETTINGS = dict(IOT_MACHINE_ID_DEFAULT="mixing-mill-2", IOT_MACHINE_ID_SOURCE="extrusion-mill-1",
                IOT_STANDARD_MIN_BATCHES=15)
NEW_DAY = date(2026, 12, 1)


def seed(profile, compound: str, count: int, *, end: str = "10:15:30", first_day: date = date(2026, 11, 1)):
    """`count` normal batches, one a day, with +/-3 % spread in current."""
    return [
        make_detected_batch(profile, "10:00", end, day=first_day + timedelta(days=index), compound=compound,
                            scale=0.97 + (index % 7) * 0.01)
        for index in range(count)
    ]


class SectionTests(SimpleTestCase):
    def test_out_of_band_sections_ignore_short_blips(self) -> None:
        flags = [(t, not (360 <= t < 540 or t == 700)) for t in range(0, 900, 10)]
        self.assertEqual(iot_compare.out_of_band_sections(flags, 10), [[360, 540]])
        self.assertEqual(iot_compare._section_text([360, 540]), "Outside band 6–9 min")


@override_settings(**SETTINGS)
class StandardTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    def test_not_ready_until_fifteen_normal_batches(self) -> None:
        batches = seed(ag7020, "AG-7020", 15)
        batches[0].excluded = True
        batches[0].save()
        with self.assertRaisesMessage(iot_standards.StandardBuildError, "Standard not ready for AG-7020 (14/15"):
            iot_standards.build_standard("AG-7020")
        self.assertEqual(batches[3].result, DetectedBatch.RESULT_NO_STANDARD)
        # When the 4th batch arrived, its compound had 4 normal batches.
        self.assertEqual(batches[3].reasons, ["Standard not ready (4/15)"])

    def test_median_band_and_standard_values(self) -> None:
        seed(ag7020, "AG-7020", 16)
        standard = iot_standards.build_standard("AG-7020")
        self.assertEqual((standard.version, standard.built_from_n), (1, 16))
        self.assertEqual(standard.points[1]["t"] - standard.points[0]["t"], 10)
        self.assertGreater(standard.points[0]["median"], 85)  # aligned at the loading point
        for point in standard.points:
            self.assertLessEqual(point["lower"], point["median"])
            self.assertLessEqual(point["median"], point["upper"])
        for name in ("duration_s", "energy_kwh", "peak_current", "time_to_peak_s", "steady_current"):
            stat = standard.stats[name]
            self.assertLessEqual(stat["low"], stat["median"])
            self.assertLessEqual(stat["median"], stat["high"])
        self.assertAlmostEqual(standard.stats["duration_s"]["median"], 930, delta=1)

    def test_building_compares_the_batches_that_were_waiting(self) -> None:
        batches = seed(ag7020, "AG-7020", 15)
        iot_standards.build_standard("AG-7020")
        batches[0].refresh_from_db()
        self.assertIn(batches[0].result, {DetectedBatch.RESULT_OK, DetectedBatch.RESULT_VARIATION})
        self.assertIsNotNone(batches[0].match_pct)

    def test_versions_and_weekly_rebuild(self) -> None:
        seed(ag7020, "AG-7020", 15)
        seed(agfrc70, "AG-FRC 70", 3, end="10:20:00", first_day=date(2026, 10, 1))
        first = iot_standards.build_standard("AG-7020")
        self.assertEqual(iot_standards.build_standard("AG-7020").pk, first.pk)  # unchanged set
        report = {item["compound"]: item for item in iot_standards.rebuild_all()}
        self.assertFalse(report["AG-7020"]["built"])
        self.assertFalse(report["AG-FRC 70"]["built"])
        seed(ag7020, "AG-7020", 1, first_day=date(2026, 9, 1))
        report = {item["compound"]: item for item in iot_standards.rebuild_all()}
        self.assertEqual(report["AG-7020"]["version"], 2)
        first.refresh_from_db()
        self.assertFalse(first.is_active)

    def test_command(self) -> None:
        with self.assertRaises(CommandError):
            call_command("build_standard_curves", "--compound", "AG-7020", stdout=StringIO())
        seed(ag7020, "AG-7020", 15)
        call_command("build_standard_curves", "--all", stdout=StringIO())
        self.assertEqual(StandardCurve.objects.count(), 1)


@override_settings(**SETTINGS)
class CompareTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        seed(ag7020, "AG-7020", 15)
        seed(agfrc70, "AG-FRC 70", 15, end="10:20:00", first_day=date(2026, 10, 1))
        iot_standards.rebuild_all()

    def test_normal_batch_is_ok(self) -> None:
        batch = make_detected_batch(ag7020, "11:01", "11:16:30", day=NEW_DAY, compound="AG-7020")
        self.assertEqual(batch.result, DetectedBatch.RESULT_OK, batch.reasons)
        self.assertGreaterEqual(batch.match_pct, 85)
        self.assertEqual(batch.reasons, [])

    def test_twenty_percent_more_energy_is_a_major_variation(self) -> None:
        batch = make_detected_batch(ag7020, "11:01", "11:16:30", day=NEW_DAY, compound="AG-7020", scale=1.21)
        self.assertEqual(batch.result, DetectedBatch.RESULT_MAJOR)
        self.assertGreater(batch.energy_dev_pct, 20)
        self.assertIn(f"Energy {batch.energy_dev_pct:+.0f} %", batch.reasons)
        self.assertTrue(batch.out_of_band)

    def test_four_minute_longer_mix_is_a_major_variation(self) -> None:
        batch = make_detected_batch(ag7020_long, "11:01", "11:20:30", day=NEW_DAY, compound="AG-7020")
        self.assertEqual(batch.result, DetectedBatch.RESULT_MAJOR)
        self.assertIn("Mixing 4 min longer than standard", batch.reasons)
        self.assertTrue(any(reason.startswith("Energy +") for reason in batch.reasons))
        self.assertEqual(batch.duration_dev_s, 240)

    def test_late_loading_peak_is_a_variation_with_its_reason(self) -> None:
        batch = make_detected_batch(ag7020_late, "11:01", "11:18:30", day=NEW_DAY, compound="AG-7020")
        self.assertIn(batch.result, {DetectedBatch.RESULT_VARIATION, DetectedBatch.RESULT_MAJOR})
        self.assertIn("Loading peak 2 min late", batch.reasons)

    def test_low_steady_current(self) -> None:
        def weak(t: int, duration: int) -> float:
            value = ag7020(t, duration)
            return value - 8 if 140 <= t < 600 else value  # covers the 30-60 % steady window

        batch = make_detected_batch(weak, "11:01", "11:16:30", day=NEW_DAY, compound="AG-7020")
        self.assertIn("Steady current 8 A low", batch.reasons)
        self.assertNotEqual(batch.result, DetectedBatch.RESULT_OK)

    def test_wrong_compound_selection_is_flagged(self) -> None:
        batch = make_detected_batch(agfrc70, "12:24", "12:44", day=NEW_DAY, compound="AG-7020")
        self.assertEqual(batch.looks_like, "AG-FRC 70")
        self.assertIn("Looks like AG-FRC 70, check compound selection", batch.reasons)
        self.assertEqual(batch.result, DetectedBatch.RESULT_MAJOR)

    def test_not_logged_batch_gets_no_result_but_a_pattern_hint(self) -> None:
        batch = make_detected_batch(agfrc70, "12:24", "12:44", day=NEW_DAY, compound="")
        self.assertEqual(batch.result, DetectedBatch.RESULT_NO_STANDARD)
        self.assertEqual(batch.looks_like, "AG-FRC 70")

from __future__ import annotations

from datetime import date

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from compounding import iot_patterns, iot_standards
from compounding.models import DetectedBatch, ProcessGroup, ProcessGroupCompound
from compounding.tests.iot_fixtures import agfrc70, ag7020, make_detected_batch
from compounding.tests.test_iot_standards import SETTINGS, seed


def ag7020_late(t: int, duration: int) -> float:
    """AG-7020 whose loading spike comes 2 minutes late (the mill ran near-empty first)."""
    return 52.0 if t < 120 else ag7020(t - 120, duration - 120)


def flat_overload(t: int, duration: int) -> float:
    return 130.0


class DtwTests(SimpleTestCase):
    def test_identical_and_shifted_curves(self) -> None:
        curve = [100, 110, 90, 73, 73, 74, 72, 60, 50]
        self.assertEqual(iot_patterns.dtw_distance(curve, curve), 0)
        shifted = [100, 100] + curve
        self.assertLess(iot_patterns.dtw_distance(shifted, curve), 2)
        self.assertGreater(iot_patterns.dtw_distance([40] * 9, curve), 20)


@override_settings(**SETTINGS)
class PredictionTests(TestCase):
    def setUp(self) -> None:
        cache.clear()

    def _standards(self, frc_count: int = 15) -> None:
        seed(ag7020, "AG-7020", 15)
        seed(agfrc70, "AG-FRC 70", frc_count, minutes="20:00", first_day=date(2026, 10, 1))
        iot_standards.rebuild_all()

    def test_nearest_standard_by_dtw_with_alternatives(self) -> None:
        self._standards(frc_count=15)
        with self.settings(IOT_KNN_MIN_PER_COMPOUND=100):  # keep kNN out of this test
            ag = make_detected_batch(ag7020, "11:01", "11:16:30", day=date(2026, 12, 1))
            frc = make_detected_batch(agfrc70, "12:24", "12:44", day=date(2026, 12, 1))
        self.assertEqual((ag.predicted_label, ag.prediction_method), ("AG-7020", "dtw"))
        self.assertGreater(ag.prediction_confidence, 80)
        self.assertEqual(ag.prediction_alternatives[0]["label"], "AG-FRC 70")
        self.assertEqual(frc.predicted_label, "AG-FRC 70")

    def test_knn_takes_over_with_fifteen_labelled_batches_each(self) -> None:
        self._standards(frc_count=15)
        ag = make_detected_batch(ag7020, "11:01", "11:16:30", day=date(2026, 12, 1), scale=1.02)
        frc = make_detected_batch(agfrc70, "12:24", "12:44", day=date(2026, 12, 1))
        self.assertEqual((ag.predicted_label, ag.prediction_method), ("AG-7020", "knn"))
        self.assertEqual((frc.predicted_label, frc.prediction_method), ("AG-FRC 70", "knn"))
        self.assertGreaterEqual(frc.prediction_confidence, 80)

    def test_unknown_pattern(self) -> None:
        self._standards(frc_count=15)
        with self.settings(IOT_KNN_MIN_PER_COMPOUND=100):
            odd = make_detected_batch(flat_overload, "13:00", "13:15", day=date(2026, 12, 1))
        self.assertEqual(odd.predicted_label, "")

    def test_prediction_is_at_process_group_level(self) -> None:
        group = ProcessGroup.objects.create(name="Spike NBR")
        ProcessGroupCompound.objects.create(process_group=group, compound_code="AG-7020")
        self._standards(frc_count=3)
        batch = make_detected_batch(ag7020, "11:01", "11:16:30", day=date(2026, 12, 1))
        self.assertEqual(batch.predicted_label, "Spike NBR")
        self.assertEqual(batch.predicted_key, f"group:{group.pk}")


@override_settings(**SETTINGS)
class JudgementTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        seed(ag7020, "AG-7020", 15)
        iot_standards.rebuild_all()

    def test_normal_batch_matches(self) -> None:
        batch = make_detected_batch(ag7020, "11:01", "11:16:30", day=date(2026, 12, 1), tag="AG-7020")
        self.assertEqual(batch.match_result, DetectedBatch.MATCH_OK)
        self.assertGreaterEqual(batch.match_pct, 85)
        self.assertLess(abs(batch.energy_dev_pct), 5)
        self.assertEqual(batch.reasons, [])
        self.assertEqual(batch.judged_label, "AG-7020")

    def test_heavier_batch_does_not_match_and_says_why(self) -> None:
        batch = make_detected_batch(ag7020, "11:01", "11:16:30", day=date(2026, 12, 1), tag="AG-7020", scale=1.15)
        self.assertEqual(batch.match_result, DetectedBatch.MATCH_NO)
        self.assertIn("energy +15 %", batch.reasons)
        self.assertLessEqual(len(batch.reasons), 2)

    def test_late_spike_is_a_reason(self) -> None:
        batch = make_detected_batch(ag7020_late, "11:01", "11:18:30", day=date(2026, 12, 1), tag="AG-7020")
        self.assertIn("loading spike 2 min late", batch.reasons)
        self.assertEqual(batch.peak_time_dev_s // 60, 2)

    def test_no_standard_yet(self) -> None:
        batch = make_detected_batch(agfrc70, "12:24", "12:44", day=date(2026, 12, 1), tag="AG-FRC 70")
        self.assertEqual(batch.match_result, DetectedBatch.MATCH_NO_STANDARD)
        self.assertIsNone(batch.match_pct)

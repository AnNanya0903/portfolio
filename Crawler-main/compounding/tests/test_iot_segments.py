from __future__ import annotations

from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from compounding import iot_samples, iot_segments
from compounding.models import IST_TIMEZONE, DetectedBatch, MachineSegment
from compounding.tests.iot_fixtures import DAY, agfrc70, ag7020, as_rows, build_payloads, idle, ist_ms, off

MACHINE = "mixing-mill-2"
SETTINGS = dict(IOT_MACHINE_ID_DEFAULT=MACHINE, IOT_MACHINE_ID_SOURCE="extrusion-mill-1", MIN_BATCH_SECONDS=480)


def hhmm(ts_ms: int) -> str:
    return iot_segments.ms_to_datetime(ts_ms).astimezone(IST_TIMEZONE).strftime("%H:%M")


def minutes_apart(ts_ms: int, expected: str) -> float:
    return abs(ts_ms - ist_ms(expected)) / 60000


@override_settings(**SETTINGS)
class SegmentationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.rows = as_rows(build_payloads())
        cls.segments = iot_segments.segment_samples(cls.rows, machine_id=MACHINE)

    def test_finds_the_eight_batches_of_27_november(self) -> None:
        batches = [s for s in self.segments if s.state == iot_segments.BATCH]
        expected = [("11:01", "11:17"), ("11:19", "11:34"), ("11:41", "11:57"), ("11:58", "12:13"),
                    ("12:24", "12:44"), ("13:15", "13:36"), ("13:38", "13:57"), ("13:58", "14:19")]
        self.assertEqual(len(batches), 8, [(hhmm(b.start_ts), hhmm(b.end_ts)) for b in batches])
        for batch, (start, end) in zip(batches, expected):
            self.assertLessEqual(minutes_apart(batch.start_ts, start), 1.0, f"start {hhmm(batch.start_ts)} vs {start}")
            self.assertLessEqual(minutes_apart(batch.end_ts, end), 1.5, f"end {hhmm(batch.end_ts)} vs {end}")

    def test_idle_between_batches_and_off_at_lunch(self) -> None:
        between = [s for s in self.segments if s.start_ts >= ist_ms("12:12") and s.end_ts <= ist_ms("12:25")]
        self.assertEqual([s.state for s in between], [iot_segments.IDLE])
        self.assertGreaterEqual(between[0].duration_s, 9 * 60)
        lunch = [s for s in self.segments if s.state == iot_segments.OFF and s.start_ts > ist_ms("12:30")][0]
        self.assertLessEqual(minutes_apart(lunch.start_ts, "12:45"), 1.0)
        self.assertLessEqual(minutes_apart(lunch.end_ts, "13:12"), 1.0)
        self.assertEqual(self.segments[0].state, iot_segments.OFF)
        self.assertEqual(self.segments[-1].state, iot_segments.OFF)

    def test_segments_cover_the_data_without_gaps(self) -> None:
        for before, after in zip(self.segments, self.segments[1:]):
            self.assertEqual(before.end_ts, after.start_ts)
            self.assertNotEqual((before.state, after.state), (iot_segments.IDLE, iot_segments.IDLE))

    def test_short_bursts_are_not_batches(self) -> None:
        plan = [(idle, "09:50", "09:59"), (ag7020, "09:59", "10:03"), (idle, "10:03", "10:20")]
        segments = iot_segments.segment_samples(as_rows(build_payloads(plan)), machine_id=MACHINE)
        self.assertEqual([s.state for s in segments], [iot_segments.IDLE])

    def test_short_dips_stay_inside_the_batch(self) -> None:
        def with_dip(t: int, duration: int) -> float:
            return 30.0 if 400 <= t < 450 else 73.0  # a 50 s dip, no reload spike after it

        def loaded(t: int, duration: int) -> float:
            return 40.0 + t * 3 if t < 20 else 105.0 if t < 100 else with_dip(t, duration)

        plan = [(idle, "10:00", "10:05"), (loaded, "10:05", "10:20"), (idle, "10:20", "10:30")]
        segments = iot_segments.segment_samples(as_rows(build_payloads(plan)), machine_id=MACHINE)
        self.assertEqual([s.state for s in segments], ["idle", "batch", "idle"])

    def test_telemetry_gap_is_no_data(self) -> None:
        rows = as_rows(build_payloads([(idle, "10:00", "10:10")])) + as_rows(build_payloads([(idle, "10:30", "10:40")]))
        segments = iot_segments.segment_samples(rows, machine_id=MACHINE)
        self.assertEqual([s.state for s in segments], ["idle", "no_data", "idle"])

    def test_open_end_marks_the_running_batch(self) -> None:
        plan = [(idle, "10:00", "10:05"), (ag7020, "10:05", "10:09")]
        segments = iot_segments.segment_samples(as_rows(build_payloads(plan)), machine_id=MACHINE, open_end=True)
        self.assertEqual(segments[-1].state, iot_segments.BATCH)
        self.assertTrue(segments[-1].is_open)


@override_settings(**SETTINGS)
class BatchNumbersTests(SimpleTestCase):
    def test_energy_is_the_kwh_difference_inside_the_batch(self) -> None:
        rows = as_rows(build_payloads([(idle, "10:00", "10:05"), (ag7020, "10:05", "10:20:30"), (idle, "10:20:30", "10:30")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("10:05"), ist_ms("10:20:30"), MACHINE)
        self.assertGreaterEqual(numbers["energy_kwh"], 10.0)
        self.assertLessEqual(numbers["energy_kwh"], 10.5)
        self.assertGreaterEqual(numbers["peak_current"], 100)
        self.assertLess(numbers["time_to_peak_s"], 3 * 60)
        self.assertEqual(numbers["duration_s"], 930)

    def test_meter_reset_gives_no_energy(self) -> None:
        rows = [{"ts_ms": 0, "avg_current": 70.0, "t_kwh_import": 500.0},
                {"ts_ms": 2000, "avg_current": 70.0, "t_kwh_import": 0.4}]
        self.assertIsNone(iot_segments.batch_numbers(rows, 0, 4000)["energy_kwh"])

    def test_load_point_uses_the_fallback_without_a_big_peak(self) -> None:
        def low_peak(t: int, duration: int) -> float:
            return 45.0 + t if t < 30 else 77.0 if t < 120 else 69.0

        rows = as_rows(build_payloads([(low_peak, "12:24", "12:44")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("12:24"), ist_ms("12:44"), MACHINE)
        self.assertEqual(numbers["load_threshold_a"], 70.0)
        self.assertLess(numbers["peak_current"], 90)
        rows = as_rows(build_payloads([(agfrc70, "12:24", "12:44")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("12:24"), ist_ms("12:44"), MACHINE)
        self.assertEqual(numbers["load_threshold_a"], 90.0)
        self.assertGreaterEqual(numbers["peak_current"], 96)
        self.assertLessEqual(numbers["peak_current"], 107)

    def test_steady_current_and_dips(self) -> None:
        rows = as_rows(build_payloads([(agfrc70, "12:24", "12:44")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("12:24"), ist_ms("12:44"), MACHINE)
        self.assertAlmostEqual(numbers["steady_current"], 69, delta=1.5)
        rows = as_rows(build_payloads([(ag7020, "11:01", "11:16:30")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("11:01"), ist_ms("11:16:30"), MACHINE)
        self.assertAlmostEqual(numbers["steady_current"], 73, delta=2)
        self.assertGreaterEqual(numbers["dips"], 3)  # the choppy cutting phase

    def test_curve_is_every_10_s_from_the_load_point(self) -> None:
        rows = as_rows(build_payloads([(ag7020, "11:01", "11:17")]))
        numbers = iot_segments.batch_numbers(rows, ist_ms("11:01"), ist_ms("11:17"), MACHINE)
        self.assertGreater(numbers["curve"][0], 90)
        self.assertAlmostEqual(len(numbers["curve"]), (ist_ms("11:17") - numbers["load_ts"]) / 10000, delta=1)

    def test_per_machine_load_threshold(self) -> None:
        with self.settings(IOT_LOAD_THRESHOLDS={MACHINE: 100}):
            rows = as_rows(build_payloads([(ag7020, "11:01", "11:17")]))
            self.assertEqual(iot_segments.batch_numbers(rows, ist_ms("11:01"), ist_ms("11:17"), MACHINE)["load_threshold_a"], 100.0)


@override_settings(**SETTINGS)
class DetectDayTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        iot_samples.store_payloads(MACHINE, build_payloads())

    def test_detect_day_stores_batches_and_covering_segments(self) -> None:
        result = iot_segments.detect_day(MACHINE, DAY)

        self.assertEqual(result["batches"], 8)
        batches = list(DetectedBatch.objects.order_by("start_at"))
        for batch in batches[:4]:
            self.assertGreaterEqual(batch.energy_kwh, 10.0)
            self.assertLessEqual(batch.energy_kwh, 10.5)
            self.assertIsNotNone(batch.load_at)
            self.assertTrue(batch.curve)
        segments = list(MachineSegment.objects.order_by("start_at"))
        self.assertEqual(sum(1 for s in segments if s.state == "batch"), 8)
        for batch in batches[4:]:
            self.assertGreaterEqual(batch.duration_s, 19 * 60)
            self.assertLessEqual(batch.duration_s, 22 * 60)
            self.assertGreaterEqual(batch.energy_kwh, 10.5)
            self.assertLessEqual(batch.energy_kwh, 14.5)
        self.assertTrue(all(s.detected_batch_id for s in segments if s.state == "batch"))

    def test_re_running_keeps_batch_identity(self) -> None:
        iot_segments.detect_day(MACHINE, DAY)
        first_ids = list(DetectedBatch.objects.order_by("start_at").values_list("pk", flat=True))
        iot_segments.detect_day(MACHINE, DAY)
        self.assertEqual(list(DetectedBatch.objects.order_by("start_at").values_list("pk", flat=True)), first_ids)
        self.assertEqual(MachineSegment.objects.filter(state="batch").count(), 8)

    def test_backfill_command(self) -> None:
        out = StringIO()
        call_command("iot_detect_batches", "--date", DAY.isoformat(), stdout=out)
        self.assertIn("8 batches", out.getvalue())
        self.assertEqual(DetectedBatch.objects.count(), 8)


@override_settings(**SETTINGS)
class OperatorEntryLinkTests(TestCase):
    """The compound comes from the operator's entry with the largest overlap; typed times don't matter."""

    def setUp(self) -> None:
        from compounding.models import CompoundingIoTRun, CompoundingPlan

        cache.clear()
        iot_samples.store_payloads(MACHINE, build_payloads())
        plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=950)
        for compound, start, end in (
            ("AG-7020", "09:59", "10:00"),
            ("AG-7020", "11:01", "11:17"),
            ("AG-7020", "11:55", "11:57"),     # a 16 min batch logged as 1-2 min
            ("AG-FRC 70", "12:39", "12:53"),   # logged 15 min late
        ):
            CompoundingIoTRun.objects.create(
                plan=plan, machine_id=MACHINE, compound_code=compound, stock_quality="good",
                started_at=iot_segments.ms_to_datetime(ist_ms(start)),
                ended_at=iot_segments.ms_to_datetime(ist_ms(end)),
            )
        iot_segments.detect_day(MACHINE, DAY)
        self.batches = list(DetectedBatch.objects.order_by("start_at"))

    def test_compounds_come_from_the_largest_overlap(self) -> None:
        self.assertEqual(self.batches[0].compound_code, "AG-7020")
        self.assertEqual(self.batches[2].compound_code, "AG-7020")  # linked although logged as 1 min
        # The mis-tapped 1-2 min entry now carries the machine's times.
        self.assertEqual(self.batches[2].log_run.started_at, self.batches[2].start_at)
        self.assertEqual(self.batches[2].log_run.source, "corrected")
        self.assertEqual(self.batches[4].compound_code, "AG-FRC 70")
        self.assertEqual(self.batches[1].compound_code, "")
        self.assertEqual(self.batches[1].compound_label, "Not logged")

    def test_one_minute_entries_create_no_batches(self) -> None:
        self.assertEqual(len(self.batches), 8)
        self.assertGreater(self.batches[0].start_at, iot_segments.ms_to_datetime(ist_ms("10:30")))

    def test_machine_times_are_kept(self) -> None:
        self.assertLessEqual(minutes_apart(int(self.batches[2].start_at.timestamp() * 1000), "11:41"), 1.0)

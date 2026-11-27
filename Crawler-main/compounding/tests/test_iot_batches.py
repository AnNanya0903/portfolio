from __future__ import annotations

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_batches, iot_samples
from compounding.models import IST_TIMEZONE, CompoundingIoTRun, CompoundingMBPlan, CompoundingPlan
from dashboard.models import UserServiceAccess

MACHINE = "mixing-mill-2"
SETTINGS = dict(
    DEBUG=False,
    IOT_PORTAL_URL="https://iot.magicwand.biz",
    IOT_SERVICE_TOKEN="bridge-token",
    IOT_MACHINE_ID_DEFAULT=MACHINE,
    IOT_MACHINE_ID_SOURCE="extrusion-mill-1",
    MIN_BATCH_SECONDS=480,
)


def ist(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 27, hour, minute, second, tzinfo=IST_TIMEZONE)


def ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def batch_profile(start: datetime, *, minutes: float = 15.5, kwh_start: float = 1000.0, idle_before_s: int = 120,
                  idle_after_s: int = 150, energy_kwh: float = 10.2) -> list:
    """Samples every 2 s shaped like today's AG-7020 batches.

    Idle (~8 A) -> ramp to ~60 A as the mill takes the rubber -> loading spike 100-117 A for ~3 min ->
    plateau ~73 A -> dips to 45-60 A at the end -> idle again. Cumulative kWh rises by `energy_kwh` over the
    working part, so the batch summary should report that difference.
    """
    samples = []
    t0 = ms(start) - idle_before_s * 1000
    work_s = int(minutes * 60)
    total_s = idle_before_s + work_s + idle_after_s
    kwh = kwh_start
    per_step_kwh = energy_kwh / (work_s / 2)
    for step in range(0, total_s, 2):
        ts = t0 + step * 1000
        offset = step - idle_before_s
        if offset < 0 or offset >= work_s:
            current = 8.0 + (step % 6) * 0.3
        elif offset < 50:
            current = 55.0 + offset * 0.2
        elif offset < 50 + 180:
            current = 100.0 + ((offset // 2 * 7) % 18)  # 100-117 A
        elif offset < work_s - 120:
            current = 73.0 + ((offset // 2 * 3) % 5) - 2
        else:
            current = 45.0 + ((offset // 2 * 5) % 16)  # 45-60 A
        if 0 <= offset < work_s:
            kwh += per_step_kwh
        samples.append({
            "machineId": "extrusion-mill-1",
            "ts": ts,
            "registers": [],
            "raw": {"avg_current": round(current, 2), "t_kwh_import": round(kwh, 4), "t_watt": current * 560},
        })
    return samples


class BatchSummaryTests(TestCase):
    def test_summary_numbers_come_from_the_samples(self) -> None:
        start = ist(10, 11)
        raw = batch_profile(start, energy_kwh=10.2, idle_before_s=0, idle_after_s=0)
        samples = [{"ts_ms": item["ts"], "avg_current": item["raw"]["avg_current"],
                    "t_kwh_import": item["raw"]["t_kwh_import"]} for item in raw]
        end_ms = samples[-1]["ts_ms"]

        summary = iot_batches.compute_summary(samples, ms(start), end_ms)

        self.assertAlmostEqual(summary["energy_kwh"], 10.2, delta=0.05)
        self.assertEqual(summary["peak_current"], 117.0)
        self.assertGreater(summary["avg_current"], 68)
        self.assertLess(summary["avg_current"], 80)
        # The spike starts 50 s in (first sample above 90 A) and lasts ~3 min.
        self.assertEqual(summary["load_start_ts"], ms(start) + 50_000)
        self.assertAlmostEqual(summary["loading_spike_duration_s"], 180, delta=6)
        self.assertLess(summary["time_to_peak_s"], 4 * 60)
        self.assertEqual(summary["duration_s"], round((end_ms - ms(start)) / 1000))

    def test_meter_reset_gives_no_energy(self) -> None:
        samples = [{"ts_ms": 0, "avg_current": 70.0, "t_kwh_import": 500.0},
                   {"ts_ms": 2000, "avg_current": 70.0, "t_kwh_import": 0.4}]
        self.assertIsNone(iot_batches.compute_summary(samples, 0, 2000)["energy_kwh"])


@override_settings(**SETTINGS)
class BatchValidityTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(username="mill-operator", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date="2026-11-27", cjc_number=901, created_by=self.user)
        CompoundingMBPlan.objects.create(plan=self.plan, compound_code="AG-7020", total_quantity=100)

    def _run(self, start: datetime, end: datetime) -> CompoundingIoTRun:
        return CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id=MACHINE, compound_code="AG-7020", started_at=start, ended_at=end
        )

    def test_one_minute_batch_is_invalid(self) -> None:
        run = iot_batches.finalize_batch(self._run(ist(9, 59), ist(10, 0)))
        self.assertEqual(run.status, CompoundingIoTRun.STATUS_INVALID)
        self.assertIn("Shorter than 8 min", run.invalid_reason)
        self.assertFalse(run.counts_in_statistics)

    def test_full_batch_is_completed_with_summary(self) -> None:
        iot_samples.store_payloads(MACHINE, batch_profile(ist(10, 11)))
        run = iot_batches.finalize_batch(self._run(ist(10, 11), ist(10, 11) + timedelta(minutes=15.5)))
        self.assertEqual(run.status, CompoundingIoTRun.STATUS_COMPLETED)
        self.assertAlmostEqual(run.energy_kwh, 10.2, delta=0.1)
        self.assertEqual(run.peak_current, 117.0)
        self.assertIsNotNone(run.load_start_ts)

    def test_new_batches_start_with_pending_lab_result_and_no_grade(self) -> None:
        run = self._run(ist(11, 0), ist(11, 15))
        self.assertEqual(run.lab_result, CompoundingIoTRun.LAB_PENDING)
        self.assertEqual(run.stock_quality, "")

    def test_end_batch_needs_no_quality_and_marks_short_batches_invalid(self) -> None:
        self.client.post(reverse("compounding:start_iot_batch"), {"plan_date": "2026-11-27", "compound_code": "AG-7020"})
        run = CompoundingIoTRun.objects.get(ended_at__isnull=True)
        self.assertEqual(run.status, CompoundingIoTRun.STATUS_RUNNING)

        response = self.client.post(
            reverse("compounding:end_iot_batch"), {"plan_date": "2026-11-27", "run_id": run.pk}
        )

        self.assertEqual(response.status_code, 302)
        run.refresh_from_db()
        self.assertIsNotNone(run.ended_at)
        self.assertEqual(run.stock_quality, "")
        self.assertEqual(run.lab_result, CompoundingIoTRun.LAB_PENDING)
        self.assertEqual(run.status, CompoundingIoTRun.STATUS_INVALID)

    def test_hand_logged_run_without_quality_is_saved(self) -> None:
        self.client.post(
            reverse("compounding:save_iot_run"),
            {"plan_date": "2026-11-27", "compound_code": "AG-7020",
             "started_at": "2026-11-27T10:30", "ended_at": "2026-11-27T10:46"},
        )
        run = CompoundingIoTRun.objects.get(compound_code="AG-7020")
        self.assertEqual(run.status, CompoundingIoTRun.STATUS_COMPLETED)
        self.assertEqual(run.duration_s, 16 * 60)
        self.assertEqual(run.stock_quality, "")


def day_of_samples() -> list:
    """Four logged AG-7020 batches and one run nobody logged, as described for 27 Nov 2026."""
    samples = []
    plan = ((ist(10, 11), 15.5), (ist(10, 30), 15.5), (ist(11, 1), 14), (ist(11, 17), 15.5), (ist(11, 40), 15))
    for index, (start, minutes) in enumerate(plan):
        # Short idle tails so one batch's idle samples never overlap the next batch's start; the next
        # batch's idle lead-in completes the 90 s of idle. The day's last batch gets a full tail.
        tail = 150 if index == len(plan) - 1 else 60
        samples += batch_profile(start, minutes=minutes, energy_kwh=10.2, idle_after_s=tail)
    return samples


def stored_rows(raw: list) -> list:
    return [{"ts_ms": item["ts"], "avg_current": item["raw"]["avg_current"]} for item in raw]


class DetectRunsTests(TestCase):
    def test_finds_every_run_of_the_day(self) -> None:
        runs = iot_batches.detect_runs(stored_rows(day_of_samples()))

        starts = [iot_batches.ms_to_datetime(run.start_ts).astimezone(IST_TIMEZONE).strftime("%H:%M") for run in runs]
        self.assertEqual(starts, ["10:11", "10:30", "11:01", "11:17", "11:40"])
        self.assertTrue(all(run.finished for run in runs))

    def test_short_spikes_do_not_start_a_run(self) -> None:
        rows = [{"ts_ms": i * 2000, "avg_current": 80.0 if 10 <= i < 30 else 8.0} for i in range(200)]  # 40 s
        self.assertEqual(iot_batches.detect_runs(rows), [])

    def test_a_run_still_on_the_machine_has_no_end(self) -> None:
        rows = [{"ts_ms": i * 2000, "avg_current": 8.0 if i < 10 else 70.0} for i in range(200)]
        runs = iot_batches.detect_runs(rows)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].start_ts, 20_000)
        self.assertIsNone(runs[0].end_ts)

    def test_a_telemetry_gap_ends_the_run(self) -> None:
        rows = [{"ts_ms": i * 2000, "avg_current": 70.0} for i in range(100)]
        rows += [{"ts_ms": 400_000 + i * 2000, "avg_current": 8.0} for i in range(100)]
        runs = iot_batches.detect_runs(rows)
        self.assertEqual(runs[0].end_ts, 198_000)

    def test_thresholds_come_from_settings(self) -> None:
        rows = [{"ts_ms": i * 2000, "avg_current": 35.0} for i in range(100)]
        self.assertEqual(iot_batches.detect_runs(rows), [])
        with self.settings(IOT_BATCH_START_THRESHOLD_A=30):
            self.assertEqual(len(iot_batches.detect_runs(rows)), 1)


@override_settings(**SETTINGS)
class ReplayDayTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(username="replay-operator", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date="2026-11-27", cjc_number=902, created_by=self.user)
        iot_samples.store_payloads(MACHINE, day_of_samples())

    def _logged(self, pressed_start: datetime, pressed_end: datetime) -> CompoundingIoTRun:
        return CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id=MACHINE, compound_code="AG-7020", started_at=pressed_start,
            ended_at=pressed_end, stock_quality="", detection_source=CompoundingIoTRun.SOURCE_MANUAL,
        )

    def _replay(self):
        day = ms(ist(0, 0))
        return iot_batches.process_machine(MACHINE, start_ts=day, end_ts=day + 86_400_000 - 1)

    def test_replay_attaches_logged_batches_and_flags_the_unlogged_run(self) -> None:
        one_minute = iot_batches.finalize_batch(self._logged(ist(9, 59), ist(10, 0)))
        # Operators tapped Start a little before the mill took the rubber, and End a little late.
        for hour, minute in ((10, 10), (10, 29), (11, 0), (11, 16)):
            self._logged(ist(hour, minute), ist(hour, minute) + timedelta(minutes=17))

        self._replay()

        logged = list(
            CompoundingIoTRun.objects.filter(compound_code="AG-7020", run_detected=True).order_by("started_at")
        )
        self.assertEqual(
            [run.started_at.astimezone(IST_TIMEZONE).strftime("%H:%M") for run in logged],
            ["10:11", "10:30", "11:01", "11:17"],
        )
        for run in logged:
            self.assertEqual(run.detection_source, CompoundingIoTRun.SOURCE_MANUAL)
            self.assertEqual(run.status, CompoundingIoTRun.STATUS_COMPLETED)
            self.assertGreaterEqual(run.energy_kwh, 10.0)
            self.assertLessEqual(run.energy_kwh, 10.5)
            self.assertIsNotNone(run.load_start_ts)

        unlogged = CompoundingIoTRun.objects.get(detection_source=CompoundingIoTRun.SOURCE_AUTO)
        self.assertEqual(unlogged.compound_code, CompoundingIoTRun.UNASSIGNED_COMPOUND)
        self.assertEqual(unlogged.started_at.astimezone(IST_TIMEZONE).strftime("%H:%M"), "11:40")
        self.assertEqual(unlogged.status, CompoundingIoTRun.STATUS_COMPLETED)

        one_minute.refresh_from_db()
        self.assertEqual(one_minute.status, CompoundingIoTRun.STATUS_INVALID)
        self.assertFalse(one_minute.run_detected)

    def test_replaying_twice_changes_nothing(self) -> None:
        self._replay()
        first = sorted(CompoundingIoTRun.objects.values_list("started_at", "ended_at", "energy_kwh"))
        self._replay()
        self.assertEqual(sorted(CompoundingIoTRun.objects.values_list("started_at", "ended_at", "energy_kwh")), first)
        self.assertEqual(CompoundingIoTRun.objects.count(), 5)


@override_settings(**SETTINGS)
class UnloggedRunTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = get_user_model().objects.create_user(username="late-operator", password="secret123")
        access, _ = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = ["compounding_board"]
        access.save()
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date="2026-11-27", cjc_number=903, created_by=self.user)
        CompoundingMBPlan.objects.create(plan=self.plan, compound_code="AG-7020", total_quantity=100)
        # Five minutes of a batch on the mill, ending "now".
        self.now = ist(11, 45)
        start = ms(self.now) - 300_000
        iot_samples.store_payloads(
            MACHINE,
            [{"ts": start + i * 2000, "raw": {"avg_current": 73.0, "t_kwh_import": 100 + i * 0.01}} for i in range(151)],
        )

    def test_running_machine_without_a_logged_batch_shows_the_banner_until_start_is_tapped(self) -> None:
        state = iot_batches.machine_state(MACHINE, now_ms=ms(self.now))
        self.assertTrue(state["machineRunning"])
        self.assertTrue(state["showUnloggedBanner"])

        iot_batches.process_machine(MACHINE, start_ts=ms(self.now) - 600_000, end_ts=ms(self.now))
        auto = CompoundingIoTRun.objects.get()
        self.assertEqual(auto.status, CompoundingIoTRun.STATUS_RUNNING)
        self.assertEqual(auto.compound_code, CompoundingIoTRun.UNASSIGNED_COMPOUND)

        response = self.client.post(
            reverse("compounding:start_iot_batch"), {"plan_date": "2026-11-27", "compound_code": "AG-7020"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(CompoundingIoTRun.objects.count(), 1)
        auto.refresh_from_db()
        self.assertEqual(auto.compound_code, "AG-7020")
        self.assertEqual(auto.detection_source, CompoundingIoTRun.SOURCE_MANUAL)
        self.assertFalse(iot_batches.machine_state(MACHINE, now_ms=ms(self.now))["showUnloggedBanner"])

    def test_banner_is_in_the_dashboard_data(self) -> None:
        from unittest.mock import patch

        with patch("compounding.iot_samples.sync_recent"), patch(
            "compounding.iot_batches.timezone.now", return_value=self.now
        ), patch("magicwand.views.time.time", return_value=ms(self.now) / 1000):
            response = self.client.get(reverse("iot_dashboard_data"))
        self.assertTrue(response.json()["batchState"]["showUnloggedBanner"])

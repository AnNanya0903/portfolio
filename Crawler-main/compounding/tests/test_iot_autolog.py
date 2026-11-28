from __future__ import annotations

from datetime import date

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_samples, iot_segments
from compounding.iot_segments import ms_to_datetime
from compounding.models import (
    IST_TIMEZONE,
    CompoundingIoTRun as Run,
    CompoundingLine,
    CompoundingPlan,
    DetectedBatch,
    IoTRunAudit,
)
from compounding.tests.iot_fixtures import ag7020, build_payloads, idle, ist_ms, off
from compounding.tests.test_iot_daily import SETTINGS, user_with

MACHINE = "mixing-mill-2"
DAY = date(2026, 11, 28)


def running_already(t: int, duration: int) -> float:
    return 72.0 if t < duration - 60 else 48.0


def mixing(t: int, duration: int) -> float:
    """A batch with a one-minute dip to 39 A in the middle."""
    if 660 <= t < 720:
        return 39.0
    return ag7020(t, duration)


# 07:37 already running; three batches that never drop to idle; idle; off; then three more.
PLAN = [
    (running_already, "07:37", "07:43"),
    (ag7020, "07:43", "08:01"),
    (ag7020, "08:01", "08:22"),
    (ag7020, "08:22", "08:40"),
    (idle, "08:40", "08:48"),
    (off, "08:48", "09:20"),
    (idle, "09:20", "09:23"),
    (mixing, "09:23", "09:42"),
    (ag7020, "09:42", "10:00"),
    (ag7020, "10:00", "10:18"),
    (idle, "10:18", "10:25"),
]


def clock(value) -> str:
    return value.astimezone(IST_TIMEZONE).strftime("%H:%M")


@override_settings(**SETTINGS)
class AutoLogTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=970)
        iot_samples.store_payloads(MACHINE, build_payloads(PLAN, day=DAY))

    def manual(self, start, end, compound="AG-PRX 70", **extra):
        return Run.objects.create(
            plan=self.plan, machine_id=MACHINE, compound_code=compound,
            started_at=ms_to_datetime(ist_ms(start, DAY)), ended_at=ms_to_datetime(ist_ms(end, DAY)), **extra,
        )

    def board(self, compound, fb=5):
        return CompoundingLine.objects.create(plan=self.plan, compound_code=compound, shift1_fb=fb)

    def detect(self):
        iot_segments.detect_day(MACHINE, DAY)
        return list(DetectedBatch.objects.order_by("start_at"))

    def assert_near(self, value, expected):
        self.assertLessEqual(abs(value.timestamp() * 1000 - ist_ms(expected, DAY)), 90_000, f"{clock(value)} != {expected}")

    def test_loading_spikes_split_batches_and_short_dips_do_not(self) -> None:
        batches = self.detect()
        self.assertEqual([b.is_partial for b in batches], [True] + [False] * 6,
                         [(clock(b.start_at), clock(b.end_at)) for b in batches])
        expected = [("07:43", "08:01"), ("08:01", "08:22"), ("08:22", "08:40"),
                    ("09:23", "09:42"), ("09:42", "10:00"), ("10:00", "10:18")]
        for batch, (start, end) in zip(batches[1:], expected):
            self.assert_near(batch.start_at, start)
            self.assert_near(batch.end_at, end)

    def test_every_batch_is_logged_pending_and_partial_is_not(self) -> None:
        self.detect()
        runs = list(Run.objects.order_by("started_at"))
        self.assertEqual(len(runs), 6)
        for run in runs:
            self.assertEqual((run.source, run.compound_code, run.stock_quality), ("iot_auto", Run.UNASSIGNED, ""))
            self.assertEqual(run.notes, "Logged from IoT graph")
            self.assertEqual(run.started_at, run.detected_batch.start_at)
            self.assertFalse(run.is_confirmed)

    def test_one_entry_covering_three_batches_is_split_and_rerun_adds_nothing(self) -> None:
        original = self.manual("07:39", "08:40", stock_quality="ok", notes="campaign")
        self.detect()
        split = list(Run.objects.filter(source="split").order_by("started_at"))
        self.assertEqual(len(split), 3)
        self.assertEqual(split[0].pk, original.pk)
        self.assertEqual({(r.compound_code, r.stock_quality, r.notes) for r in split},
                         {("AG-PRX 70", "ok", "campaign")})
        self.assertEqual(IoTRunAudit.objects.filter(action="split").count(), 3)
        before = list(Run.objects.order_by("pk").values_list("pk", "started_at", "ended_at", "source"))
        self.detect()
        self.detect()
        self.assertEqual(list(Run.objects.order_by("pk").values_list("pk", "started_at", "ended_at", "source")), before)
        self.assertEqual(Run.objects.count(), 6)
        self.assertEqual(DetectedBatch.objects.filter(log_run__isnull=False).count(), 6)

    def test_operator_compound_wins_over_the_single_planned_one(self) -> None:
        self.board("AG-PRX 70", fb=10)
        self.manual("09:24", "09:41", compound="AG-OTHER")
        self.detect()
        operator = Run.objects.get(compound_code="AG-OTHER")
        self.assertFalse(operator.compound_needs_confirm)
        others = list(Run.objects.exclude(pk=operator.pk))
        self.assertEqual(len(others), 5)
        self.assertTrue(all(r.compound_code == "AG-PRX 70" and r.compound_needs_confirm for r in others))

    def test_two_planned_compounds_leave_it_unassigned(self) -> None:
        self.board("AG-PRX 70")
        self.board("AG-7020")
        self.detect()
        self.assertEqual(set(Run.objects.values_list("compound_code", flat=True)), {Run.UNASSIGNED})

    def test_mistap_is_corrected_and_entry_without_a_run_is_flagged(self) -> None:
        mistap = self.manual("09:55", "09:56")
        nothing = self.manual("08:55", "08:56")
        self.detect()
        mistap.refresh_from_db()
        nothing.refresh_from_db()
        self.assertEqual(mistap.source, "corrected")
        self.assertEqual(mistap.started_at, mistap.detected_batch.start_at)
        self.assertTrue(IoTRunAudit.objects.filter(run=mistap, action="corrected").exists())
        self.assertEqual(nothing.flag, "no_machine_run")
        self.assertFalse(nothing.is_confirmed)

    def test_confirm_counts_on_the_board_and_delete_is_not_recreated(self) -> None:
        self.board("AG-PRX 70")
        self.detect()
        run = Run.objects.order_by("started_at").first()
        self.client.force_login(user_with(["compounding_board"], "operator"))
        board_url = reverse("compounding:display") + f"?date={DAY.isoformat()}"
        self.assertNotContains(self.client.get(board_url), "confirmed on the mill")
        self.client.post(reverse("compounding:iot_run_confirm", args=[run.pk]))
        run.refresh_from_db()
        self.assertTrue(run.is_confirmed)
        self.assertContains(self.client.get(board_url), "1 confirmed on the mill")
        page = self.client.get(reverse("compounding:iot_dashboard"), {"date": DAY.isoformat()})
        self.assertContains(page, 'id="iotRunsTable"')
        self.assertContains(page, "please confirm")

        delete_url = reverse("compounding:iot_run_delete", args=[run.pk])
        self.client.post(delete_url, {"confirm_delete": "1"})
        self.assertTrue(Run.objects.filter(pk=run.pk).exists())  # operators cannot delete
        self.client.force_login(user_with(["compounding_planning"], "supervisor"))
        self.client.post(delete_url, {"confirm_delete": "1"})
        self.assertFalse(Run.objects.filter(pk=run.pk).exists())
        self.detect()
        self.assertEqual(Run.objects.count(), 5)

    def test_edit_sets_quality_but_not_times(self) -> None:
        self.detect()
        run = Run.objects.order_by("started_at").last()
        times = (run.started_at, run.ended_at)
        self.client.force_login(user_with(["compounding_board"], "editor"))
        url = reverse("compounding:iot_run_edit", args=[run.pk])
        self.assertContains(self.client.get(url), "times from the machine")
        self.client.post(url, {"compound_code": Run.UNASSIGNED, "stock_quality": "good",
                               "started_at": "2026-11-28T01:00"})
        run.refresh_from_db()
        self.assertEqual(run.stock_quality, "good")
        self.assertEqual((run.started_at, run.ended_at), times)
        self.assertContains(self.client.get(url), "edited by editor")

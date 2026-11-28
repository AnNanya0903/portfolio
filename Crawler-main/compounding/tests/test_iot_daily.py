from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_daily, iot_samples, iot_segments, iot_standards
from compounding.iot_segments import ms_to_datetime
from compounding.models import CompoundingIoTRun, CompoundingPlan, DetectedBatch
from compounding.tests.iot_fixtures import DAY, ag7020, agfrc70, build_payloads, ist_ms, make_detected_batch
from compounding.tests.test_iot_compare import seed
from dashboard.models import UserServiceAccess

MACHINE = "mixing-mill-2"
SETTINGS = dict(
    DEBUG=False,
    IOT_PORTAL_URL="https://iot.magicwand.biz",
    IOT_SERVICE_TOKEN="bridge-token",
    IOT_MACHINE_ID_DEFAULT=MACHINE,
    IOT_MACHINE_ID_SOURCE="extrusion-mill-1",
    MIN_BATCH_SECONDS=480,
    IOT_STANDARD_MIN_BATCHES=15,
)
ENTRIES = (
    ("AG-7020", "09:59", "10:00"),
    ("AG-7020", "11:01", "11:17"),
    ("AG-7020", "11:19", "11:34"),
    ("AG-7020", "11:55", "11:57"),
    ("AG-7020", "11:58", "12:13"),
    ("AG-FRC 70", "12:39", "12:53"),
    ("AG-FRC 70", "13:15", "13:36"),
    ("AG-FRC 70", "13:38", "13:57"),
    ("AG-FRC 70", "13:58", "14:19"),
)


def user_with(services, username):
    user = get_user_model().objects.create_user(username=username, password="secret123")
    access, _ = UserServiceAccess.objects.get_or_create(user=user)
    access.restrict = True
    access.allowed_services = services
    access.save()
    return user


@override_settings(**SETTINGS)
class DailyViewTests(TestCase):
    """27 Nov 2026 as the acceptance describes it, with standards built from earlier days."""

    def setUp(self) -> None:
        cache.clear()
        seed(ag7020, "AG-7020", 15, first_day=date(2026, 10, 1))
        seed(agfrc70, "AG-FRC 70", 15, end="10:20:00", first_day=date(2026, 11, 1))
        iot_standards.rebuild_all()
        plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=960)
        for compound, start, end in ENTRIES:
            CompoundingIoTRun.objects.create(
                plan=plan, machine_id=MACHINE, compound_code=compound, stock_quality="good",
                started_at=ms_to_datetime(ist_ms(start)), ended_at=ms_to_datetime(ist_ms(end)),
            )
        iot_samples.store_payloads(MACHINE, build_payloads())
        iot_segments.detect_day(MACHINE, DAY)
        self.day = iot_daily.build_day(MACHINE, DAY)

    def test_compounds_and_batch_counts(self) -> None:
        self.assertEqual(self.day["total"], 8)
        sections = {section["compound"]: section for section in self.day["sections"]}
        self.assertEqual(list(sections), ["AG-7020", "AG-FRC 70"])
        ag, frc = sections["AG-7020"], sections["AG-FRC 70"]
        self.assertEqual([row["start"][:4] for row in ag["rows"]], ["11:0", "11:1", "11:4", "11:5"])
        self.assertEqual([row["start"][:4] for row in frc["rows"]], ["12:2", "13:1", "13:3", "13:5"])
        self.assertGreaterEqual(ag["avg_kwh"], 10.0)
        self.assertLessEqual(ag["avg_kwh"], 10.5)
        for row in frc["rows"]:
            self.assertGreaterEqual(row["minutes"], 19)
            self.assertLessEqual(row["minutes"], 22)
        self.assertEqual(sum(ag["counts"].values()), 4)
        self.assertEqual(ag["counts"]["ok"], 4, [row["reasons"] for row in ag["rows"]])
        self.assertTrue(ag["chart"]["standard"])
        self.assertEqual(len(ag["chart"]["lines"]), 4)

    def test_timeline_covers_the_day(self) -> None:
        pieces = self.day["timeline"]["pieces"]
        states = [piece["state"] for piece in pieces]
        self.assertEqual(states.count("batch"), 8)
        self.assertIn("off", states)
        self.assertAlmostEqual(sum(piece["width"] for piece in pieces), 100, delta=0.5)
        batch_colours = {piece["colour"] for piece in pieces if piece["state"] == "batch"}
        self.assertEqual(len(batch_colours), 2)  # one colour per compound

    def test_reports(self) -> None:
        csv_text = iot_daily.report_csv(self.day)
        self.assertIn("AG-7020,4,4,0,0", csv_text)
        self.assertIn("AG-FRC 70,4", csv_text)
        pdf = iot_daily.report_pdf(self.day, MACHINE)
        self.assertTrue(pdf.startswith(b"%PDF"))
        summary = iot_daily.end_of_day_summary(self.day)
        self.assertTrue(summary[0].endswith("8 batches."))

    def test_pages(self) -> None:
        self.client.force_login(user_with(["compounding_board"], "viewer"))
        page = self.client.get(reverse("compounding:iot_dashboard"), {"date": DAY.isoformat()})
        self.assertContains(page, "AG-FRC 70")
        self.assertContains(page, 'data-daily-chart="daily-chart-1"')
        partial = self.client.get(reverse("compounding:iot_daily_partial"), {"date": DAY.isoformat()})
        self.assertContains(partial, "Standard v1")
        csv_response = self.client.get(reverse("compounding:iot_daily_report", args=["csv"]), {"date": DAY.isoformat()})
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        pdf_response = self.client.get(reverse("compounding:iot_daily_report", args=["pdf"]), {"date": DAY.isoformat()})
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertEqual(self.client.get(reverse("compounding:iot_daily_report", args=["xls"])).status_code, 404)
        batch = DetectedBatch.objects.order_by("start_at").first()
        detail = self.client.get(reverse("compounding:iot_detected_batch", args=[batch.pk]))
        self.assertContains(detail, "Against the standard")
        self.assertNotContains(detail, "Leave this batch out")
        self.assertContains(self.client.get(reverse("compounding:iot_standards")), "AG-FRC 70")

    def test_admin_excludes_a_batch_and_rebuilds(self) -> None:
        batch = DetectedBatch.objects.order_by("start_at").first()
        self.client.force_login(user_with(["compounding_board"], "operator"))
        self.client.post(reverse("compounding:iot_exclude_batch", args=[batch.pk]), {"excluded": "1"})
        batch.refresh_from_db()
        self.assertFalse(batch.excluded)

        admin = user_with(["compounding_planning"], "admin")
        self.client.force_login(admin)
        self.client.post(reverse("compounding:iot_exclude_batch", args=[batch.pk]),
                         {"excluded": "1", "excluded_reason": "rubber stuck"})
        batch.refresh_from_db()
        self.assertTrue(batch.excluded)
        self.assertEqual(batch.excluded_by, admin)
        self.client.post(reverse("compounding:iot_standards"), {"compound": "AG-7020"})
        self.assertEqual(iot_standards.active_standard("AG-7020").version, 2)

    def test_major_variation_shows_in_its_section(self) -> None:
        heavy = make_detected_batch(ag7020, "15:00", "15:15:30", compound="AG-7020", scale=1.21)
        self.assertEqual(heavy.result, DetectedBatch.RESULT_MAJOR)
        section = next(s for s in iot_daily.build_day(MACHINE, DAY)["sections"] if s["compound"] == "AG-7020")
        self.assertEqual(section["counts"]["major"], 1)
        line = next(line for line in section["chart"]["lines"] if line["id"] == heavy.pk)
        self.assertEqual(line["colour"], iot_daily.RESULT_COLOURS["major"])


@override_settings(**SETTINGS)
class LiveBandTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        seed(ag7020, "AG-7020", 15, first_day=date(2026, 10, 1))
        iot_standards.rebuild_all()
        plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=961)
        CompoundingIoTRun.objects.create(plan=plan, machine_id=MACHINE, compound_code="AG-7020", stock_quality="",
                                         started_at=ms_to_datetime(ist_ms("11:00")), ended_at=None)

    def _run_until(self, profile, until: str, scale: float = 1.0):
        iot_samples.store_payloads(MACHINE, build_payloads([(profile, "10:55", "11:01"), (profile, "11:01", until)],
                                                           scale=scale))
        now = ms_to_datetime(ist_ms(until))
        with patch("compounding.iot_segments.timezone.now", return_value=now), \
                patch("compounding.iot_daily.timezone.now", return_value=now):
            iot_segments.detect_day(MACHINE, DAY)
            return iot_daily.live_status(MACHINE)

    def test_band_is_placed_from_the_loading_point(self) -> None:
        def mill(t, duration):
            return 37.0 if duration == 360 else ag7020(t, 930)

        status = self._run_until(mill, "11:08")
        self.assertEqual(status["compound"], "AG-7020")
        self.assertTrue(status["band"])
        self.assertEqual(status["band"][0]["ts"], status["loadTs"])
        self.assertEqual(status["warning"], "")

    def test_warning_when_the_current_leaves_the_band(self) -> None:
        def heavy_mill(t, duration):
            if duration == 360:
                return 37.0
            value = ag7020(t, 930)
            return value + 25 if t >= 200 else value

        status = self._run_until(heavy_mill, "11:08")
        self.assertIn("Current left the AG-7020 band", status["warning"])


@override_settings(**SETTINGS)
class LiveBandApiTests(TestCase):
    def test_dashboard_data_carries_the_live_batch(self) -> None:
        from django.test import Client

        client = Client()
        client.force_login(user_with(["compounding_board"], "live-viewer"))
        status = {"batchId": 1, "compound": "AG-7020", "band": [{"ts": 1, "lower": 60, "upper": 80, "median": 70}],
                  "warning": "Current left the AG-7020 band at 3:20 after loading", "standardReady": True}
        with patch("compounding.iot_samples.sync_recent"), patch("compounding.iot_daily.live_status", return_value=status), \
                patch("magicwand.views.requests.get") as bridge:
            bridge.return_value.status_code = 200
            bridge.return_value.json.return_value = {"ok": True, "latest": None, "history": []}
            body = client.get(reverse("iot_dashboard_data")).json()
        self.assertEqual(body["liveBatch"]["warning"], status["warning"])


@override_settings(**SETTINGS)
class AiSummaryFactsTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        seed(ag7020, "AG-7020", 15, first_day=date(2026, 10, 1))
        iot_standards.rebuild_all()
        make_detected_batch(ag7020, "11:01", "11:16:30", compound="AG-7020")
        self.heavy = make_detected_batch(ag7020, "11:20", "11:35:30", compound="AG-7020", scale=1.21)

    def test_facts_carry_numbers_results_and_reasons(self) -> None:
        facts = iot_daily.summary_facts(MACHINE, DAY)
        self.assertTrue(facts[0].startswith("Date 2026-11-27: 2 batches, 1 with a variation"))
        self.assertIn("AG-7020: 2 batches; 1 OK, 0 Variation, 1 Major variation", facts[1])
        variation = facts[2]
        self.assertIn("RESULT Major variation", variation)
        self.assertIn(self.heavy.reasons[0], variation)

    def test_prompt_marks_the_results_as_final(self) -> None:
        from magicwand.views import _build_iot_fallback_summary, _build_iot_summary_prompt

        data = {"machineId": MACHINE, "latest": None, "history": [], "dayFacts": iot_daily.summary_facts(MACHINE, DAY)}
        prompt = _build_iot_summary_prompt(data)
        self.assertIn("is final: never change it", prompt)
        self.assertIn("RESULT Major variation", prompt)
        self.assertIn("Day and variations:", _build_iot_fallback_summary(data))

from __future__ import annotations

from datetime import date

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding import iot_reconcile, iot_samples, iot_segments, iot_standards
from compounding.iot_segments import ms_to_datetime
from compounding.models import CompoundingIoTRun, CompoundingPlan, DetectedBatch, IoTRunCorrection
from compounding.tests.iot_fixtures import DAY, agfrc70, ag7020, build_payloads, ist_ms
from compounding.tests.test_iot_standards import seed
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


def user_with(services, username):
    user = get_user_model().objects.create_user(username=username, password="secret123")
    access, _ = UserServiceAccess.objects.get_or_create(user=user)
    access.restrict = True
    access.allowed_services = services
    access.save()
    return user


@override_settings(**SETTINGS)
class ReconciliationTests(TestCase):
    """The acceptance day: 27 Nov 2026 with the operator logs as they were really entered."""

    def setUp(self) -> None:
        cache.clear()
        self.plan = CompoundingPlan.objects.create(plan_date=DAY, cjc_number=940)
        iot_samples.store_payloads(MACHINE, build_payloads())
        for compound, start, end in (
            ("AG-7020", "09:59", "10:00"),     # 1-min log, the machine was idle
            ("AG-7020", "11:01", "11:17"),
            ("AG-7020", "11:19", "11:34"),
            ("AG-7020", "11:55", "11:57"),     # real batch 11:41-11:57
            ("AG-7020", "11:58", "12:13"),
            ("AG-FRC 70", "12:39", "12:53"),   # real batch 12:24-12:44
        ):
            self._log(compound, start, end)
        iot_segments.detect_day(MACHINE, DAY)

    def _log(self, compound, start, end):
        return CompoundingIoTRun.objects.create(
            plan=self.plan, machine_id=MACHINE, compound_code=compound,
            started_at=ms_to_datetime(ist_ms(start)), ended_at=ms_to_datetime(ist_ms(end)), stock_quality="good",
        )

    def _rows(self):
        return iot_reconcile.build_rows(MACHINE, DAY)

    def _row_at(self, rows, hhmm):
        return next(row for row in rows if row["start_text"][:4] == hhmm[:4] and row["kind"] != "log")

    def test_verdicts_for_the_day(self) -> None:
        rows = self._rows()
        batches = [row for row in rows if row["kind"] == "batch"]
        self.assertEqual(len(batches), 5)
        verdicts = [(row["start_text"], row["verdict"]) for row in batches]
        self.assertEqual([verdict for _start, verdict in verdicts],
                         ["matches", "matches", "mislogged", "matches", "time_mismatch"], verdicts)
        mislogged = batches[2]
        self.assertEqual(mislogged["log_text"], "AG-7020 11:55–11:57")
        self.assertIn("logged 2 min of", mislogged["reason"])
        frc = batches[4]
        self.assertIn("start +15 min", frc["reason"])
        self.assertTrue(mislogged["correctable"] and frc["correctable"])

    def test_one_minute_log_is_no_machine_run_and_creates_no_batch(self) -> None:
        rows = self._rows()
        no_run = [row for row in rows if row["verdict"] == "no_machine_run"]
        self.assertEqual(len(no_run), 1)
        self.assertEqual(no_run[0]["log_text"], "AG-7020 09:59–10:00")
        self.assertFalse(DetectedBatch.objects.filter(start_at__lt=ms_to_datetime(ist_ms("11:00"))).exists())

    def test_idle_and_off_rows_cover_the_day(self) -> None:
        rows = self._rows()
        states = [(row["start_text"], row["verdict"]) for row in rows if row["kind"] == "state"]
        self.assertEqual(states[0][1], "off")
        self.assertIn("idle", [verdict for start, verdict in states if start.startswith("12:1")])
        self.assertEqual(states[-1][1], "off")
        self.assertTrue(states[-1][0].startswith("12:4"))
        # The 2.5 min idle at 11:17 is too short for its own row.
        self.assertFalse(any(start.startswith("11:16") or start.startswith("11:17") for start, _v in states))

    def test_summary_cards(self) -> None:
        summary = iot_reconcile.summarize(self._rows())
        self.assertEqual(summary["batches"], 5)
        self.assertEqual(summary["log_matches"], 3)
        by_compound = {item["compound"]: item for item in summary["compounds"]}
        self.assertEqual(by_compound["AG-7020"]["count"], 4)
        self.assertGreaterEqual(by_compound["AG-7020"]["avg_kwh"], 10.0)
        self.assertLessEqual(by_compound["AG-7020"]["avg_kwh"], 10.5)
        self.assertEqual(by_compound["AG-FRC 70"]["count"], 1)

    def test_not_logged(self) -> None:
        CompoundingIoTRun.objects.filter(compound_code="AG-FRC 70").delete()
        iot_segments.detect_day(MACHINE, DAY)
        row = [row for row in self._rows() if row["kind"] == "batch"][4]
        self.assertEqual(row["verdict"], "not_logged")

    def test_compound_mismatch_needs_a_confident_ai(self) -> None:
        seed(ag7020, "AG-7020", 15, first_day=date(2026, 10, 1))
        seed(agfrc70, "AG-FRC 70", 15, minutes="20:00", first_day=date(2026, 11, 1))
        iot_standards.rebuild_all()
        log = CompoundingIoTRun.objects.get(compound_code="AG-FRC 70")
        log.compound_code = "AG-7020"
        log.started_at = ms_to_datetime(ist_ms("12:24"))
        log.ended_at = ms_to_datetime(ist_ms("12:44"))
        log.save()
        iot_segments.detect_day(MACHINE, DAY)
        row = [row for row in self._rows() if row["kind"] == "batch"][4]
        self.assertEqual(row["verdict"], "compound_mismatch")
        self.assertIn("AG-FRC 70", row["reason"])

    def test_accept_machine_times_corrects_the_log_with_an_audit(self) -> None:
        supervisor = user_with(["compounding_planning"], "supervisor")
        self.client.force_login(supervisor)
        row = [row for row in self._rows() if row["kind"] == "batch"][2]

        response = self.client.post(reverse("compounding:iot_accept_machine_times"),
                                    {"date": DAY.isoformat(), "batch_id": row["batch"].pk, "log_id": row["log"].pk})

        self.assertEqual(response.status_code, 302)
        log = CompoundingIoTRun.objects.get(pk=row["log"].pk)
        self.assertEqual(log.started_at, row["batch"].start_at)
        self.assertEqual(log.ended_at, row["batch"].end_at)
        correction = IoTRunCorrection.objects.get()
        self.assertEqual(correction.corrected_by, supervisor)
        self.assertEqual(correction.old_started_at, ms_to_datetime(ist_ms("11:55")))
        self.assertEqual(correction.verdict, "mislogged")
        fixed = [row for row in self._rows() if row["kind"] == "batch"][2]
        self.assertEqual(fixed["verdict"], "matches")

    def test_only_supervisors_may_accept(self) -> None:
        self.client.force_login(user_with(["compounding_board"], "operator"))
        row = [row for row in self._rows() if row["kind"] == "batch"][2]
        self.client.post(reverse("compounding:iot_accept_machine_times"),
                         {"date": DAY.isoformat(), "batch_id": row["batch"].pk, "log_id": row["log"].pk})
        self.assertFalse(IoTRunCorrection.objects.exists())

    def test_matching_rows_cannot_be_corrected(self) -> None:
        self.client.force_login(user_with(["compounding_planning"], "supervisor2"))
        row = [row for row in self._rows() if row["kind"] == "batch"][0]
        self.client.post(reverse("compounding:iot_accept_machine_times"),
                         {"date": DAY.isoformat(), "batch_id": row["batch"].pk, "log_id": row["log"].pk})
        self.assertFalse(IoTRunCorrection.objects.exists())

    def test_dashboard_table_partial_and_csv(self) -> None:
        self.client.force_login(user_with(["compounding_board"], "viewer"))
        page = self.client.get(reverse("compounding:iot_dashboard"), {"date": DAY.isoformat()})
        self.assertContains(page, 'id="iotReconTable"')
        self.assertContains(page, "Mis-logged")
        self.assertContains(page, "Time mismatch")
        self.assertNotContains(page, "Accept machine times")  # operators don't get the button

        partial = self.client.get(reverse("compounding:iot_recon_partial"), {"date": DAY.isoformat()})
        self.assertContains(partial, "No machine run")

        csv_response = self.client.get(reverse("compounding:iot_recon_csv"), {"date": DAY.isoformat()})
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        lines = csv_response.content.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith("Start,End,Duration (min),Energy (kWh)"))
        self.assertTrue(any("Mis-logged" in line for line in lines))

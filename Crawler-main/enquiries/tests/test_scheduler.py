"""The scheduled automations run by Django itself: the hourly run, its lock and log, and the cron-only URL."""
import io
import json
import pathlib
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from enquiries import auto_follow_up, scheduler, services
from enquiries.models import AutomationRun

ON = dict(FOLLOW_UP_AUTO_ENABLED=True, FOLLOW_UP_AUTO_DRY_RUN=False,
          STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
CRON = {"HTTP_X_APPENGINE_CRON": "true"}


class CronEndpointTests(TestCase):
    url = "/enquiries/api/cron/automations/"

    def test_the_url_is_where_cron_yaml_points(self):
        self.assertEqual(reverse("enquiries:cron_automations"), self.url)
        text = (pathlib.Path(__file__).resolve().parents[2] / "cron.yaml").read_text(encoding="utf-8")
        self.assertIn(f"url: {self.url}", text)
        self.assertIn("schedule: every 1 hours", text)
        self.assertIn("target: magicwand-backend", text)
        self.assertNotIn("staging", text.replace("staging never sends", ""))  # only live is scheduled

    def test_only_app_engine_cron_may_call_it(self):
        with patch.object(scheduler, "run_all") as run_all:
            for method in ("get", "post"):
                self.assertEqual(getattr(self.client, method)(self.url).status_code, 403)
                self.assertEqual(getattr(self.client, method)(self.url, HTTP_X_APPENGINE_CRON="false").status_code, 403)
                self.assertEqual(getattr(self.client, method)(self.url, HTTP_X_N8N_TOKEN="anything").status_code, 403)
        run_all.assert_not_called()

    def test_cron_starts_a_run_and_gets_the_summary(self):
        with patch.object(scheduler, "run_all", return_value={"ok": True, "run_id": 7}) as run_all:
            response = self.client.get(self.url, **CRON)
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "run_id": 7}))
        run_all.assert_called_once_with(trigger="cron")

    def test_a_problem_inside_a_run_is_still_a_200_so_cron_does_not_retry(self):
        with patch.object(scheduler, "follow_up_step", side_effect=RuntimeError("boom")), \
                patch.object(scheduler, "start_step", return_value={}), \
                patch.object(scheduler, "marketing_lead_step", return_value={}), \
                patch.object(scheduler.services, "list_enquiries", return_value=[]), \
                patch.object(scheduler, "reply_check_step", return_value={}):
            response = self.client.post(self.url, **CRON)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("boom", response.json()["follow_ups"]["error"])


class N8nIsGoneTests(TestCase):
    def test_the_old_n8n_urls_and_token_no_longer_exist(self):
        from django.conf import settings

        for name, kwargs in (("enquiries:auto_follow_up_due", {}), ("enquiries:auto_follow_up_send", {"enquiry_id": "E1"}),
                             ("enquiries:customer_emails_run", {}), ("marketing:api_check_replies", {})):
            with self.assertRaises(NoReverseMatch):
                reverse(name, kwargs=kwargs)
        self.assertFalse(hasattr(settings, "N8N_API_TOKEN"))
        for old in ("/enquiries/api/auto-follow-ups/due/", "/enquiries/api/customer-emails/run/",
                    "/marketing/api/check-replies/"):
            self.assertIn(self.client.post(old, HTTP_X_N8N_TOKEN="x").status_code, (302, 403, 404))


@override_settings(**ON)
class RunAllTests(TestCase):
    def _steps(self, **overrides):
        steps = {"follow_up_step": {"enabled": True, "due": [], "sent": []},
                 "start_step": {"started": []},
                 "marketing_lead_step": {"created": [], "started": []},
                 "reply_check_step": {"campaigns": [], "replied": 0, "failed": 0}}
        steps.update(overrides)
        load = patch.object(scheduler.services, "list_enquiries", return_value=[])
        load.start()
        self.addCleanup(load.stop)
        patches = [patch.object(scheduler, name, return_value=value) if not isinstance(value, Exception)
                   else patch.object(scheduler, name, side_effect=value) for name, value in steps.items()]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_run_does_all_three_steps_and_is_logged(self):
        self._steps()
        result = scheduler.run_all(trigger="command")
        self.assertTrue(result["ok"])
        self.assertEqual({"load", "start_sequences", "marketing_leads", "follow_ups", "reply_checks"} - set(result), set())
        run = AutomationRun.objects.get()
        self.assertTrue(run.ok)
        self.assertEqual((run.trigger, run.finished_at is not None), ("command", True))
        self.assertEqual(set(json.loads(run.summary)), {"load", "start_sequences", "marketing_leads", "follow_ups", "reply_checks"})

    def test_one_failing_step_does_not_stop_the_others(self):
        self._steps(marketing_lead_step=RuntimeError("gmail down"))
        result = scheduler.run_all()
        self.assertFalse(result["ok"])
        self.assertIn("gmail down", result["marketing_leads"]["error"])
        self.assertEqual(result["reply_checks"]["replied"], 0)  # still ran
        self.assertFalse(AutomationRun.objects.get().ok)

    def test_a_step_that_reports_an_error_marks_the_run_not_ok(self):
        self._steps(follow_up_step={"error": "could not load enquiries: down"})
        self.assertFalse(scheduler.run_all()["ok"])

    def test_a_second_run_waits_while_one_is_in_progress(self):
        self._steps()
        AutomationRun.objects.create(started_at=timezone.now() - timedelta(minutes=2))  # not finished
        self.assertEqual(scheduler.run_all(), {"skipped": "another run is still in progress"})
        self.assertEqual(AutomationRun.objects.count(), 1)

    def test_a_dead_run_does_not_block_forever(self):
        self._steps()
        AutomationRun.objects.create(
            started_at=timezone.now() - timedelta(minutes=AutomationRun.STALE_MINUTES + 5))
        self.assertTrue(scheduler.run_all()["ok"])
        self.assertEqual(AutomationRun.objects.count(), 2)

    def test_the_management_command_runs_it_and_prints_the_result(self):
        self._steps()
        out = io.StringIO()
        call_command("run_automations", stdout=out)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertEqual(AutomationRun.objects.get().trigger, "command")


@override_settings(**ON)
class StepTests(TestCase):
    def test_follow_ups_are_sent_for_every_due_enquiry(self):
        due = [{"enquiry_id": "E1"}, {"enquiry_id": "E2"}]
        with patch.object(auto_follow_up, "due_follow_ups", return_value=due), \
                patch.object(auto_follow_up, "run_auto_follow_up",
                             side_effect=lambda eid, today=None: {"enquiry_id": eid, "result": "sent"}) as send:
            result = scheduler.follow_up_step()
        self.assertEqual([r["enquiry_id"] for r in result["sent"]], ["E1", "E2"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual((result["enabled"], result["dry_run"]), (True, False))

    @override_settings(FOLLOW_UP_AUTO_ENABLED=False)
    def test_nothing_happens_when_automatic_follow_ups_are_off(self):
        with patch.object(auto_follow_up, "due_follow_ups") as due, \
                patch.object(auto_follow_up, "detect_gmail_quotes") as detect:
            follow_ups = scheduler.follow_up_step()
            started = scheduler.start_step([{"enquiry_id": "E1", "status": "open"}])
            leads = scheduler.marketing_lead_step([{"enquiry_id": "E1", "status": "rejected"}])
        due.assert_not_called()
        detect.assert_not_called()
        self.assertEqual((follow_ups["due"], follow_ups["sent"], started["started"], leads["created"]),
                         ([], [], [], []))

    def test_one_enquiry_failing_does_not_stop_the_rest(self):
        def send(eid, today=None):
            if eid == "E1":
                raise RuntimeError("bad data")
            return {"enquiry_id": eid, "result": "sent"}

        with patch.object(auto_follow_up, "due_follow_ups", return_value=[{"enquiry_id": "E1"}, {"enquiry_id": "E2"}]), \
                patch.object(auto_follow_up, "run_auto_follow_up", side_effect=send):
            result = scheduler.follow_up_step()
        self.assertEqual([(r["enquiry_id"], r["result"]) for r in result["sent"]],
                         [("E1", auto_follow_up.FAILED), ("E2", "sent")])

    def test_the_time_budget_leaves_the_rest_for_the_next_run(self):
        due = [{"enquiry_id": "E1"}, {"enquiry_id": "E2"}, {"enquiry_id": "E3"}]
        calls = []
        with patch.object(auto_follow_up, "due_follow_ups", return_value=due), \
                patch.object(auto_follow_up, "run_auto_follow_up",
                             side_effect=lambda eid, today=None: calls.append(eid) or {"enquiry_id": eid, "result": "sent"}):
            result = scheduler.follow_up_step(deadline=0)  # already past
        self.assertEqual((calls, "stopped" in result), ([], True))

    def test_a_service_outage_is_reported_not_raised(self):
        with patch.object(auto_follow_up, "due_follow_ups", side_effect=services.EnquiryServiceError("down")):
            self.assertIn("could not load enquiries", scheduler.follow_up_step()["error"])

    def test_reply_check_counts_replies_and_failures(self):
        campaigns = [{"name": "a", "result": "checked", "replied": 2}, {"name": "b", "result": "failed"}]
        with patch("marketing.campaigns.check_all_replies", return_value=campaigns):
            result = scheduler.reply_check_step()
        self.assertEqual((result["replied"], result["failed"]), (2, 1))

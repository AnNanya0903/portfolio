from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    compounding_business_date,
)
from compounding.stock import crin_fb_totals
from dashboard.models import UserServiceAccess

OLD_DAY = "2026-03-02"


class WorkQueueTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="queue-user",
            email="queue@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=OLD_DAY, cjc_number=500, created_by=self.user)
        self._sheet("400")

    def _sheet(self, mb_stock, fb_stock="0"):
        CompoundingMasterSheet.objects.update_or_create(
            plan=self.plan,
            defaults={
                "created_by": self.user,
                "headers": ["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
                "rows": [["7020", "100", "20", "120", mb_stock, fb_stock]],
            },
        )

    def _crin(self, number="CRIN-1", quantity=1200, **extra):
        return CompoundingCRIN.objects.create(plan=self.plan, crin_number=number, compound_code="7020", quantity=quantity, created_by=self.user, **extra)

    def _queue(self, range_key="all"):
        response = self.client.get(reverse("compounding:work_queue"), {"range": range_key})
        self.assertEqual(response.status_code, 200)
        return {stage["key"]: stage["items"] for stage in response.context["queue"]["stages"]}, response

    def _ids(self, items):
        return [item["id"] for item in items]

    def test_short_crin_waits_for_mb_with_suggested_batches(self):
        crin = self._crin()  # needs 1000 MB, 400 in stock

        queue, response = self._queue()

        item = queue["mb"][0]
        self.assertEqual(item["id"], crin.id)
        self.assertEqual((item["mb_short"], item["suggested_batches"]), ("600", 6))
        self.assertContains(response, "Send MB")

    def test_sending_mb_from_queue_moves_it_and_returns_here(self):
        crin = self._crin()

        response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            {"next": reverse("compounding:work_queue"), "plan_date": compounding_business_date().isoformat(), "compound_code": "7020", "planning_source": "batches", "mb_batches": "6", "shift": "1"},
        )

        self.assertEqual(response["Location"], reverse("compounding:work_queue"))
        queue, _response = self._queue()
        self.assertTrue(queue["mb"][0]["mb_on_board"])
        self.assertEqual(queue["mb"][0]["id"], crin.id)

    def test_enough_mb_is_ready_for_fb_and_sends_back_here(self):
        self._sheet("1000")
        crin = self._crin()
        queue, response = self._queue()
        self.assertEqual(self._ids(queue["fb"]), [crin.id])
        self.assertContains(response, "Send FB")

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            {"next": reverse("compounding:work_queue"), "plan_date": OLD_DAY, "shift": "2"},
        )

        self.assertEqual(response["Location"], reverse("compounding:work_queue"))
        queue, _response = self._queue()
        self.assertEqual(self._ids(queue["board"]), [crin.id])
        self.assertIn("Shift 2: 10 FB", queue["board"][0]["detail"])

    def test_oldest_crin_gets_mb_first(self):
        self._sheet("1000")
        first = self._crin("CRIN-1")
        second = self._crin("CRIN-2")

        queue, _response = self._queue()

        self.assertEqual(self._ids(queue["fb"]), [first.id])
        self.assertEqual(self._ids(queue["mb"]), [second.id])

    def test_made_fb_is_ready_to_issue_and_issue_returns_here(self):
        self._sheet("1000", fb_stock="1200")
        crin = self._crin()
        self.client.post(reverse("compounding:send_to_board", args=[crin.id]), {"plan_date": OLD_DAY, "shift": "1"})
        line = crin.planned_lines.get()
        self.client.post(reverse("compounding:set_actuals"), {"plan_date": OLD_DAY, "line_id": line.id, "actual_mb": "0", "actual_fb": "10"})

        queue, response = self._queue()
        self.assertEqual(self._ids(queue["issue"]), [crin.id])
        self.assertContains(response, "Issue")

        expected = (crin_fb_totals([crin.id]).get(crin.id) or {}).get("issued", Decimal("0.000"))
        response = self.client.post(
            reverse("compounding:issue_fb", args=[crin.id]),
            {"next": reverse("compounding:work_queue"), "quantity": "1200", "expected_issued": str(expected)},
        )

        self.assertEqual(response["Location"], reverse("compounding:work_queue"))
        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_CLOSED)
        queue, _response = self._queue()
        self.assertFalse(any(queue.values()))

    def test_unsafe_return_address_is_ignored(self):
        self._sheet("1000")
        crin = self._crin()

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            {"next": "https://example.com/", "plan_date": OLD_DAY, "shift": "1"},
        )
        self.assertNotIn("example.com", response["Location"])

        response = self.client.post(
            reverse("compounding:mb_send_to_board"),
            {"next": "//example.com/compounding/", "plan_date": OLD_DAY, "compound_code": "7020", "planning_source": "batches", "mb_batches": "1", "shift": "3"},
        )
        self.assertNotIn("example.com", response["Location"])

    def test_closed_crins_are_not_listed(self):
        self._crin(status=CompoundingCRIN.STATUS_CLOSED)

        queue, response = self._queue()

        self.assertFalse(any(queue.values()))
        self.assertContains(response, "No open CRINs")

    def test_board_user_sees_queue_without_planning_buttons(self):
        self._sheet("1000")
        self._crin()
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_board"])

        _queue, response = self._queue()

        self.assertNotContains(response, "Send FB</button>")
        self.assertContains(response, "Planning will send FB")

    def test_production_user_cannot_open_queue(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_production"])

        self.assertEqual(self.client.get(reverse("compounding:work_queue")).status_code, 302)

    def test_home_planning_and_board_link_to_queue(self):
        url = reverse("compounding:work_queue")
        for name, params in (("board", {}), ("planning", {"date": OLD_DAY}), ("display", {"date": OLD_DAY})):
            self.assertContains(self.client.get(reverse(f"compounding:{name}"), params), f'href="{url}"')


class WorkQueueRecentTests(WorkQueueTests):
    """The queue shows recent CRINs by default and keeps older ones one click away."""

    def _today_plan(self):
        plan = CompoundingPlan.objects.create(plan_date=compounding_business_date(), cjc_number=501, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "5000", "0"]],
        )
        return plan

    def test_default_shows_only_today_and_counts_older(self):
        old = self._crin("CRIN-OLD")
        recent = CompoundingCRIN.objects.create(plan=self._today_plan(), crin_number="CRIN-NEW", compound_code="7020", quantity=1200, created_by=self.user)

        queue, response = self._queue("today")

        shown = [item["id"] for items in queue.values() for item in items]
        self.assertEqual(shown, [recent.id])
        self.assertNotIn(old.id, shown)
        self.assertEqual(response.context["queue"]["hidden"], 1)
        self.assertContains(response, "1 older open CRIN hidden")

    def test_no_range_means_today(self):
        self._crin("CRIN-OLD")

        response = self.client.get(reverse("compounding:work_queue"))

        self.assertEqual(response.context["range_key"], "today")
        self.assertEqual(response.context["queue"]["total"], 0)
        self.assertContains(response, "Nothing recent is open")

    def test_week_and_all_ranges(self):
        self._crin("CRIN-OLD")
        recent_plan = CompoundingPlan.objects.create(
            plan_date=compounding_business_date() - timedelta(days=3), cjc_number=502, created_by=self.user
        )
        CompoundingCRIN.objects.create(plan=recent_plan, crin_number="CRIN-3D", compound_code="7020", quantity=100, created_by=self.user)

        week = self.client.get(reverse("compounding:work_queue"), {"range": "week"})
        everything = self.client.get(reverse("compounding:work_queue"), {"range": "all"})

        self.assertEqual(week.context["queue"]["total"], 1)
        self.assertEqual(week.context["queue"]["hidden"], 1)
        self.assertEqual(everything.context["queue"]["total"], 2)
        self.assertEqual(everything.context["queue"]["hidden"], 0)

    def test_unknown_range_falls_back_to_today(self):
        response = self.client.get(reverse("compounding:work_queue"), {"range": "banana"})

        self.assertEqual(response.context["range_key"], "today")

    def test_older_crins_still_take_mb_first(self):
        self._sheet("1000")
        self._crin("CRIN-OLD")  # needs all 1000 MB
        recent = CompoundingCRIN.objects.create(plan=self._today_plan(), crin_number="CRIN-NEW", compound_code="7020", quantity=1200, created_by=self.user)
        CompoundingMasterSheet.objects.filter(plan=recent.plan).update(
            rows=[["7020", "100", "20", "120", "1000", "0"]]
        )

        queue, _response = self._queue("today")

        self.assertEqual([item["id"] for item in queue["mb"]], [recent.id])
        self.assertEqual(queue["fb"], [])

    def test_crin_on_todays_board_stays_visible(self):
        self._sheet("5000")
        old = self._crin("CRIN-OLD")
        self.client.post(
            reverse("compounding:send_to_board", args=[old.id]),
            {"plan_date": OLD_DAY, "shift": "1"},
        )
        CompoundingLine.objects.filter(crin_request=old).update(plan=self._today_plan())

        queue, _response = self._queue("today")

        self.assertEqual([item["id"] for item in queue["board"]], [old.id])

    def test_actions_return_to_the_same_range(self):
        response = self.client.get(reverse("compounding:work_queue"), {"range": "week"})

        self.assertEqual(response.context["back_url"], f"{reverse('compounding:work_queue')}?range=week")
        self.assertContains(response, "?range=all")

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingCRINRequest,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    compounding_business_date,
)
from compounding.views import _mb_availability
from dashboard.models import UserServiceAccess


class OlderOpenCRINTests(TestCase):
    """A CRIN raised on an earlier day stays in FB planning, Planning and the board flow until issued."""

    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="older-user", email="older@example.com", password="secret123")
        self.client.force_login(self.user)
        self.today = compounding_business_date()
        self.yesterday = self.today - timedelta(days=1)
        self.old_plan = CompoundingPlan.objects.create(plan_date=self.yesterday, cjc_number=500, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.old_plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "5000", "0"]],
        )

    def _old_crin(self, number="CRIN-OLD", quantity=1200, **extra):
        return CompoundingCRIN.objects.create(plan=self.old_plan, crin_number=number, compound_code="7020", quantity=quantity, created_by=self.user, **extra)

    def _fb_page(self, **params):
        response = self.client.get(reverse("compounding:fb_planning"), {"date": self.today.isoformat(), **params})
        self.assertEqual(response.status_code, 200)
        return response

    def test_production_crin_raised_yesterday_shows_in_todays_fb_planning(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_production", "compounding_planning"])
        header = CompoundingCRINRequest.objects.create(plan=self.old_plan, request_number="CRIN-2026-204", created_by=self.user)
        crin = self._old_crin("CRIN-2026-204-1", request=header)

        response = self._fb_page()

        rows = {row["id"]: row for row in response.context["crins"]}
        self.assertIn(crin.id, rows)
        self.assertEqual(rows[crin.id]["crin_number"], "CRIN-2026-204")
        self.assertTrue(rows[crin.id]["is_older"])
        self.assertContains(response, "Raised")

    def test_older_crin_can_be_sent_to_todays_board(self):
        crin = self._old_crin()

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            {"plan_date": self.today.isoformat(), "shift": "1"},
        )

        line = crin.planned_lines.get()
        self.assertEqual(line.plan.plan_date, self.today)
        self.assertEqual(line.shift1_fb, 10)
        board = self.client.get(reverse("compounding:display"), {"date": self.today.isoformat()})
        self.assertEqual([row["id"] for row in board.context["fb_rows"]], [line.id])

    def test_older_crin_is_not_sent_twice_from_another_day(self):
        crin = self._old_crin()
        for plan_date in (self.today, self.yesterday):
            self.client.post(
                reverse("compounding:send_to_board", args=[crin.id]),
                {"plan_date": plan_date.isoformat(), "shift": "1"},
            )

        self.assertEqual(crin.planned_lines.count(), 1)
        row = next(item for item in self._fb_page().context["crins"] if item["id"] == crin.id)
        self.assertTrue(row["has_line"])

    def test_closed_crin_from_another_day_cannot_be_sent(self):
        crin = self._old_crin(status=CompoundingCRIN.STATUS_CLOSED)

        response = self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            {"plan_date": self.today.isoformat(), "shift": "1"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(crin.planned_lines.exists())
        self.assertNotIn(crin.id, [item["id"] for item in self._fb_page().context["crins"]])

    def test_planning_page_lists_older_open_crins(self):
        crin = self._old_crin()

        response = self.client.get(reverse("compounding:planning"), {"date": self.today.isoformat()})

        row = next(item for item in response.context["crin_requests"] if item["id"] == crin.id)
        self.assertTrue(row["is_older"])
        self.assertContains(response, "Raised")

    def test_mb_demand_counts_older_open_crins_so_nothing_is_double_booked(self):
        self._old_crin("CRIN-OLD")  # 10 FB = 1000 MB
        today_plan = CompoundingPlan.objects.create(plan_date=self.today, cjc_number=501, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=today_plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "1500", "0"]],
        )
        CompoundingCRIN.objects.create(plan=today_plan, crin_number="CRIN-NEW", compound_code="7020", quantity=1200, created_by=self.user)

        availability = _mb_availability(today_plan, "7020")

        self.assertEqual(availability["open_fb"], 20)
        self.assertEqual(availability["required"], Decimal("2000"))
        self.assertEqual(availability["shortage"], Decimal("500"))

    def test_fb_planning_gives_free_mb_to_the_older_crin_first(self):
        self._old_crin("CRIN-OLD")
        today_plan = CompoundingPlan.objects.create(plan_date=self.today, cjc_number=501, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=today_plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "1000", "0"]],
        )
        new = CompoundingCRIN.objects.create(plan=today_plan, crin_number="CRIN-NEW", compound_code="7020", quantity=1200, created_by=self.user)

        rows = {row["crin_number"]: row for row in self._fb_page().context["crins"]}

        self.assertTrue(rows["CRIN-OLD"]["mb_ready"])
        self.assertFalse(rows[new.crin_number]["mb_ready"])

    def test_todays_work_sends_an_older_crin_to_todays_board(self):
        crin = self._old_crin()

        page = self.client.get(reverse("compounding:work_queue"), {"range": "all"})
        self.assertContains(page, f'name="plan_date" value="{self.today.isoformat()}"')
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            {"plan_date": self.today.isoformat(), "shift": "2", "next": reverse("compounding:work_queue")},
        )

        self.assertEqual(crin.planned_lines.get().plan.plan_date, self.today)


class RecentCRINScopeTests(OlderOpenCRINTests):
    """FB planning and Planning list recent CRINs by default, with older open ones one click away."""

    def _stale_crin(self, days=5):
        plan = CompoundingPlan.objects.create(plan_date=self.today - timedelta(days=days), cjc_number=490, created_by=self.user)
        return CompoundingCRIN.objects.create(plan=plan, crin_number="CRIN-STALE", compound_code="7020", quantity=600, created_by=self.user)

    def test_fb_planning_hides_old_crins_but_keeps_yesterdays(self):
        yesterday_crin = self._old_crin("CRIN-YDAY")
        stale = self._stale_crin()

        response = self._fb_page()

        shown = [row["id"] for row in response.context["crins"]]
        self.assertIn(yesterday_crin.id, shown)
        self.assertNotIn(stale.id, shown)
        self.assertEqual(response.context["hidden_older"], 1)
        self.assertContains(response, "1 older open CRIN hidden")
        self.assertContains(response, "range=all")

    def test_all_open_shows_the_old_ones_too(self):
        stale = self._stale_crin()

        response = self._fb_page(range="all")

        self.assertIn(stale.id, [row["id"] for row in response.context["crins"]])
        self.assertEqual(response.context["hidden_older"], 0)
        self.assertContains(response, "Show recent only")

    def test_planning_page_hides_old_crins_too(self):
        recent = self._old_crin("CRIN-YDAY")
        stale = self._stale_crin()

        response = self.client.get(reverse("compounding:planning"), {"date": self.today.isoformat()})

        shown = [row["id"] for row in response.context["crin_requests"]]
        self.assertIn(recent.id, shown)
        self.assertNotIn(stale.id, shown)
        self.assertEqual(response.context["hidden_older"], 1)
        everything = self.client.get(reverse("compounding:planning"), {"date": self.today.isoformat(), "range": "all"})
        self.assertIn(stale.id, [row["id"] for row in everything.context["crin_requests"]])

    def test_a_days_own_closed_crin_still_shows_and_no_note_when_nothing_is_hidden(self):
        done = self._old_crin("CRIN-DONE", status=CompoundingCRIN.STATUS_CLOSED)

        response = self.client.get(reverse("compounding:fb_planning"), {"date": self.yesterday.isoformat()})

        self.assertIn(done.id, [row["id"] for row in response.context["crins"]])
        self.assertNotContains(response, "older open CRIN")

import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingCRINRequest,
    CompoundingMasterSheet,
    CompoundingPlan,
    compounding_business_date,
)
from compounding.stock import crin_fb_totals
from compounding.views import _mb_availability
from dashboard.models import UserServiceAccess


class ProductionCRINTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="production-user",
            email="production@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.sheet_plan = CompoundingPlan.objects.create(
            plan_date=compounding_business_date(),
            cjc_number=500,
            created_by=self.user,
        )
        CompoundingMasterSheet.objects.create(
            plan=self.sheet_plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[
                ["7020", "100", "20", "120", "5000", "1000"],
                ["6820", "100", "20", "120", "2000", "500"],
                ["EPX 60", "50", "10", "60", "900", "100"],
                ["EPX 70", "50", "10", "60", "900", "100"],
            ],
        )

    def _restrict_to(self, services):
        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = list(services)
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])

    def _raise(self, rows, *, submission_id=None, note="", follow=True):
        return self.client.post(
            reverse("compounding:raise_crin_request"),
            data={
                "submission_id": str(submission_id or uuid.uuid4()),
                "compound_code": [row[0] for row in rows],
                "quantity": [row[1] for row in rows],
                "note": note,
            },
            follow=follow,
        )

    def _messages(self, response) -> str:
        return " ".join(str(message) for message in response.context["messages"])

    def test_one_crin_can_cover_several_compounds(self):
        response = self._raise(
            [("7020", "3000"), ("6820", "1200"), ("EPX 60", "600"), ("EPX 70", "300")],
            note="Urgent",
        )

        header = CompoundingCRINRequest.objects.get()
        self.assertEqual(header.note, "Urgent")
        self.assertEqual(header.created_by, self.user)
        items = list(header.items.order_by("crin_number"))
        self.assertEqual([item.crin_number for item in items], [f"{header.request_number}-{n}" for n in (1, 2, 3, 4)])
        self.assertEqual([item.compound_code for item in items], ["7020", "6820", "EPX 60", "EPX 70"])
        self.assertEqual(items[0].order_quantity, 3000)
        self.assertIn("with 4 compounds", self._messages(response))

    def test_blank_rows_are_ignored(self):
        self._raise([("", ""), ("7020", "100"), ("", "")])

        self.assertEqual(CompoundingCRIN.objects.count(), 1)

    def test_invalid_entries_raise_nothing_and_keep_the_form(self):
        cases = [
            ([("7020", "100"), ("", "50")], "Select a compound"),
            ([("7020", "100"), ("UNKNOWN", "50")], "Select a compound"),
            ([("7020", "100"), ("7020", "50")], "entered twice"),
            ([("7020", "0")], "whole-number"),
            ([("7020", "1.5")], "whole-number"),
            ([("7020", "-5")], "whole-number"),
            ([("7020", "99999999999")], "whole-number"),
            ([("", "")], "at least one"),
        ]
        for rows, expected in cases:
            response = self._raise(rows)
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, self._messages(response))
        self.assertFalse(CompoundingCRINRequest.objects.exists())
        self.assertFalse(CompoundingCRIN.objects.exists())
        kept = self._raise([("7020", "100"), ("7020", "50")]).context["draft_rows"]
        self.assertEqual(kept[0], {"compound_code": "7020", "quantity": "100"})

    def test_repeat_submission_is_not_raised_twice(self):
        submission_id = uuid.uuid4()

        self._raise([("7020", "100")], submission_id=submission_id)
        response = self._raise([("7020", "100")], submission_id=submission_id)

        self.assertEqual(CompoundingCRINRequest.objects.count(), 1)
        self.assertEqual(CompoundingCRIN.objects.count(), 1)
        self.assertIn("already raised", self._messages(response))

    def test_each_compound_flows_through_planning_and_issue(self):
        self._raise([("7020", "240"), ("6820", "120")])
        crin = CompoundingCRIN.objects.get(compound_code="7020")

        self.assertEqual(_mb_availability(crin.plan, "7020")["required"], Decimal("200"))
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": crin.plan.plan_date.isoformat(), "shift": "1"},
        )
        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": crin.plan.plan_date.isoformat(), "line_id": str(line.id), "actual_mb": "0", "actual_fb": "2"},
        )
        expected = (crin_fb_totals([crin.id]).get(crin.id) or {}).get("issued", Decimal("0.000"))
        self.client.post(
            reverse("compounding:issue_fb", args=[crin.id]),
            data={"quantity": "240", "expected_issued": str(expected)},
        )

        response = self.client.get(reverse("compounding:production"))
        group = response.context["groups"][0]
        self.assertEqual(group["closed_count"], 1)
        self.assertEqual(group["item_count"], 2)
        labels = {row["compound_code"]: row["stage_label"] for row in group["items"]}
        self.assertEqual(labels["7020"], "Closed")
        self.assertEqual(labels["6820"], "Raised")

    def test_production_user_can_raise_and_track(self):
        self._restrict_to(["compounding_production"])

        page = self.client.get(reverse("compounding:production"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Raise CRIN")
        self._raise([("7020", "100")])

        self.assertEqual(CompoundingCRIN.objects.count(), 1)
        self.assertNotContains(self.client.get(reverse("compounding:production")), "FB stock")

    def test_production_user_cannot_open_compounding_work(self):
        self._restrict_to(["compounding_production"])
        self._raise([("7020", "100")])
        crin = CompoundingCRIN.objects.get()
        before = crin.updated_at

        for name in ("board", "planning", "display", "stock_ledger", "fb_issue", "fb_planning", "mb_planning"):
            response = self.client.get(reverse(f"compounding:{name}"))
            self.assertEqual(response.status_code, 302, name)
        for url in (
            reverse("compounding:issue_fb", args=[crin.id]),
            reverse("compounding:send_to_board", args=[crin.id]),
            reverse("compounding:set_actuals"),
            reverse("compounding:create_crin"),
        ):
            self.assertIn(self.client.post(url, data={"plan_date": "2026-02-03"}).status_code, {302, 403}, url)
        crin.refresh_from_db()
        self.assertEqual(crin.updated_at, before)
        self.assertFalse(crin.planned_lines.exists())
        self.assertFalse(crin.stock_entries.exists())

    def test_planning_user_can_raise(self):
        self._restrict_to(["compounding_planning"])

        self._raise([("7020", "100")])

        self.assertEqual(CompoundingCRIN.objects.count(), 1)

    def test_other_users_cannot_raise(self):
        self._restrict_to(["compounding_board", "enquiries"])

        self.assertEqual(self.client.get(reverse("compounding:production")).status_code, 302)
        self.assertEqual(self._raise([("7020", "100")], follow=False).status_code, 302)
        self.assertFalse(CompoundingCRIN.objects.exists())

    def test_legacy_compounding_access_does_not_grant_production(self):
        self._restrict_to(["enquiries"])
        self.assertEqual(self.client.get(reverse("compounding:production")).status_code, 302)

    def test_production_menu_links_to_raise_crin(self):
        self._restrict_to(["compounding_production"])

        response = self.client.get(reverse("compounding:production"))

        self.assertContains(response, 'href="/compounding/production/"')
        self.assertNotContains(response, 'href="/compounding/" class="nav-pill-custom')

    def test_opening_the_page_does_not_create_a_plan(self):
        CompoundingPlan.objects.filter(id=self.sheet_plan.id).update(plan_date="2026-01-01")
        before = CompoundingPlan.objects.count()

        response = self.client.get(reverse("compounding:production"))

        self.assertEqual(CompoundingPlan.objects.count(), before)
        self.assertIn("7020", response.context["compound_options"])

    def test_single_older_crins_still_appear(self):
        CompoundingCRIN.objects.create(
            plan=self.sheet_plan,
            crin_number="CRIN-2026-100",
            compound_code="7020",
            quantity=100,
            created_by=self.user,
        )

        response = self.client.get(reverse("compounding:production"))

        self.assertEqual(response.context["groups"][0]["number"], "CRIN-2026-100")

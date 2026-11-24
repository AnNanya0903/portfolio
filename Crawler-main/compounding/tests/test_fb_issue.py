import importlib
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingStockEntry as Entry,
)
from compounding.stock import crin_fb_totals, stock_balances
from compounding.views import _mb_availability

PLAN_DATE = "2026-03-02"
KEY = "7020"


class FBIssueTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="issue-user",
            email="issue@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["7020", "100", "20", "120", "5000", "1000"]],
        )

    def _crin(self, number="CRIN-1", quantity=600, **extra) -> CompoundingCRIN:
        return CompoundingCRIN.objects.create(
            plan=self.plan,
            crin_number=number,
            compound_code="7020",
            quantity=quantity,
            created_by=self.user,
            **extra,
        )

    def _issue(self, crin, quantity, expected=None, note=""):
        if expected is None:
            expected = (crin_fb_totals([crin.id]).get(crin.id) or {}).get("issued", Decimal("0.000"))
        return self.client.post(
            reverse("compounding:issue_fb", args=[crin.id]),
            data={"quantity": str(quantity), "expected_issued": str(expected), "note": note},
            follow=True,
        )

    def _messages(self, response) -> str:
        return " ".join(str(message) for message in response.context["messages"])

    def _row(self, crin, show=""):
        params = {"show": show} if show else {}
        response = self.client.get(reverse("compounding:fb_issue"), params)
        return next(row for row in response.context["rows"] if row["id"] == crin.id)

    def _fb(self):
        return stock_balances([KEY])[KEY]["fb"]

    def test_part_issue_reduces_stock_and_keeps_crin_open(self):
        crin = self._crin()

        response = self._issue(crin, 250, note="Line 2")

        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_OPEN)
        self.assertEqual(self._fb(), Decimal("750.000"))
        self.assertIn("350 is left to issue", self._messages(response))
        row = self._row(crin)
        self.assertEqual(row["stage_label"], "Part Issued")
        self.assertEqual(row["issued"], "250")
        self.assertEqual(row["to_issue"], "350")
        entry = Entry.objects.get(entry_type=Entry.ENTRY_FB_ISSUED)
        self.assertEqual(entry.quantity, Decimal("-250"))
        self.assertEqual(entry.note, "Line 2")
        self.assertEqual(entry.created_by, self.user)

    def test_full_issue_closes_crin(self):
        crin = self._crin()

        self._issue(crin, 250)
        response = self._issue(crin, 350)

        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_CLOSED)
        self.assertEqual(self._fb(), Decimal("400.000"))
        self.assertIn("now closed", self._messages(response))
        self.assertEqual(self._row(crin, show="closed")["stage_label"], "Closed")

    def test_cannot_issue_more_than_order(self):
        crin = self._crin()

        response = self._issue(crin, 601)

        self.assertIn("left to issue", self._messages(response))
        self.assertFalse(Entry.objects.filter(entry_type=Entry.ENTRY_FB_ISSUED).exists())

    def test_cannot_issue_more_than_fb_stock(self):
        crin = self._crin(quantity=1500)

        response = self._issue(crin, 1001)

        self.assertIn("FB stock is available", self._messages(response))
        self.assertFalse(Entry.objects.filter(entry_type=Entry.ENTRY_FB_ISSUED).exists())

    def test_two_crins_cannot_spend_same_stock(self):
        first = self._crin("CRIN-1", 800)
        second = self._crin("CRIN-2", 800)

        self._issue(first, 800)
        response = self._issue(second, 800)

        self.assertIn("Only 200", self._messages(response))
        self.assertEqual(self._fb(), Decimal("200.000"))

    def test_repeated_form_is_not_recorded_twice(self):
        crin = self._crin()

        self._issue(crin, 100, expected="0.000")
        response = self._issue(crin, 100, expected="0.000")

        self.assertIn("just updated", self._messages(response))
        self.assertEqual(Entry.objects.filter(entry_type=Entry.ENTRY_FB_ISSUED).count(), 1)

    def test_invalid_quantities_are_rejected(self):
        crin = self._crin()

        for value in ("0", "-5", "abc", "NaN", "Infinity", "1.2345"):
            response = self._issue(crin, value)
            self.assertIn("Enter an issue quantity", self._messages(response))
        self.assertFalse(Entry.objects.filter(entry_type=Entry.ENTRY_FB_ISSUED).exists())

    def test_closed_crin_cannot_be_issued(self):
        crin = self._crin(status=CompoundingCRIN.STATUS_CLOSED)

        response = self._issue(crin, 10)

        self.assertIn("already closed", self._messages(response))
        self.assertFalse(Entry.objects.filter(entry_type=Entry.ENTRY_FB_ISSUED).exists())

    def test_order_quantity_survives_batches_returning_to_planning(self):
        crin = self._crin(quantity=240)  # 2 FB batches
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )
        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "1"},
        )

        crin.refresh_from_db()
        self.assertEqual(crin.quantity, 120)
        self.assertEqual(crin.order_quantity, 240)
        row = self._row(crin)
        self.assertEqual(row["ordered"], "240")
        self.assertEqual(row["made"], "120")
        self.assertEqual(row["stage_label"], "FB Planned")

    def test_finished_batches_wait_for_issue_without_new_demand(self):
        crin = self._crin(quantity=240)
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )
        self.assertEqual(self._row(crin)["stage_label"], "In Production")
        line = crin.planned_lines.get()
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "2"},
        )

        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_OPEN)
        self.assertEqual(self._row(crin)["stage_label"], "FB Ready")
        self.assertEqual(_mb_availability(self.plan, "7020")["open_fb"], 0)

        self._issue(crin, 240)
        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_CLOSED)

    def test_mb_only_crin_closes_when_batches_are_made(self):
        crin = self._crin(mb_planning=2, fb_planning=0)
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_mb=2, crin_request=crin)

        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "2", "actual_fb": "0"},
        )

        crin.refresh_from_db()
        self.assertEqual(crin.status, CompoundingCRIN.STATUS_CLOSED)

    def test_stages_for_new_crins(self):
        raised = self._crin("CRIN-1", 600)
        planned = self._crin("CRIN-2", 600, fb_planning=5)
        short = self._crin("CRIN-3", 7200)

        self.assertEqual(self._row(raised)["stage_label"], "Raised")
        self.assertEqual(self._row(planned)["stage_label"], "FB Planned")
        self.assertEqual(self._row(short)["stage_label"], "MB Pending")

    def test_open_crins_from_earlier_days_are_shown(self):
        crin = self._crin()
        CompoundingPlan.objects.create(plan_date="2026-03-03", cjc_number=501, created_by=self.user)

        self.assertEqual(self._row(crin)["crin_number"], "CRIN-1")

    def test_migration_fills_order_quantity_for_existing_crins(self):
        crin = self._crin(quantity=450)
        CompoundingCRIN.objects.filter(id=crin.id).update(ordered_quantity=None)
        migration = importlib.import_module("compounding.migrations.0016_crin_fb_issue")

        migration.fill_ordered_quantity(django_apps, None)

        crin.refresh_from_db()
        self.assertEqual(crin.ordered_quantity, 450)

    def test_issue_appears_in_stock_history(self):
        crin = self._crin()
        self._issue(crin, 100)

        response = self.client.get(reverse("compounding:stock_ledger"), {"compound": "7020"})

        self.assertContains(response, "FB issued to Production")
        self.assertEqual(response.context["selected_row"]["fb_stock"], "900")

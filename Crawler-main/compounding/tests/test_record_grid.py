import io
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from compounding.models import (
    CompoundingDailyScrap,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingRecordChange,
    CompoundingRecordValue,
    CompoundingStockEntry as Entry,
)
from compounding.records import build_month_report
from compounding.stock import stock_balances
from compounding.tests.test_production_record import workbook_bytes
from dashboard.models import UserServiceAccess

KEY = "AG7020"


class RecordGridTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="grid-user",
            email="grid@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date="2026-12-05", cjc_number=700, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=self.plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["AG-7020", "100", "20", "120", "1000", "50"]],
        )

    def _save(self, *changes, month="2026-12"):
        return self.client.post(
            reverse("compounding:save_record_grid"),
            data=json.dumps({"month": month, "changes": list(changes)}),
            content_type="application/json",
        )

    def _change(self, field, value, old="0", day=None, key=KEY):
        return {"key": key, "field": field, "day": day, "value": value, "old": old}

    def _row(self, month=date(2026, 12, 1)):
        return next(row for row in build_month_report(month, show_all=True)["all_rows"] if row["key"] == KEY)

    def _stock(self):
        return stock_balances([KEY])[KEY]

    def test_typed_batches_use_excel_formulas_and_update_stock(self):
        response = self._save(self._change("day", "3", day=2), self._change("production", "5"))

        self.assertEqual(response.status_code, 200)
        row = self._row()
        self.assertEqual(row["mixing_kg"], Decimal("300"))
        self.assertEqual(row["production_kg"], Decimal("500"))
        self.assertEqual(row["fb_kg"], Decimal("360"))
        self.assertEqual(row["closing_kg"], Decimal("1200"))
        self.assertEqual(self._stock(), {"mb": Decimal("1200.000"), "fb": Decimal("410.000")})

    def test_editing_a_value_posts_only_the_difference(self):
        self._save(self._change("day", "3", day=2))
        self._save(self._change("day", "1", old="3", day=2))

        self.assertEqual(self._stock()["mb"], Decimal("900.000"))
        self.assertEqual(self._stock()["fb"], Decimal("170.000"))
        self._save(self._change("day", "", old="1", day=2))
        self.assertEqual(self._stock()["mb"], Decimal("1000.000"))
        self.assertEqual(self._stock()["fb"], Decimal("50.000"))

    def test_board_day_is_locked_and_not_counted_twice(self):
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="AG-7020", shift1_fb=2)
        self.client.post(
            reverse("compounding:set_actuals"),
            {"plan_date": "2026-12-05", "line_id": line.id, "actual_mb": "0", "actual_fb": "2"},
        )
        before = self._stock()

        response = self._save(self._change("day", "9", old="2", day=5))

        self.assertEqual(response.status_code, 400)
        self.assertIn("locked", response.json()["error"])
        self.assertEqual(self._stock(), before)
        self.assertTrue(self._row()["day_cells"][4]["locked"])

    def test_board_save_warns_when_day_was_already_typed(self):
        self._save(self._change("day", "3", day=5))
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="AG-7020", shift1_fb=2)

        response = self.client.post(
            reverse("compounding:set_actuals"),
            {"plan_date": "2026-12-05", "line_id": line.id, "actual_mb": "0", "actual_fb": "2"},
            follow=True,
        )

        text = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("already has 3 batches typed", text)

    def test_weight_change_keeps_live_opening_kg(self):
        self._save(self._change("mb_weight", "50", old="100"))

        row = self._row()
        self.assertEqual(row["opening_kg"], Decimal("1000"))
        self.assertEqual(row["opening_batches"], Decimal("20"))

    def test_typed_production_adds_to_board_production(self):
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="AG-7020", shift1_mb=2)
        self.client.post(
            reverse("compounding:set_actuals"),
            {"plan_date": "2026-12-05", "line_id": line.id, "actual_mb": "2", "actual_fb": "0"},
        )

        low = self._save(self._change("production", "1", old="2"))
        ok = self._save(self._change("production", "5", old="2"))

        self.assertEqual(low.status_code, 400)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self._row()["production_batches"], Decimal("5"))
        self.assertEqual(self._stock()["mb"], Decimal("1500.000"))

    def test_opening_change_updates_stock(self):
        self._save(self._change("opening", "8", old="10"))

        row = self._row()
        self.assertEqual(row["opening_kg"], Decimal("800"))
        self.assertEqual(row["adjustment_kg"], Decimal("0"))
        self.assertEqual(self._stock()["mb"], Decimal("800.000"))

    def test_weight_change_recalculates_typed_stock(self):
        self._save(self._change("day", "2", day=1))
        self._save(self._change("mb_weight", "50", old="100"))

        row = self._row()
        self.assertEqual(row["mixing_kg"], Decimal("100"))
        self.assertEqual(self._stock()["mb"], Decimal("900.000"))

    def test_invalid_change_saves_nothing(self):
        for value in ("-1", "abc", "NaN", "1.2345"):
            response = self._save(self._change("day", "2", day=1), self._change("day", value, day=2))
            self.assertEqual(response.status_code, 400)
        self.assertFalse(CompoundingRecordValue.objects.exists())
        self.assertFalse(Entry.objects.exists())

    def test_out_of_date_page_is_rejected(self):
        self._save(self._change("day", "2", day=1))

        response = self._save(self._change("day", "5", old="0", day=1))

        self.assertEqual(response.status_code, 400)
        self.assertIn("changed by someone else", response.json()["error"])
        self.assertEqual(self._row()["batches"], Decimal("2"))

    def test_changes_are_recorded(self):
        self._save(self._change("day", "2", day=1))

        change = CompoundingRecordChange.objects.get()
        self.assertEqual((change.compound_code, change.field, change.day, change.old_value, change.new_value, change.changed_by), ("AG-7020", "day", 1, "0", "2", self.user))

    def test_board_user_can_only_save_scrap(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_board"])

        blocked = self._save(self._change("day", "2", day=1))
        allowed = self._save(self._change("scrap", "4", day=1, key=""))

        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(CompoundingDailyScrap.objects.get().quantity, Decimal("4"))

    def test_production_user_cannot_save(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_production"])

        response = self._save(self._change("scrap", "4", day=1, key=""))

        self.assertEqual(response.status_code, 403)

    def test_imported_month_edit_does_not_change_stock(self):
        self.client.post(reverse("compounding:import_production_record"), {"record_file": SimpleUploadedFile("r.xlsx", workbook_bytes())})

        response = self._save(self._change("day", "4", old="2", day=1), month="2026-11")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._row(date(2026, 11, 1))["batches"], Decimal("7"))
        self.assertFalse(Entry.objects.exists())

    def test_page_matches_excel_layout(self):
        response = self.client.get(reverse("compounding:production_record"), {"month": "2026-12"})

        for label in ("SL NO", "Compound Code", "Description", "Uom", "Previous bal. qty.", "Total MB Mixing Qty", "MB Production Qty", "MB Opening Stock Qty", "Total FB Qty", "MB Batch Weight", "Final Chemical Weight", "Rejection in percentage"):
            self.assertContains(response, label)

    def test_download_has_working_excel_formulas(self):
        self._save(self._change("day", "3", day=2), self._change("production", "5"))

        response = self.client.get(reverse("compounding:download_production_record"), {"month": "2026-12"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("compounding-production-record-2026-12.xlsx", response["Content-Disposition"])
        ws = load_workbook(io.BytesIO(response.content))["December-2026"]
        self.assertEqual(ws["B5"].value, "AG-7020")
        self.assertEqual(ws["G5"].value, "=$AP$5*G6")
        self.assertEqual(ws["G6"].value, 3)
        self.assertEqual(ws["AK5"].value, "=SUM(F5:AJ5)")
        self.assertEqual(ws["AN5"].value, "=(AL5+AM5)-(AK5)")
        self.assertEqual(ws["AO5"].value, "=AQ5*AK6+AK5")
        self.assertEqual(ws["A7"].value, "Total production")

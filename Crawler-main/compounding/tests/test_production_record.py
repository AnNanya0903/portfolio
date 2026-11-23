import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from compounding.models import (
    CompoundingDailyScrap,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingRecordMonth,
    CompoundingStockEntry,
)
from compounding.records import build_compound_summary, build_month_report
from compounding.stock import stock_balances
from dashboard.models import UserServiceAccess


def workbook_bytes(title="November-2026", extra_sheet=None):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws["C2"] = "COMPOUNDING MONTHLY PRODUCTION RECORD"
    headers = ["SL NO", "Compound Code", "Description", "Uom", "Previous bal. qty."] + list(range(1, 32)) + [
        "Total MB Mixing Qty", "MB Production Qty", "MB Opening Stock Qty", "MB Closing", "Total FB Qty", " MB Bath Weight ", "Final Chemical Weight",
    ]
    for column, value in enumerate(headers, start=1):
        ws.cell(3, column, value)
    ws["B5"] = "20} AG-7020"
    ws["E5"] = 50
    ws["AP5"] = 100
    ws["AQ5"] = 20
    ws["F6"] = 2
    ws["H6"] = 3
    ws["AL6"] = 10
    ws["AM6"] = 1
    ws["B7"] = "AG-6820"
    ws["AP7"] = 50
    ws["AQ7"] = 10
    ws["G8"] = 4
    ws["AM8"] = 1
    ws["AL10"] = 2
    ws["A11"] = "Total production"
    ws["A12"] = "Total scrap"
    ws["F12"] = 5
    if extra_sheet:
        wb.create_sheet(extra_sheet)
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


class ProductionRecordTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="record-user",
            email="record@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _import(self, content=None, name="record.xlsx"):
        upload = SimpleUploadedFile(name, content or workbook_bytes())
        return self.client.post(reverse("compounding:import_production_record"), {"record_file": upload}, follow=True)

    def _plan(self, plan_date="2026-12-05", mb_stock="500"):
        plan = CompoundingPlan.objects.create(plan_date=plan_date, cjc_number=700, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=plan,
            created_by=self.user,
            headers=["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
            rows=[["AG-7020", "100", "20", "120", mb_stock, "0"]],
        )
        return plan

    def _save_actuals(self, plan, *, mb, fb):
        line = CompoundingLine.objects.create(plan=plan, compound_code="AG-7020", shift1_mb=mb, shift1_fb=fb)
        self.client.post(
            reverse("compounding:set_actuals"),
            {"plan_date": plan.plan_date, "line_id": line.id, "actual_mb": mb, "actual_fb": fb},
        )
        return line

    def test_imported_month_uses_the_excel_formulas(self):
        response = self._import()

        self.assertContains(response, "Imported November 2026")
        report = build_month_report(date(2026, 11, 1), show_all=True)
        row = next(item for item in report["all_rows"] if item["compound_code"] == "AG-7020")
        self.assertEqual(row["batches"], Decimal("5"))
        self.assertEqual(row["mixing_kg"], Decimal("500"))
        self.assertEqual(row["production_kg"], Decimal("1000"))
        self.assertEqual(row["opening_kg"], Decimal("100"))
        self.assertEqual(row["closing_kg"], Decimal("600"))
        self.assertEqual(row["fb_kg"], Decimal("600"))
        self.assertEqual(report["totals"]["mixing_kg"], Decimal("700"))
        self.assertEqual(report["totals"]["fb_with_previous"], Decimal("890"))
        self.assertEqual(report["day_totals"][0]["rejection"], Decimal("2.5"))

    def test_import_does_not_change_live_stock(self):
        self._import()

        self.assertFalse(CompoundingStockEntry.objects.exists())
        self.assertEqual(stock_balances(), {})

    def test_reimport_replaces_the_same_month(self):
        self._import()
        self._import()

        self.assertEqual(CompoundingRecordMonth.objects.count(), 1)
        # AG-7020, AG-6820, and the unnamed row that has production batches.
        self.assertEqual(CompoundingRecordMonth.objects.get().rows.count(), 3)

    def test_summary_pages_ignore_non_month_sheets(self):
        self._import(workbook_bytes(extra_sheet="Compound Summary"))

        self.assertEqual(CompoundingRecordMonth.objects.count(), 1)

    def test_bad_files_are_rejected_without_saving(self):
        self.assertContains(self._import(b"not excel", "record.xlsx"), "could not be read")
        self.assertContains(self._import(b"text", "record.csv"), "Only .xlsx")
        self.assertContains(self._import(workbook_bytes(title="Data")), "No month sheets")
        self.assertFalse(CompoundingRecordMonth.objects.exists())

    def test_live_month_comes_from_board_and_matches_ledger(self):
        plan = self._plan()
        CompoundingStockEntry.objects.create(
            compound_key="AG7020",
            compound_code="AG-7020",
            stock_type=CompoundingStockEntry.STOCK_MB,
            entry_type=CompoundingStockEntry.ENTRY_OPENING,
            quantity=Decimal("500"),
            plan=plan,
        )
        self._save_actuals(plan, mb=3, fb=2)

        report = build_month_report(date(2026, 12, 1))

        row = report["rows"][0]
        self.assertEqual(row["day_cells"][4]["batches"], Decimal("2"))
        self.assertEqual(row["mixing_kg"], Decimal("200"))
        self.assertEqual(row["production_kg"], Decimal("300"))
        self.assertEqual(row["fb_kg"], Decimal("240"))
        self.assertEqual(row["closing_kg"], stock_balances(["AG7020"])["AG7020"]["mb"])

    def test_opening_comes_from_previous_month_ledger(self):
        previous = CompoundingPlan.objects.create(plan_date="2026-11-30", cjc_number=699, created_by=self.user)
        CompoundingStockEntry.objects.create(
            compound_key="AG7020",
            compound_code="AG-7020",
            stock_type=CompoundingStockEntry.STOCK_MB,
            entry_type=CompoundingStockEntry.ENTRY_OPENING,
            quantity=Decimal("800"),
            plan=previous,
        )
        plan = self._plan(mb_stock="0")
        self._save_actuals(plan, mb=0, fb=1)

        row = build_month_report(date(2026, 12, 1))["rows"][0]

        self.assertEqual(row["opening_kg"], Decimal("800"))
        self.assertEqual(row["closing_kg"], Decimal("700"))

    def test_scrap_saves_clears_and_calculates_rejection(self):
        plan = self._plan()
        self._save_actuals(plan, mb=0, fb=2)

        self.client.post(reverse("compounding:save_record_scrap"), {"month": "2026-12", "scrap_5": "10"})
        report = build_month_report(date(2026, 12, 1))
        self.assertEqual(report["totals"]["scrap"], Decimal("10"))
        self.assertEqual(report["day_totals"][4]["rejection"], Decimal("5"))

        self.client.post(reverse("compounding:save_record_scrap"), {"month": "2026-12", "scrap_5": ""})
        self.assertFalse(CompoundingDailyScrap.objects.exists())

    def test_invalid_scrap_saves_nothing(self):
        for value in ("-1", "abc", "NaN", "1.2345"):
            response = self.client.post(
                reverse("compounding:save_record_scrap"),
                {"month": "2026-12", "scrap_1": "5", "scrap_2": value},
                follow=True,
            )
            self.assertContains(response, "Day 2")
        self.assertFalse(CompoundingDailyScrap.objects.exists())

    def test_summary_totals_one_and_different_compounds(self):
        self._import()
        self._import(workbook_bytes(title="September-2026"))

        summary = build_compound_summary([date(2026, 9, 1), date(2026, 11, 1)])

        row = next(item for item in summary["rows"] if item["compound_code"] == "AG-7020")
        self.assertEqual(row["batches"], Decimal("10"))
        self.assertEqual(row["fb_kg"], Decimal("1200"))
        self.assertEqual(summary["totals"]["mixing_kg"], Decimal("1400"))
        response = self.client.get(reverse("compounding:compound_record_summary"))
        self.assertContains(response, "Total of selected compounds")
        self.assertContains(response, 'data-batches="10')

    def test_pages_render_and_show_negative_stock(self):
        wb = workbook_bytes()
        self._import(wb)

        response = self.client.get(reverse("compounding:production_record"), {"month": "2026-11", "show": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AG-7020")
        self.assertContains(response, "Negative closing MB stock")

    def test_access_rules(self):
        access = UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_board"])
        self.assertEqual(self.client.get(reverse("compounding:production_record")).status_code, 200)
        self.assertEqual(self._import().redirect_chain[-1][1], 302)
        self.assertFalse(CompoundingRecordMonth.objects.exists())

        access.allowed_services = ["compounding_production"]
        access.save(update_fields=["allowed_services", "updated_at"])
        self.assertEqual(self.client.get(reverse("compounding:production_record")).status_code, 302)
        self.client.post(reverse("compounding:save_record_scrap"), {"month": "2026-12", "scrap_1": "5"})
        self.assertFalse(CompoundingDailyScrap.objects.exists())

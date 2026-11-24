import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from compounding.models import CompoundingLine, CompoundingPlan, CompoundingStockEntry as Entry
from compounding.stock import stock_balances
from compounding.views import _mb_availability, _sheet_spec_for_compound


class StockLedgerTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="stock-user",
            email="stock@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _upload(self, plan_date: str, *, mb_stock="1000", fb_stock="50", code="AG 7020"):
        wb = Workbook()
        ws = wb.active
        ws.append(["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"])
        ws.append([code, "100", "20", "120", mb_stock, fb_stock])
        buffer = io.BytesIO()
        wb.save(buffer)
        wb.close()
        upload = SimpleUploadedFile("master.xlsx", buffer.getvalue())
        return self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": plan_date, "master_excel": upload},
        )

    def _line(self, plan_date: str, code="AG-7020") -> CompoundingLine:
        plan, _created = CompoundingPlan.objects.get_or_create(
            plan_date=plan_date,
            defaults={"cjc_number": 600, "created_by": self.user},
        )
        return CompoundingLine.objects.create(plan=plan, compound_code=code, shift1_mb=5, shift1_fb=5)

    def _save_actuals(self, line, plan_date: str, *, mb: int, fb: int):
        return self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": plan_date, "line_id": str(line.id), "actual_mb": str(mb), "actual_fb": str(fb)},
            follow=True,
        )

    def _balance(self, key="AG7020"):
        return stock_balances([key])[key]

    def test_first_upload_creates_opening_stock(self):
        self._upload("2026-03-02")

        self.assertEqual(Entry.objects.filter(entry_type=Entry.ENTRY_OPENING).count(), 2)
        self.assertEqual(self._balance(), {"mb": Decimal("1000.000"), "fb": Decimal("50.000")})

    def test_actuals_record_each_movement(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")

        self._save_actuals(line, "2026-03-02", mb=3, fb=2)

        quantities = {entry.entry_type: entry.quantity for entry in Entry.objects.filter(line=line)}
        self.assertEqual(quantities[Entry.ENTRY_MB_MADE], Decimal("300"))
        self.assertEqual(quantities[Entry.ENTRY_MB_USED], Decimal("-200"))
        self.assertEqual(quantities[Entry.ENTRY_FB_MADE], Decimal("240"))
        self.assertEqual(self._balance(), {"mb": Decimal("1100.000"), "fb": Decimal("290.000")})

    def test_stock_carries_to_next_day_without_new_upload(self):
        self._upload("2026-03-02")
        first = self._line("2026-03-02")
        self._save_actuals(first, "2026-03-02", mb=3, fb=0)
        next_plan = CompoundingPlan.objects.create(plan_date="2026-03-03", cjc_number=601, created_by=self.user)

        self.assertEqual(_sheet_spec_for_compound(next_plan, "AG-7020")["mb_stock"], Decimal("1300.000"))

        second = self._line("2026-03-03")
        response = self._save_actuals(second, "2026-03-03", mb=2, fb=1)

        self.assertEqual(self._balance()["mb"], Decimal("1400.000"))
        self.assertEqual(_mb_availability(next_plan, "AG-7020")["mb_stock"], Decimal("1400.000"))
        text = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("updated stock", text)

    def test_second_save_does_not_post_again(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")

        self._save_actuals(line, "2026-03-02", mb=3, fb=2)
        self._save_actuals(line, "2026-03-02", mb=9, fb=9)

        self.assertEqual(Entry.objects.filter(line=line).count(), 3)
        self.assertEqual(self._balance()["mb"], Decimal("1100.000"))

    def test_line_cannot_have_duplicate_movement(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")
        self._save_actuals(line, "2026-03-02", mb=3, fb=0)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Entry.objects.create(
                compound_key="AG7020",
                compound_code="AG-7020",
                stock_type=Entry.STOCK_MB,
                entry_type=Entry.ENTRY_MB_MADE,
                quantity=Decimal("300"),
                line=line,
            )

    def test_new_upload_records_count_difference_and_keeps_history(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")
        self._save_actuals(line, "2026-03-02", mb=1, fb=0)  # 1100

        self._upload("2026-03-03", mb_stock="1050", fb_stock="50")

        count = Entry.objects.get(entry_type=Entry.ENTRY_STOCK_COUNT, stock_type=Entry.STOCK_MB)
        self.assertEqual(count.quantity, Decimal("-50"))
        self.assertEqual(self._balance()["mb"], Decimal("1050.000"))
        self.assertTrue(Entry.objects.filter(entry_type=Entry.ENTRY_MB_MADE).exists())

    def test_older_date_upload_does_not_change_live_stock(self):
        self._upload("2026-03-03", mb_stock="1000")

        response = self.client.get(reverse("compounding:stock_ledger"))
        self.assertEqual(response.status_code, 200)
        self._upload("2026-03-02", mb_stock="5")

        self.assertEqual(self._balance()["mb"], Decimal("1000.000"))

    def test_blank_upload_cell_does_not_reset_stock(self):
        self._upload("2026-03-02")

        self._upload("2026-03-03", mb_stock=None, fb_stock="60")

        self.assertEqual(self._balance(), {"mb": Decimal("1000.000"), "fb": Decimal("60.000")})

    def test_existing_sheet_without_ledger_starts_from_sheet_stock(self):
        self._upload("2026-03-02")
        Entry.objects.all().delete()
        line = self._line("2026-03-02")

        self._save_actuals(line, "2026-03-02", mb=1, fb=0)

        self.assertEqual(self._balance()["mb"], Decimal("1100.000"))
        self.assertTrue(Entry.objects.filter(entry_type=Entry.ENTRY_OPENING, stock_type=Entry.STOCK_MB).exists())

    def test_planning_page_shows_live_stock_from_ledger(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")
        self._save_actuals(line, "2026-03-02", mb=1, fb=0)

        response = self.client.get(reverse("compounding:planning"), {"date": "2026-03-03"})

        self.assertEqual(response.context["master_sheet"]["rows"][0][4], "1100")

    def test_stock_page_shows_balance_and_history(self):
        self._upload("2026-03-02")
        line = self._line("2026-03-02")
        self._save_actuals(line, "2026-03-02", mb=3, fb=2)

        response = self.client.get(reverse("compounding:stock_ledger"), {"compound": "AG 7020"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_row"]["mb_stock"], "1100")
        self.assertEqual(response.context["entries_total"], 5)
        self.assertContains(response, "MB used in FB")

    def test_stock_page_does_not_create_a_plan(self):
        before = CompoundingPlan.objects.count()

        response = self.client.get(reverse("compounding:stock_ledger"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CompoundingPlan.objects.count(), before)

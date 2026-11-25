import io
import json

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from compounding.models import CompoundingMasterSheet, CompoundingPlan
from compounding.views import _sheet_spec_for_compound, _sheet_table_with_detected_headers, _normalize_master_sheet_decimal_cells

try:
    from openpyxl import Workbook
except Exception:  # pragma: no cover
    Workbook = None


@override_settings(CONTRACT_ENABLED=False)
class CompoundingMasterSheetTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="sheet-user",
            email="sheet@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _xlsx_file(self, name: str = "master.xlsx") -> SimpleUploadedFile:
        if Workbook is None:
            self.skipTest("openpyxl not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "Master"
        ws.append(["Compound", "Quantity"])
        ws.append(["AG-7020", 5000])
        ws.append(["AG-FRC-70", 1200])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        buf.seek(0)
        return SimpleUploadedFile(
            name=name,
            content=buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_upload_master_sheet_creates_snapshot(self):
        response = self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-18", "master_excel": self._xlsx_file()},
        )
        self.assertEqual(response.status_code, 302)

        sheet = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-18")
        self.assertEqual(sheet.headers, ["Compound", "Quantity"])
        self.assertEqual(sheet.rows[0], ["AG-7020", "5000"])
        self.assertEqual(sheet.rows[1], ["AG-FRC-70", "1200"])

    def test_save_master_sheet_is_blocked_after_upload(self):
        self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-18", "master_excel": self._xlsx_file()},
        )
        original = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-18")
        original_headers = list(original.headers or [])
        original_rows = [list(row or []) for row in list(original.rows or [])]

        payload = {
            "plan_date": "2026-02-18",
            "headers": ["Compound", "Quantity", "Remark"],
            "rows": [
                ["AG-7020", "4500", "Revised"],
                ["AG-FRC-70", "1200", ""],
            ],
        }
        response = self.client.post(
            reverse("compounding:save_master_sheet"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("read-only", (response.json().get("error") or "").lower())

        sheet = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-18")
        self.assertEqual(sheet.headers, original_headers)
        self.assertEqual(sheet.rows, original_rows)

    def test_upload_ignores_empty_rows_and_columns(self):
        if Workbook is None:
            self.skipTest("openpyxl not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "Master"
        ws.append(["Compound", "Quantity", ""])
        ws.append(["AG-7020", 5000, ""])
        ws.append(["", "", ""])
        ws.append(["AG-FRC-70", 1200, ""])
        ws.append(["", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        buf.seek(0)
        upload = SimpleUploadedFile(
            name="master_with_blanks.xlsx",
            content=buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-19", "master_excel": upload},
        )
        self.assertEqual(response.status_code, 302)

        sheet = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-19")
        self.assertEqual(sheet.headers, ["Compound", "Quantity"])
        self.assertEqual(sheet.rows, [["AG-7020", "5000"], ["AG-FRC-70", "1200"]])

    def test_save_master_sheet_trim_payload_is_blocked(self):
        self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-20", "master_excel": self._xlsx_file()},
        )
        original = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-20")
        original_headers = list(original.headers or [])
        original_rows = [list(row or []) for row in list(original.rows or [])]

        payload = {
            "plan_date": "2026-02-20",
            "headers": ["Compound", "", ""],
            "rows": [
                ["AG-7020", "", ""],
                ["", "", ""],
                ["AG-FRC-70", "", ""],
            ],
        }
        response = self.client.post(
            reverse("compounding:save_master_sheet"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

        sheet = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-20")
        self.assertEqual(sheet.headers, original_headers)
        self.assertEqual(sheet.rows, original_rows)

    def test_upload_detects_header_row_after_title_rows(self):
        if Workbook is None:
            self.skipTest("openpyxl not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "Compound Details"
        ws.append(["AVIGIRI Urethane & Rubber industries Pvt. Ltd."])
        ws.append(["Compound Code ,MB, FB Weight, & MB Stock level"])
        ws.append(
            [
                "SL. No",
                "Compound Code",
                "MB  Weight",
                "Chemicals  Weight",
                "MB & FB Total Weight",
                "MB Min Stock Lavel",
                "MB Correct Stock Levels",
                "FB Correct Stock Levels",
            ]
        )
        ws.append(["1", "AG-NF 60", "36.54", "2.047", "38.587", "100", "231.522", "79.221"])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        buf.seek(0)
        upload = SimpleUploadedFile(
            name="master_with_title_rows.xlsx",
            content=buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        response = self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-21", "master_excel": upload},
        )
        self.assertEqual(response.status_code, 302)

        sheet = CompoundingMasterSheet.objects.get(plan__plan_date="2026-02-21")
        self.assertEqual(sheet.headers[1], "Compound Code")
        self.assertEqual(sheet.rows[0][1], "AG-NF 60")

        plan = CompoundingPlan.objects.get(plan_date="2026-02-21")
        spec = _sheet_spec_for_compound(plan, "AG-NF-60")
        self.assertIsNotNone(spec.get("mb_weight"))
        self.assertEqual(str(spec.get("mb_weight")), "36.54")
        self.assertEqual(str(spec.get("mb_min_stock")), "100")

    def test_sheet_spec_falls_back_to_latest_previous_master_sheet(self):
        if Workbook is None:
            self.skipTest("openpyxl not installed")
        wb = Workbook()
        ws = wb.active
        ws.title = "Master"
        ws.append(["Compound Code", "MB Weight", "MB Min Stock Lavel"])
        ws.append(["AG-7020", "36.465", "25000"])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        buf.seek(0)
        upload = SimpleUploadedFile(
            name="master_previous_day.xlsx",
            content=buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-23", "master_excel": upload},
        )
        current_plan = CompoundingPlan.objects.create(
            plan_date="2026-02-24",
            cjc_number=777,
            created_by=self.user,
        )

        spec = _sheet_spec_for_compound(current_plan, "AG-7020")
        self.assertEqual(str(spec.get("mb_weight")), "36.465")
        self.assertEqual(str(spec.get("mb_min_stock")), "25000")

    def test_planning_page_shows_latest_previous_master_sheet(self):
        self.client.post(
            reverse("compounding:upload_master_sheet"),
            data={"plan_date": "2026-02-23", "master_excel": self._xlsx_file("persistent_master.xlsx")},
        )

        response = self.client.get(reverse("compounding:planning"), {"date": "2026-02-24"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["master_sheet"]["exists"])
        self.assertTrue(response.context["master_sheet"]["is_carried_forward"])
        self.assertEqual(response.context["master_sheet"]["source_plan_date"].isoformat(), "2026-02-23")
        self.assertContains(response, "persistent_master.xlsx")
        self.assertContains(response, "AG-7020")
        self.assertContains(response, "Using the latest uploaded master sheet from 2026-02-23")

    def test_decimal_normalization_supports_plural_stock_level_headers(self):
        headers = [
            "SL. No",
            "Compound Code",
            "MB  Weight",
            "Chemicals  Weight",
            "MB & FB Total Weight",
            "MB Min Stock Lavel",
            "MB Correct Stock Levels",
            "FB Correct Stock Levels",
        ]
        rows = [["1", "AG-6015", "35.915", "1.08", "36.995", "500", "1849.7499999999998", "186.05500000000001"]]
        detected_headers, detected_rows, _cols = _sheet_table_with_detected_headers(headers, rows)
        normalized = _normalize_master_sheet_decimal_cells(detected_headers, detected_rows, places=3)
        self.assertEqual(normalized[0][6], "1849.75")
        self.assertEqual(normalized[0][7], "186.055")

    def test_decimal_normalization_supports_fb_total_weight_header(self):
        headers = [
            "SL. No",
            "Compound Code",
            "MB  Weight",
            "Chemicals  Weight",
            "FB Total Weight",
            "MB Min Stock Lavel",
            "MB Correct Stock Levels",
            "FB Correct Stock Levels",
        ]
        rows = [["1", "AG-8020", "35.969", "1.508", "37.477000000000004", "2000", "2061.235", "563.663"]]
        detected_headers, detected_rows, _cols = _sheet_table_with_detected_headers(headers, rows)
        normalized = _normalize_master_sheet_decimal_cells(detected_headers, detected_rows, places=3)
        self.assertEqual(normalized[0][4], "37.477")


class CompoundingTimeTests(TestCase):
    """Times show in IST, and a day follows the 6 AM IST rule, not the UTC clock."""

    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(username="time-user", email="time@example.com", password="secret123")
        self.client.force_login(self.user)

    def test_master_sheet_updated_time_is_shown_in_ist(self):
        from datetime import datetime, timezone as dt_timezone

        plan = CompoundingPlan.objects.create(plan_date="2026-11-24", cjc_number=700, created_by=self.user)
        sheet = CompoundingMasterSheet.objects.create(
            plan=plan, created_by=self.user, headers=["Compound Code", "MB Weight"], rows=[["AG-7020", "100"]],
        )
        CompoundingMasterSheet.objects.filter(pk=sheet.pk).update(updated_at=datetime(2026, 11, 24, 10, 0, tzinfo=dt_timezone.utc))

        response = self.client.get(reverse("compounding:planning"), {"date": "2026-11-24"})

        self.assertContains(response, "2026-11-24 15:30 IST")
        self.assertNotContains(response, "2026-11-24 10:00")

    def test_new_crin_without_a_date_uses_the_business_day_at_the_6am_gap(self):
        from datetime import datetime, timezone as dt_timezone
        from unittest import mock

        from compounding.models import CompoundingCRIN

        plan = CompoundingPlan.objects.create(plan_date="2026-11-24", cjc_number=701, created_by=self.user)
        CompoundingMasterSheet.objects.create(
            plan=plan, created_by=self.user, headers=["Compound Code", "MB Weight"], rows=[["AG-7020", "100"]],
        )
        # 05:45 IST on 25 Nov is still the 24 Nov production day, but the UTC date is already the 25th.
        at = datetime(2026, 11, 25, 0, 15, tzinfo=dt_timezone.utc)
        with mock.patch("django.utils.timezone.now", return_value=at):
            self.client.post(reverse("compounding:create_crin"), {"compound_code": "AG-7020", "quantity": "10"})

        crin = CompoundingCRIN.objects.get()
        self.assertEqual(crin.plan.plan_date.isoformat(), "2026-11-24")


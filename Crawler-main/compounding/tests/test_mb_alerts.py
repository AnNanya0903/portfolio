from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import CompoundingCRIN, CompoundingLine, CompoundingMasterSheet, CompoundingPlan
from compounding.views import _mb_shortage_alerts

PLAN_DATE = "2026-03-02"


class MBStockAlertTests(TestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="alert-user",
            email="alert@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)
        self.plan = CompoundingPlan.objects.create(plan_date=PLAN_DATE, cjc_number=500, created_by=self.user)

    def _sheet(self, mb_stock="400"):
        CompoundingMasterSheet.objects.update_or_create(
            plan=self.plan,
            defaults={
                "created_by": self.user,
                "headers": ["Compound Code", "MB Weight", "Chemicals Weight", "MB & FB Total Weight", "MB Correct Stock Lev", "FB Correct Stock Lev"],
                "rows": [["7020", "100", "20", "120", mb_stock, "0"]],
            },
        )

    def _crin(self, number="CRIN-1", quantity=1200, **extra):
        return CompoundingCRIN.objects.create(
            plan=self.plan,
            crin_number=number,
            compound_code="7020",
            quantity=quantity,
            created_by=self.user,
            **extra,
        )

    def test_short_stock_shows_alert_on_planning_and_fb_pages(self):
        self._sheet(mb_stock="400")
        self._crin()  # needs 1000

        for name in ("planning", "fb_planning"):
            response = self.client.get(reverse(f"compounding:{name}"), {"date": PLAN_DATE})
            self.assertContains(response, "MB stock is not available")
            self.assertContains(response, "short by")
            alert = response.context["mb_alerts"][0]
            self.assertEqual((alert["required"], alert["stock"], alert["short"]), ("1000", "400", "600"))

    def test_zero_stock_shows_alert(self):
        self._sheet(mb_stock="0")
        self._crin()

        alert = _mb_shortage_alerts(self.plan)[0]

        self.assertEqual(alert["stock"], "0")
        self.assertEqual(alert["short"], "1000")

    def test_enough_stock_shows_no_alert(self):
        self._sheet(mb_stock="1000")
        self._crin()

        response = self.client.get(reverse("compounding:planning"), {"date": PLAN_DATE})

        self.assertEqual(response.context["mb_alerts"], [])
        self.assertNotContains(response, "data-mb-alerts")

    def test_missing_stock_shows_alert(self):
        self._sheet(mb_stock="")
        self._crin()

        alert = _mb_shortage_alerts(self.plan)[0]

        self.assertFalse(alert["known"])

    def test_mb_on_the_board_is_not_counted_as_stock(self):
        self._sheet(mb_stock="400")
        self._crin()
        CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_mb=6)

        alert = _mb_shortage_alerts(self.plan)[0]

        self.assertEqual(alert["short"], "600")
        self.assertEqual(alert["incoming"], "600")

    def test_send_button_carries_popup_message_when_short(self):
        self._sheet(mb_stock="400")
        self._crin()

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        self.assertContains(response, "data-mb-alert=")
        self.assertIn("short by 600", response.context["crins"][0]["mb_alert"])

    def test_send_button_has_no_popup_when_stock_is_enough(self):
        self._sheet(mb_stock="1000")
        self._crin()

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        self.assertNotContains(response, "data-mb-alert=")

    def test_crin_with_no_fb_needs_no_popup(self):
        self._sheet(mb_stock="0")
        self._crin(fb_planning=0, mb_planning=2)

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        self.assertTrue(response.context["crins"][0]["mb_ready"])
        self.assertEqual(response.context["mb_alerts"], [])

    def test_raising_short_crin_warns_immediately(self):
        self._sheet(mb_stock="400")

        response = self.client.post(
            reverse("compounding:create_crin"),
            data={"plan_date": PLAN_DATE, "compound_code": "7020", "quantity": "1200"},
            follow=True,
        )

        text = " ".join(str(message) for message in response.context["messages"])
        self.assertIn("MB stock is not available for 7020", text)

    def test_server_still_blocks_short_send(self):
        self._sheet(mb_stock="400")
        crin = self._crin()

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )

        self.assertFalse(crin.planned_lines.exists())

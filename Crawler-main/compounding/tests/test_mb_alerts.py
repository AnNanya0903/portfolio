from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from compounding.models import (
    CompoundingCRIN,
    CompoundingLine,
    CompoundingMasterSheet,
    CompoundingPlan,
    CompoundingStockEntry,
)
from compounding.views import _mb_shortage_alerts
from dashboard.models import UserServiceAccess

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

    def _negative_stock(self, code="7020", quantity="-50"):
        CompoundingStockEntry.objects.create(
            compound_key=code,
            compound_code=code,
            stock_type=CompoundingStockEntry.STOCK_MB,
            entry_type=CompoundingStockEntry.ENTRY_OPENING,
            quantity=Decimal(quantity),
            plan=self.plan,
        )

    def test_negative_stock_alerts_on_every_screen_without_open_crins(self):
        self._sheet(mb_stock="0")
        self._negative_stock()

        for name in ("planning", "fb_planning", "mb_planning", "display"):
            response = self.client.get(reverse(f"compounding:{name}"), {"date": PLAN_DATE})
            self.assertContains(response, "MB stock is negative (-50)")
        stock = self.client.get(reverse("compounding:stock_ledger"))
        self.assertContains(stock, "data-stock-negative")

    def test_negative_stock_is_added_to_the_shortage(self):
        self._sheet(mb_stock="0")
        self._negative_stock()
        self._crin()  # needs 1000

        alert = _mb_shortage_alerts(self.plan)[0]

        self.assertTrue(alert["negative"])
        self.assertEqual((alert["stock"], alert["short"]), ("-50", "1050"))

    def test_saving_actuals_that_make_stock_negative_shows_alert(self):
        self._sheet(mb_stock="100")
        line = CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_fb=2)

        response = self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(line.id), "actual_mb": "0", "actual_fb": "2"},
            follow=True,
        )

        self.assertContains(response, "MB stock is negative (-100)")

    def test_board_user_sees_alert_without_planning_link(self):
        self._sheet(mb_stock="0")
        self._negative_stock()
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["compounding_board"])

        response = self.client.get(reverse("compounding:display"), {"date": PLAN_DATE})

        self.assertContains(response, "MB stock is negative (-50)")
        self.assertNotContains(response, "/compounding/planning/mb/")

    def test_short_crin_goes_to_mb_planning_first(self):
        self._sheet(mb_stock="400")

        response = self.client.post(
            reverse("compounding:create_crin"),
            data={"plan_date": PLAN_DATE, "compound_code": "7020", "quantity": "1200"},
        )

        self.assertIn("/compounding/planning/mb/", response["Location"])
        self.assertIn("compound=7020", response["Location"])

    def test_crin_with_enough_mb_does_not_go_to_mb_planning(self):
        self._sheet(mb_stock="1000")

        response = self.client.post(
            reverse("compounding:create_crin"),
            data={"plan_date": PLAN_DATE, "compound_code": "7020", "quantity": "1200"},
        )

        self.assertNotIn("/planning/mb/", response["Location"])

    def test_short_crin_has_no_send_button_in_fb_planning(self):
        self._sheet(mb_stock="400")
        crin = self._crin()

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        self.assertContains(response, "Waiting for MB")
        self.assertContains(response, "Plan MB first")
        self.assertNotContains(response, reverse("compounding:send_to_board", args=[crin.id]))

    def test_oldest_crin_gets_the_free_mb(self):
        self._sheet(mb_stock="1000")
        first = self._crin("CRIN-1", 1200)  # needs all 1000
        second = self._crin("CRIN-2", 1200)

        response = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})

        rows = {row["id"]: row for row in response.context["crins"]}
        self.assertTrue(rows[first.id]["mb_ready"])
        self.assertFalse(rows[second.id]["mb_ready"])
        self.assertEqual(rows[second.id]["mb_free"], "0")
        self.assertContains(response, reverse("compounding:send_to_board", args=[first.id]))
        self.assertNotContains(response, reverse("compounding:send_to_board", args=[second.id]))

    def test_crin_can_be_sent_after_mb_is_made(self):
        self._sheet(mb_stock="400")
        crin = self._crin()
        mb_line = CompoundingLine.objects.create(plan=self.plan, compound_code="7020", shift1_mb=6)
        self.client.post(
            reverse("compounding:set_actuals"),
            data={"plan_date": PLAN_DATE, "line_id": str(mb_line.id), "actual_mb": "6", "actual_fb": "0"},
        )

        page = self.client.get(reverse("compounding:fb_planning"), {"date": PLAN_DATE})
        self.assertContains(page, reverse("compounding:send_to_board", args=[crin.id]))
        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )

        self.assertTrue(crin.planned_lines.exists())

    def test_server_still_blocks_short_send(self):
        self._sheet(mb_stock="400")
        crin = self._crin()

        self.client.post(
            reverse("compounding:send_to_board", args=[crin.id]),
            data={"plan_date": PLAN_DATE, "shift": "1"},
        )

        self.assertFalse(crin.planned_lines.exists())

"""Enquiry Board: “Converted to Work Order” (Sales and Top Management), once per enquiry, kept after a refresh,
with its own filter option."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

import marketing.tests  # noqa: F401  (Python 3.14 test-client shim)
from enquiries import work_orders
from enquiries.models import WorkOrder
from kyc.models import Department, UserDepartment

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", DEBUG=True)
User = get_user_model()
ROWS = [
    {"enquiry_id": "ENQ-1", "status": "open", "customer_name": "Alpha Co", "general_attachments": []},
    {"enquiry_id": "ENQ-2", "status": "accepted", "customer_name": "Beta Co", "general_attachments": []},
]


def in_department(name, username):
    user = User.objects.create_user(username, f"{username}@example.com", "x")
    UserDepartment.objects.create(user=user, department=Department.objects.get(name=name))
    return user


@override_settings(**STATIC)
@mock.patch("enquiries.views._get_api_root", return_value="http://svc")
@mock.patch("enquiries.views.services.list_enquiries", side_effect=lambda *a, **k: [dict(r) for r in ROWS])
class WorkOrderTests(TestCase):
    def setUp(self):
        call_command("seed_departments")
        self.sales = in_department("Sales", "sales-person")
        self.top = in_department("Top Management", "top-person")
        self.marketing = in_department("Marketing", "marketing-person")

    def convert(self, user, enquiry_id="ENQ-1"):
        self.client.force_login(user)
        return self.client.post(reverse("enquiries:work_order_convert", args=[enquiry_id]),
                                HTTP_REFERER=reverse("enquiries:board"))

    def test_only_sales_top_management_and_superusers_may_convert(self, *_):
        self.assertTrue(work_orders.can_convert(self.sales))
        self.assertTrue(work_orders.can_convert(self.top))
        self.assertTrue(work_orders.can_convert(User.objects.create_superuser("boss", "b@example.com", "x")))
        self.assertFalse(work_orders.can_convert(self.marketing))
        self.assertFalse(work_orders.can_convert(User.objects.create_user("nobody", "n@example.com", "x")))

    def test_marking_an_enquiry_saves_when_and_who(self, *_):
        response = self.convert(self.sales)
        self.assertEqual(response.status_code, 302)
        order = WorkOrder.objects.get(enquiry_id="ENQ-1")
        self.assertEqual(order.converted_by, self.sales)
        self.assertIsNotNone(order.converted_at)

    def test_an_enquiry_can_only_be_converted_once(self, *_):
        self.convert(self.sales)
        self.convert(self.top)
        self.assertEqual(WorkOrder.objects.filter(enquiry_id="ENQ-1").count(), 1)
        self.assertEqual(WorkOrder.objects.get(enquiry_id="ENQ-1").converted_by, self.sales)

    def test_someone_else_cannot_convert(self, *_):
        response = self.convert(self.marketing)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(WorkOrder.objects.exists())

    def test_the_conversion_needs_a_post(self, *_):
        self.client.force_login(self.sales)
        self.assertEqual(self.client.get(reverse("enquiries:work_order_convert", args=["ENQ-1"])).status_code, 405)

    def test_the_enquiry_status_itself_is_not_changed(self, *_):
        self.convert(self.sales)
        self.assertEqual(ROWS[0]["status"], "open")

    def test_the_board_shows_the_badge_after_conversion_and_the_button_before(self, *_):
        self.client.force_login(self.sales)
        page = self.client.get(reverse("enquiries:board"))
        self.assertContains(page, "data-work-order-button")
        self.convert(self.sales)
        page = self.client.get(reverse("enquiries:board"))
        self.assertContains(page, "data-work-order-chip")
        self.assertEqual(page.content.decode().count("data-work-order-button"), 1)  # only ENQ-2 still has it

    def test_the_button_is_hidden_from_people_who_cannot_convert(self, *_):
        self.client.force_login(self.marketing)
        page = self.client.get(reverse("enquiries:board"))
        self.assertNotContains(page, "data-work-order-button")

    def test_the_status_filter_offers_and_shows_only_converted_enquiries(self, *_):
        self.convert(self.sales, "ENQ-1")
        self.client.force_login(self.sales)
        page = self.client.get(reverse("enquiries:board"), {"status": "work_order"})
        self.assertContains(page, '<option value="work_order" selected>')
        self.assertEqual([e["enquiry_id"] for e in page.context["enquiries"]], ["ENQ-1"])
        page = self.client.get(reverse("enquiries:board"), {"status": "accepted"})
        self.assertEqual([e["enquiry_id"] for e in page.context["enquiries"]], ["ENQ-2"])

    def test_the_conversion_is_remembered_across_page_loads(self, *_):
        self.convert(self.top, "ENQ-2")
        self.client.force_login(self.marketing)
        page = self.client.get(reverse("enquiries:board"))
        rows = {e["enquiry_id"]: e for e in page.context["enquiries"]}
        self.assertIsNotNone(rows["ENQ-2"]["work_order"])
        self.assertIsNone(rows["ENQ-1"]["work_order"])

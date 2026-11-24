from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess


class CompoundingAccessTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="compound-user",
            email="compound@example.com",
            password="secret123",
        )
        self.client.force_login(self.user)

    def _restrict_to(self, services):
        access, _created = UserServiceAccess.objects.get_or_create(user=self.user)
        access.restrict = True
        access.allowed_services = list(services)
        access.save(update_fields=["restrict", "allowed_services", "updated_at"])

    def test_compounding_home_denied_when_not_allowed(self):
        self._restrict_to(["enquiries"])
        response = self.client.get(reverse("compounding:board"))
        self.assertEqual(response.status_code, 302)

    def test_compounding_home_allows_planning_or_board(self):
        self._restrict_to(["compounding_planning"])
        response = self.client.get(reverse("compounding:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compound planning")

        self._restrict_to(["compounding_board"])
        response = self.client.get(reverse("compounding:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compound board")

    def test_compounding_home_has_no_ai_excel_import_button(self):
        response = self.client.get(reverse("compounding:board"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Import Excel with AI")
        self.assertContains(response, "Production record")

    def test_compounding_save_requires_planning(self):
        self._restrict_to(["compounding_board"])
        response = self.client.post(
            reverse("compounding:save"),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json().get("error"), "access_denied")

    def test_compounding_crin_create_requires_planning(self):
        self._restrict_to(["compounding_board"])
        response = self.client.post(
            reverse("compounding:create_crin"),
            data={"plan_date": "2026-02-03", "compound_folder": "16 - AG FRC-70/", "quantity": "10"},
        )
        self.assertEqual(response.status_code, 302)

    @override_settings(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz")
    def test_compounding_home_shows_iot_dashboard_when_bridge_is_configured(self):
        self._restrict_to(["compounding_board"])
        response = self.client.get(reverse("compounding:board"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open IOT dashboard")

    @override_settings(DEBUG=False, IOT_PORTAL_URL="https://iot.magicwand.biz")
    def test_compounding_iot_dashboard_is_available_when_bridge_is_configured(self):
        self._restrict_to(["compounding_board"])
        response = self.client.get(reverse("compounding:iot_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Compounding IOT Dashboard")

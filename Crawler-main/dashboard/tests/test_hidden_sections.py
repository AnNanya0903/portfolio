"""Tests for hiding unused sections from the menus (MAGICWAND_HIDDEN_SERVICES)."""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from kyc.tests import *  # noqa: F401,F403  (Python 3.14 test-client fix for Django 4.2)
from magicwand.access import user_has_service_access
from magicwand.context_processors import service_access

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
KEEP = {"enquiries", "marketing", "kyc"}
HIDE_OTHERS = {"gmail_ai", "design", "deskera", "mrp", "accounts", "compounding_planning", "compounding_board",
               "discussion", "document_scanner", "gasket_counter"}


class HiddenSectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)

    def _flags(self):
        request = RequestFactory().get("/")
        request.user = self.user
        return service_access(request)["mw_service_access"]

    def test_default_setting_shows_every_section(self):
        from django.conf import settings
        self.assertEqual(settings.MAGICWAND_HIDDEN_SERVICES, set())
        self.assertTrue(all(self._flags().values()))

    @override_settings(MAGICWAND_HIDDEN_SERVICES=HIDE_OTHERS)
    def test_setting_can_keep_only_enquiries_marketing_and_kyc(self):
        shown = {slug for slug, on in self._flags().items() if on}
        self.assertEqual(shown, KEEP)

    @override_settings(MAGICWAND_HIDDEN_SERVICES=set())
    def test_empty_setting_shows_everything_again(self):
        self.assertTrue(all(self._flags().values()))

    @override_settings(MAGICWAND_HIDDEN_SERVICES={"mrp"})
    def test_hiding_does_not_take_access_away(self):
        self.assertFalse(self._flags()["mrp"])
        self.assertTrue(user_has_service_access(self.user, "mrp"))

    @override_settings(MAGICWAND_HIDDEN_SERVICES={"mrp"})
    def test_a_restricted_user_still_sees_only_their_own_sections(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["kyc", "mrp"])
        shown = {slug for slug, on in self._flags().items() if on}
        self.assertEqual(shown, {"kyc"})

    @override_settings(MAGICWAND_HIDDEN_SERVICES=HIDE_OTHERS, **STATIC)
    def test_menus_show_kept_sections_and_no_empty_headings(self):
        page = self.client.get(reverse("kyc:list")).content.decode()
        for text in ("Enquiries", "Marketing", "KYC", "Core ERP"):
            self.assertIn(text, page)
        for text in (">Operations</summary>", ">Integrations</summary>", ">Intelligence</summary>",
                     ">Utilities</summary>", ">Collaboration</summary>",
                     "</i> Manufacturing", "</i> Gmail AI", "</i> Deskera MRP", "</i> Design Match",
                     "</i> Gasket Counter", "</i> Forum"):
            self.assertNotIn(text, page, text)

    @override_settings(**STATIC)
    def test_menus_show_every_section_by_default(self):
        page = self.client.get(reverse("kyc:list")).content.decode()
        for text in (">Operations</summary>", ">Integrations</summary>", ">Intelligence</summary>",
                     ">Utilities</summary>", ">Collaboration</summary>",
                     "</i> Manufacturing", "</i> Gmail AI", "</i> Deskera MRP", "</i> Design Match",
                     "</i> Gasket Counter", "</i> Forum"):
            self.assertIn(text, page, text)

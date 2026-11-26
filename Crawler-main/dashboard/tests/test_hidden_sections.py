"""Tests for hiding unused sections from the menus (MAGICWAND_HIDDEN_SERVICES)."""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from kyc.tests import *  # noqa: F401,F403  (Python 3.14 test-client fix for Django 4.2)
from magicwand.access import SERVICE_DEFINITIONS, user_has_service_access
from magicwand.context_processors import service_access

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")
KEEP = {"enquiries", "marketing", "kyc"}
HIDE_OTHERS = set(SERVICE_DEFINITIONS) - KEEP  # every other module, including ones added later


class HiddenSectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)

    def _flags(self):
        request = RequestFactory().get("/")
        request.user = self.user
        return service_access(request)["mw_service_access"]

    def test_default_setting_hides_only_the_rarely_used_sections(self):
        from django.conf import settings
        rarely_used = {"discussion", "document_scanner", "gasket_counter",
                       "mrp", "deskera", "accounts"}  # compounding and design are shown
        self.assertEqual(settings.MAGICWAND_HIDDEN_SERVICES, rarely_used)
        shown = {slug for slug, on in self._flags().items() if on}
        self.assertEqual(shown, set(SERVICE_DEFINITIONS) - rarely_used)
        for kept in ("gmail_ai", "enquiries", "marketing", "kyc", "quotations"):
            self.assertIn(kept, shown)

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
        for text in ("Enquiries", "Marketing", "KYC"):
            self.assertIn(text, page)
        for text in (">Operations</summary>", ">Integrations</summary>", ">Intelligence</summary>",
                     ">Utilities</summary>", ">Collaboration</summary>",
                     "</i> Manufacturing", "</i> Gmail AI", "</i> Deskera MRP", "</i> Design Match",
                     "</i> Gasket Counter", "</i> Forum"):
            self.assertNotIn(text, page, text)

    @override_settings(**STATIC)
    def test_default_menus_hide_the_rarely_used_sections_but_keep_the_rest(self):
        page = self.client.get(reverse("kyc:list")).content.decode()
        for text in ("</i> Gmail AI", "</i> Quotations", "</i> Compounding",
                     "</i> Design Match"):  # shown as top-level links, not inside groups
            self.assertIn(text, page, text)
        for text in (">Core ERP</summary>", ">Intelligence</summary>", ">Operations</summary>",
                     ">Integrations</summary>", ">Utilities</summary>", ">Collaboration</summary>",
                     "</i> Manufacturing", "</i> Deskera MRP", "</i> Accounts Portal",
                     "</i> Documents", "</i> Gasket Counter", "</i> Forum",
                     "Pending Tasks"):
            self.assertNotIn(text, page, text)

    @override_settings(MAGICWAND_HIDDEN_SERVICES=set(), **STATIC)
    def test_menus_show_every_section_when_nothing_is_hidden(self):
        page = self.client.get(reverse("kyc:list")).content.decode()
        for text in (">Operations</summary>", ">Integrations</summary>",
                     ">Utilities</summary>", ">Collaboration</summary>",
                     "</i> Manufacturing", "</i> Gmail AI", "</i> Deskera MRP", "</i> Design Match",
                     "</i> Gasket Counter", "</i> Forum"):
            self.assertIn(text, page, text)
        for text in (">Core ERP</summary>", ">Intelligence</summary>"):
            self.assertNotIn(text, page, text)

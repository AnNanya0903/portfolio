"""Tests for bringing existing customers into KYC: reading a document, checking it, and the sheet import."""
import io
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from kyc import extraction, sheet_import
from kyc.models import Customer, CustomerContact, KycDocument
from kyc.teams import team_from_text

MEDIA = tempfile.mkdtemp(prefix="kyc-test-media-")
BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", MEDIA_ROOT=MEDIA,
            DEBUG=True, GEMINI_API_KEY="test-key")

READ = {
    "customer": {"name": "Acme Seals Pvt Ltd", "address": "12 Industrial Area", "city": "Bengaluru",
                 "state": "Karnataka", "pincode": "560058", "country": "", "gstin": "29ABCDE1234F1Z5", "pan": "",
                 "phone": "", "website": ""},
    "contacts": [
        {"name": "Meera", "designation": "Purchase Manager", "email": "meera@acme.example", "phone": "",
         "category": "sales_marketing", "is_purchaser": True},
        {"name": "Ravi", "designation": "Accounts", "email": "ravi@acme.example", "phone": "",
         "category": "accounts", "is_purchaser": False},
    ],
}


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


def _photo(name="kyc.jpg", content_type="image/jpeg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff fake image bytes", content_type=content_type)


def _csv(text, name="customers.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class TeamTests(TestCase):
    def test_team_from_designation_or_department(self):
        self.assertEqual(team_from_text("Accounts"), "accounts")
        self.assertEqual(team_from_text("", "Design Engineer"), "technical")
        self.assertEqual(team_from_text("Managing Director"), "top_management")
        self.assertEqual(team_from_text("Purchase Manager"), "sales_marketing")
        self.assertEqual(team_from_text("Top management"), "top_management")
        self.assertEqual(team_from_text("", "Receptionist"), "sales_marketing")


class NormalizeTests(TestCase):
    def test_model_output_is_cleaned_and_contacts_get_a_team(self):
        data = extraction.normalize({
            "customer": {"name": "  Acme   Seals ", "gstin": "29 abcde 1234f1z5", "city": None, "extra": "x",
                         "address": {"oops": 1}},
            "contacts": [
                {"name": "Meera", "department": "Purchase", "email": "MEERA@Acme.example", "is_purchaser": True},
                {"name": "", "email": "", "phone": ""},
                {"email": "accounts@acme.example", "designation": "Finance head"},
                "not a contact",
            ],
        })
        self.assertEqual(data["customer"]["name"], "Acme Seals")
        self.assertEqual(data["customer"]["gstin"], "29ABCDE1234F1Z5")
        self.assertEqual(data["customer"]["city"], "")
        self.assertEqual(data["customer"]["address"], "")
        self.assertNotIn("extra", data["customer"])
        self.assertEqual(len(data["contacts"]), 2)
        self.assertEqual(data["contacts"][0], {"name": "Meera", "designation": "", "email": "meera@acme.example",
                                               "phone": "", "category": "sales_marketing", "is_purchaser": True})
        self.assertEqual(data["contacts"][1]["name"], "accounts@acme.example")
        self.assertEqual(data["contacts"][1]["category"], "accounts")

    def test_purchaser_without_email_is_not_marked(self):
        data = extraction.normalize({"contacts": [{"name": "Meera", "phone": "98450", "is_purchaser": True}]})
        self.assertFalse(data["contacts"][0]["is_purchaser"])

    def test_garbage_gives_empty_result(self):
        data = extraction.normalize({"customer": "x", "contacts": "y"})
        self.assertFalse(extraction.has_details(data))

    @override_settings(GEMINI_API_KEY="")
    def test_no_key_is_a_clear_error(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            with self.assertRaises(extraction.KycExtractionError):
                extraction.extract_kyc(text="Acme")

    @override_settings(GEMINI_API_KEY="test-key")
    def test_model_reply_is_parsed(self):
        genai = MagicMock()
        genai.GenerativeModel.return_value.generate_content.return_value.text = (
            '```json\n{"customer": {"name": "Acme"}, "contacts": []}\n```')
        with patch("kyc.extraction.scanner.genai", genai):
            data = extraction.extract_kyc(file_bytes=b"x", content_type="image/png", filename="a.png")
        self.assertEqual(data["customer"]["name"], "Acme")
        parts = genai.GenerativeModel.return_value.generate_content.call_args[0][0]
        self.assertEqual(parts[1], {"mime_type": "image/png", "data": b"x"})

    @override_settings(GEMINI_API_KEY="test-key")
    def test_model_failure_is_a_clear_error(self):
        genai = MagicMock()
        genai.GenerativeModel.return_value.generate_content.side_effect = RuntimeError("quota")
        with patch("kyc.extraction.scanner.genai", genai), self.assertLogs("kyc.extraction", level="ERROR"):
            with self.assertRaises(extraction.KycExtractionError):
                extraction.extract_kyc(text="Acme")


@override_settings(**BASE)
class DocumentImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)

    def _upload(self, **data):
        with patch("kyc.import_views.extraction.extract_kyc", return_value=READ) as extract:
            response = self.client.post(reverse("kyc:import_new"), data)
        return response, extract

    def _review_post(self, document, **changes):
        data = {"code": "AV-0001", "name": "Acme Seals Pvt Ltd", "country": "India", "gstin": "29ABCDE1234F1Z5",
                "contacts-TOTAL_FORMS": "2", "contacts-INITIAL_FORMS": "2",
                "contacts-MIN_NUM_FORMS": "0", "contacts-MAX_NUM_FORMS": "1000"}
        for i, contact in enumerate(READ["contacts"]):
            for key in ("name", "designation", "email", "phone", "category"):
                data[f"contacts-{i}-{key}"] = contact[key]
            if contact["is_purchaser"]:
                data[f"contacts-{i}-is_purchaser"] = "on"
        data.update(changes)
        return self.client.post(reverse("kyc:import_review", args=[document.pk]), data)

    def test_upload_reads_the_document_and_opens_the_check_page(self):
        response, extract = self._upload(document=_photo())
        document = KycDocument.objects.get()
        self.assertRedirects(response, reverse("kyc:import_review", args=[document.pk]))
        self.assertEqual(extract.call_args.kwargs["content_type"], "image/jpeg")
        self.assertTrue(extract.call_args.kwargs["file_bytes"].startswith(b"\xff\xd8"))
        self.assertIsNone(document.customer)
        self.assertEqual(document.uploaded_by, self.user)
        self.assertEqual(Customer.objects.count(), 0)

        page = self.client.get(reverse("kyc:import_review", args=[document.pk]))
        self.assertEqual(page.context["form"].initial["name"], "Acme Seals Pvt Ltd")
        self.assertEqual(page.context["form"].initial["country"], "India")
        self.assertEqual(len(page.context["contacts"].forms), 2)
        self.assertContains(page, "meera@acme.example")

    def test_pasted_text_works_without_a_file(self):
        response, extract = self._upload(pasted_text="Acme Seals, Bengaluru")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(extract.call_args.kwargs["text"], "Acme Seals, Bengaluru")
        self.assertFalse(KycDocument.objects.get().file)

    def test_nothing_chosen_or_wrong_type_is_rejected(self):
        response, extract = self._upload()
        self.assertContains(response, "Choose a photo or PDF")
        response, extract = self._upload(document=SimpleUploadedFile("x.html", b"<script>", content_type="text/html"))
        self.assertContains(response, "Upload a photo")
        response, extract = self._upload(document=_photo("x.svg", "image/svg+xml"))
        self.assertContains(response, "Upload a photo")
        extract.assert_not_called()
        self.assertEqual(KycDocument.objects.count(), 0)

    def test_when_reading_fails_the_document_is_kept_for_typing_in(self):
        with patch("kyc.import_views.extraction.extract_kyc",
                   side_effect=extraction.KycExtractionError("The document could not be read automatically.")):
            response = self.client.post(reverse("kyc:import_new"), {"document": _photo()}, follow=True)
        document = KycDocument.objects.get()
        self.assertEqual(document.extracted_data, {})
        self.assertContains(response, "could not be read automatically")
        self.assertEqual(len(response.context["contacts"].forms), 0)

    def test_saving_the_check_page_creates_customer_contacts_and_links_the_document(self):
        self._upload(document=_photo())
        document = KycDocument.objects.get()
        response = self._review_post(document)
        customer = Customer.objects.get(code="AV-0001")
        self.assertRedirects(response, reverse("kyc:detail", args=[customer.pk]))
        self.assertEqual(sorted(c.name for c in customer.contacts.all()), ["Meera", "Ravi"])
        self.assertTrue(customer.contacts.get(name="Meera").is_purchaser)
        document.refresh_from_db()
        self.assertEqual(document.customer, customer)
        # The check page cannot be used twice for the same document.
        again = self.client.get(reverse("kyc:import_review", args=[document.pk]))
        self.assertRedirects(again, reverse("kyc:detail", args=[customer.pk]))

    def test_a_contact_can_be_left_out_even_if_it_is_not_valid(self):
        self._upload(document=_photo())
        document = KycDocument.objects.get()
        self._review_post(document, **{"contacts-1-email": "not an email", "contacts-1-DELETE": "on"})
        self.assertEqual([c.name for c in Customer.objects.get().contacts.all()], ["Meera"])

    def test_an_invalid_contact_saves_nothing(self):
        self._upload(document=_photo())
        document = KycDocument.objects.get()
        response = self._review_post(document, **{"contacts-1-email": "not an email"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(CustomerContact.objects.count(), 0)

    def test_discard_removes_the_document(self):
        self._upload(document=_photo())
        document = KycDocument.objects.get()
        self.client.post(reverse("kyc:import_discard", args=[document.pk]))
        self.assertEqual(KycDocument.objects.count(), 0)

    def test_document_for_an_existing_customer_is_filed_without_reading(self):
        customer = Customer.objects.create(code="AV-0009", name="Bolt Works")
        response, extract = self._upload(document=_photo(), customer=str(customer.pk))
        self.assertRedirects(response, reverse("kyc:detail", args=[customer.pk]))
        extract.assert_not_called()
        self.assertEqual(customer.documents.count(), 1)
        self.assertContains(self.client.get(reverse("kyc:detail", args=[customer.pk])), "kyc.jpg")

    def test_file_is_served_only_to_signed_in_users(self):
        self._upload(document=_photo())
        document = KycDocument.objects.get()
        url = reverse("kyc:document_file", args=[document.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\xff\xd8"))
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)


SHEET = (
    "Customer code,Company name,City,GSTIN,Contact name,Designation,Email,Mobile,Team,Purchaser\n"
    "av-0001,Acme Seals,Bengaluru,29ABCDE1234F1Z5,Meera,Purchase Manager,meera@acme.example,9845000000,,\n"
    "AV-0001,,,,Ravi,,ravi@acme.example,,Accounts,\n"
    "AV-0002,Bolt Works,Pune,,,,,,,\n"
    ",No Code Ltd,,,,,,,,\n"
    "AV-0003,Bad Tax Ltd,,123,,,,,,\n"
    "AV-0004,Old Customer,,,,,,,,\n"
    "AV-0005,Gear Co,,,Asha,,not-an-email,,,\n"
)


@override_settings(**BASE)
class SheetImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sales", password="x")
        self.client.force_login(self.user)
        Customer.objects.create(code="AV-0004", name="Old Customer (kept)")

    def test_plan_groups_rows_and_reports_problems(self):
        plan = sheet_import.build_plan(sheet_import.read_rows(_csv(SHEET)))
        self.assertEqual([c["code"] for c in plan["customers"]], ["AV-0001", "AV-0002", "AV-0005"])
        acme = plan["customers"][0]
        self.assertEqual(acme["data"]["name"], "Acme Seals")
        self.assertEqual([(c["name"], c["category"], c["is_purchaser"]) for c in acme["contacts"]],
                         [("Meera", "sales_marketing", True), ("Ravi", "accounts", False)])
        self.assertEqual(plan["contact_count"], 2)
        self.assertEqual([s["code"] for s in plan["skipped"]], ["AV-0004"])
        self.assertEqual([p["row"] for p in plan["problems"]], [5, 6, 8])
        self.assertIn("No customer code", plan["problems"][0]["message"])
        self.assertIn("AV-0003", plan["problems"][1]["message"])
        self.assertIn("Contact left out", plan["problems"][2]["message"])

    def test_excel_file_with_numbers_in_cells(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["Customer Code", "Company Name", "PIN", "Contact Person", "Mobile"])
        sheet.append(["AV-0100", "Excel Co", 560058, "Kiran", 9845012345])
        buffer = io.BytesIO()
        book.save(buffer)
        upload = SimpleUploadedFile("customers.xlsx", buffer.getvalue())
        plan = sheet_import.build_plan(sheet_import.read_rows(upload))
        self.assertEqual(plan["customers"][0]["data"]["pincode"], "560058")
        self.assertEqual(plan["customers"][0]["contacts"][0]["phone"], "9845012345")

    def test_sheet_without_the_needed_headings_is_refused(self):
        with self.assertRaises(sheet_import.SheetError):
            sheet_import.read_rows(_csv("Company,City\nAcme,Pune\n"))
        with self.assertRaises(sheet_import.SheetError):
            sheet_import.read_rows(SimpleUploadedFile("customers.pdf", b"x"))

    def test_check_then_confirm_imports_and_leaves_existing_customers_alone(self):
        url = reverse("kyc:import_sheet")
        response = self.client.post(url, {"sheet": _csv(SHEET)})
        self.assertContains(response, "Import 3 customers")
        self.assertEqual(Customer.objects.count(), 1)  # nothing saved by the check

        response = self.client.post(url, {"action": "confirm"}, follow=True)
        self.assertContains(response, "3 customers and 2 contacts imported.")
        self.assertEqual(Customer.objects.count(), 4)
        self.assertEqual(Customer.objects.get(code="AV-0004").name, "Old Customer (kept)")
        acme = Customer.objects.get(code="AV-0001")
        self.assertEqual((acme.country, acme.created_by), ("India", self.user))
        self.assertEqual(acme.contacts.count(), 2)

        # Confirming twice does nothing: the checked sheet is used once.
        response = self.client.post(url, {"action": "confirm"}, follow=True)
        self.assertContains(response, "no longer available")
        self.assertEqual(Customer.objects.count(), 4)

    def test_code_added_between_check_and_confirm_is_left_out(self):
        url = reverse("kyc:import_sheet")
        self.client.post(url, {"sheet": _csv(SHEET)})
        Customer.objects.create(code="AV-0002", name="Added by someone else")
        response = self.client.post(url, {"action": "confirm"}, follow=True)
        self.assertContains(response, "1 left out")
        self.assertEqual(Customer.objects.get(code="AV-0002").name, "Added by someone else")

    def test_template_download_can_be_imported_back(self):
        response = self.client.get(reverse("kyc:sheet_template"))
        self.assertIn("attachment", response["Content-Disposition"])
        plan = sheet_import.build_plan(sheet_import.read_rows(_csv(response.content.decode())))
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["contact_count"], 1)
        self.assertTrue(plan["customers"][0]["contacts"][0]["is_purchaser"])

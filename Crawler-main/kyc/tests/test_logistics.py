"""Transportation, preferred transporter, door / go-down delivery, MSME and Udyam on KYC customers."""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from kyc import extraction, logistics, sheet_import
from kyc.forms import CustomerForm
from kyc.models import Customer

BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


def _form(**extra):
    return CustomerForm({"code": "AY-0001", "name": "AYSH", "country": "India"} | extra)


class LogisticsRuleTests(SimpleTestCase):
    def test_transporter(self):
        for text, expected in (("VRL", ("VRL", "")), ("arc", ("ARC", "")), ("VRL-TO PAY-DOOR", ("VRL", "")),
                               ("ARC Logistics", ("ARC", "")), ("Gati", ("OTHER", "Gati")), ("Other", ("OTHER", "")),
                               ("", ("", "")), (None, ("", ""))):
            self.assertEqual(logistics.transporter(text), expected, text)

    def test_delivery(self):
        for text, expected in (("DOOR DELIVERY", "DOOR"), ("VRL-TO PAY-DOOR", "DOOR"), ("Go-Down Delivery", "GODOWN"),
                               ("godown", "GODOWN"), ("Go down", "GODOWN"), ("", ""), ("To pay", "")):
            self.assertEqual(logistics.delivery(text), expected, text)

    def test_msme(self):
        for text, expected in (("YES", True), ("yes", True), ("Y", True), ("NO", False), ("Not applicable", False),
                               ("YES / NO", None), ("", None), (None, None), (True, True), (False, False)):
            self.assertEqual(logistics.msme(text), expected, text)

    def test_udyam(self):
        self.assertEqual(logistics.udyam(" udyam-tn-12-0012345 "), "UDYAM-TN-12-0012345")
        self.assertFalse(logistics.udyam_looks_wrong("UDYAM-TN-12-0012345"))
        self.assertTrue(logistics.udyam_looks_wrong("UDYAM-TN-0012345"))
        self.assertFalse(logistics.udyam_looks_wrong("29AABCU9603R1ZX"))  # older registrations are allowed


class CustomerFormLogisticsTests(TestCase):
    def test_new_customer_defaults_to_arc_and_not_msme(self):
        form = CustomerForm()
        self.assertEqual(form.initial["preferred_transporter"], "ARC")
        self.assertEqual(form["msme_applicable"].value(), "no")
        customer = _form().save()
        self.assertEqual((customer.preferred_transporter, customer.msme_applicable), ("ARC", False))

    def test_values_from_the_aysh_form_save(self):
        form = _form(transportation=" VRL-TO  PAY-DOOR ", preferred_transporter="VRL", delivery_type="DOOR",
                     msme_applicable="no")
        self.assertTrue(form.is_valid(), form.errors)
        customer = form.save()
        self.assertEqual(customer.transportation, "VRL-TO PAY-DOOR")
        self.assertEqual((customer.preferred_transporter, customer.delivery_type), ("VRL", "DOOR"))

    def test_other_transporter_needs_a_name_and_is_cleared_otherwise(self):
        form = _form(preferred_transporter="OTHER")
        self.assertFalse(form.is_valid())
        self.assertIn("transporter's name", form.errors["other_transporter"][0])
        self.assertTrue(_form(preferred_transporter="OTHER", other_transporter="Gati").is_valid())
        form = _form(preferred_transporter="ARC", other_transporter="Gati")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().other_transporter, "")

    def test_udyam_is_required_only_when_msme_applies(self):
        form = _form(msme_applicable="yes")
        self.assertFalse(form.is_valid())
        self.assertIn("Required when MSME applies", form.errors["udyam_aadhar_no"][0])
        form = _form(msme_applicable="yes", udyam_aadhar_no="udyam-tn-12-0012345")
        self.assertTrue(form.is_valid(), form.errors)
        customer = form.save()
        self.assertEqual((customer.msme_applicable, customer.udyam_aadhar_no), (True, "UDYAM-TN-12-0012345"))
        self.assertTrue(_form(code="AY-0009", msme_applicable="no").is_valid())
        self.assertFalse(_form(code="AY-0009", msme_applicable="yes", udyam_aadhar_no="UDYAM-TN-0012").is_valid())

    def test_editing_shows_the_saved_msme_choice(self):
        customer = Customer.objects.create(code="AY-0002", name="AYSH", msme_applicable=True,
                                           udyam_aadhar_no="UDYAM-TN-12-0012345")
        self.assertEqual(CustomerForm(instance=customer)["msme_applicable"].value(), "yes")

    def test_imports_may_pass_yes_and_no_words(self):
        form = _form(msme_applicable="Yes", udyam_aadhar_no="UDYAM-TN-12-0012345")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["msme_applicable"])


class ImportLogisticsTests(TestCase):
    def test_document_values_like_the_aysh_form(self):
        data = extraction.normalize({"customer": {
            "name": "AYSH", "transportation": "VRL-TO PAY-DOOR", "preferred_transporter": "VRL",
            "delivery": "DOOR DELIVERY", "msme": "YES / NO", "udyam_number": None}})
        customer = data["customer"]
        self.assertEqual(customer["transportation"], "VRL-TO PAY-DOOR")
        self.assertEqual((customer["preferred_transporter"], customer["other_transporter"]), ("VRL", ""))
        self.assertEqual(customer["delivery_type"], "DOOR")
        self.assertEqual(customer["msme_applicable"], "")  # both printed, none marked: left for a person
        self.assertEqual(customer["udyam_aadhar_no"], "")

    def test_document_without_transport_details_gets_arc_and_reads_delivery_from_transportation(self):
        customer = extraction.normalize({"customer": {"transportation": "ARC - Go down"}})["customer"]
        self.assertEqual((customer["preferred_transporter"], customer["delivery_type"]), ("ARC", "GODOWN"))
        self.assertEqual(extraction.normalize({"customer": {}})["customer"]["preferred_transporter"], "ARC")

    def test_an_empty_document_still_counts_as_having_no_details(self):
        self.assertFalse(extraction.has_details(extraction.normalize({"customer": {}, "contacts": []})))
        self.assertTrue(extraction.has_details(extraction.normalize({"customer": {"transportation": "VRL"}})))

    def test_sheet_columns(self):
        sheet = ("Customer code,Company name,Transportation,Preferred transporter,Door / Go-Down delivery,"
                 "MSME applicable,Udyam Aadhaar No.\n"
                 "AY-0001,AYSH,VRL-TO PAY-DOOR,VRL,Door delivery,Yes,UDYAM-TN-12-0012345\n"
                 "AB-0001,Bolt Works,,,,,\n")
        upload = SimpleUploadedFile("customers.csv", sheet.encode("utf-8"), content_type="text/csv")
        plan = sheet_import.build_plan(sheet_import.read_rows(upload))
        self.assertEqual(plan["problems"], [])
        by_code = {group["code"]: group["data"] for group in plan["customers"]}
        self.assertEqual(by_code["AY-0001"]["preferred_transporter"], "VRL")
        self.assertEqual(by_code["AY-0001"]["delivery_type"], "DOOR")
        self.assertEqual(by_code["AY-0001"]["msme_applicable"], "yes")
        self.assertEqual(by_code["AB-0001"]["preferred_transporter"], "ARC")
        self.assertEqual(by_code["AB-0001"]["msme_applicable"], "no")
        sheet_import.apply_plan(plan, None)
        saved = Customer.objects.get(code="AY-0001")
        self.assertEqual((saved.msme_applicable, saved.udyam_aadhar_no), (True, "UDYAM-TN-12-0012345"))

    def test_sheet_with_msme_but_no_udyam_is_reported(self):
        sheet = "Customer code,Company name,MSME applicable\nAY-0003,AYSH,Yes\n"
        upload = SimpleUploadedFile("customers.csv", sheet.encode("utf-8"), content_type="text/csv")
        plan = sheet_import.build_plan(sheet_import.read_rows(upload))
        self.assertEqual(plan["customers"], [])
        self.assertIn("Required when MSME applies", plan["problems"][0]["message"])

    def test_template_has_the_new_columns(self):
        header = sheet_import.template_csv().splitlines()[0]
        for column in ("Transportation", "Preferred transporter", "Door / Go-Down delivery", "MSME applicable",
                       "Udyam Aadhaar No."):
            self.assertIn(column, header)


@override_settings(**BASE)
class CustomerPageLogisticsTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("kycuser", "kyc@example.com", "x"))

    def test_create_and_show(self):
        response = self.client.get(reverse("kyc:new"))
        self.assertContains(response, 'data-field="udyam_aadhar_no"')
        self.assertContains(response, '<option value="VRL">')
        response = self.client.post(reverse("kyc:new"), {
            "code": "AY-0001", "name": "AYSH", "country": "India", "transportation": "VRL-TO PAY-DOOR",
            "preferred_transporter": "OTHER", "other_transporter": "Gati", "delivery_type": "GODOWN",
            "msme_applicable": "yes", "udyam_aadhar_no": "UDYAM-TN-12-0012345"})
        customer = Customer.objects.get(code="AY-0001")
        self.assertRedirects(response, reverse("kyc:detail", args=[customer.pk]))
        page = self.client.get(reverse("kyc:detail", args=[customer.pk]))
        for text in ("VRL-TO PAY-DOOR", "Gati", "Go-Down Delivery", "UDYAM-TN-12-0012345"):
            self.assertContains(page, text)

    def test_missing_udyam_shows_the_form_again(self):
        response = self.client.post(reverse("kyc:new"), {
            "code": "AY-0001", "name": "AYSH", "country": "India", "msme_applicable": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Required when MSME applies.")
        self.assertFalse(Customer.objects.exists())

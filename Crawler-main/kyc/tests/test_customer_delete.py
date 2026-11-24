"""Deleting a whole KYC customer: typed confirmation, what goes with it, and what stays."""
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from dashboard.models import UserServiceAccess
from kyc.models import Customer, CustomerContact, EnquiryCustomer, KycDocument, KycVerification

# Local file storage for uploaded KYC documents (production uses a Cloud Storage bucket).
MEDIA = tempfile.mkdtemp(prefix="kyc-delete-test-media-")
BASE = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage", MEDIA_ROOT=MEDIA, DEBUG=True)


@override_settings(**BASE)
class CustomerDeleteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("kycuser", "kyc@example.com", "x")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(code="AV-0001", name="Acme Seals")
        self.other = Customer.objects.create(code="AV-0002", name="Beta Pumps")
        CustomerContact.objects.create(customer=self.customer, name="Rahul", email="rahul@acme.example")
        CustomerContact.objects.create(customer=self.other, name="Meera", email="meera@beta.example")
        EnquiryCustomer.objects.create(enquiry_id="ENQ-1", customer=self.customer)
        self.url = reverse("kyc:delete", args=[self.customer.pk])

    def test_detail_page_shows_what_will_be_deleted(self):
        page = self.client.get(reverse("kyc:detail", args=[self.customer.pk]))
        self.assertContains(page, "Delete this customer")
        self.assertContains(page, "1 contact")
        self.assertContains(page, "the link to 1 enquiry")
        self.assertContains(page, 'name="confirm_code"')

    def test_typing_the_code_deletes_the_customer_and_everything_under_it(self):
        response = self.client.post(self.url, {"confirm_code": " av-0001 "}, follow=True)
        self.assertRedirects(response, reverse("kyc:list"))
        self.assertContains(response, "Customer AV-0001 - Acme Seals was deleted.")
        self.assertFalse(Customer.objects.filter(pk=self.customer.pk).exists())
        self.assertFalse(CustomerContact.objects.filter(name="Rahul").exists())
        self.assertFalse(EnquiryCustomer.objects.filter(enquiry_id="ENQ-1").exists())
        # Other customers are untouched.
        self.assertTrue(Customer.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(CustomerContact.objects.filter(name="Meera").exists())

    def test_wrong_or_missing_code_deletes_nothing(self):
        for typed in ("", "AV-0002", "Acme Seals"):
            response = self.client.post(self.url, {"confirm_code": typed}, follow=True)
            self.assertContains(response, "Not deleted. Type the customer code AV-0001 exactly")
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())
        self.assertEqual(CustomerContact.objects.filter(customer=self.customer).count(), 1)

    def test_get_request_never_deletes(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())

    def test_uploaded_document_file_is_removed_too(self):
        document = KycDocument.objects.create(customer=self.customer, original_name="kyc.txt")
        document.file.save("kyc.txt", ContentFile(b"kyc form"), save=True)
        storage, name = document.file.storage, document.file.name
        self.assertTrue(storage.exists(name))
        self.client.post(self.url, {"confirm_code": "AV-0001"})
        self.assertFalse(KycDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(storage.exists(name))

    def test_verification_requests_go_with_the_customer(self):
        fields = {f.name for f in KycVerification._meta.get_fields()}
        self.assertIn("customer", fields)
        self.client.post(self.url, {"confirm_code": "AV-0001"})
        self.assertFalse(KycVerification.objects.filter(customer_id=self.customer.pk).exists())

    def test_users_without_kyc_access_cannot_delete(self):
        UserServiceAccess.objects.create(user=self.user, restrict=True, allowed_services=["enquiries"])
        self.client.post(self.url, {"confirm_code": "AV-0001"})
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())

    def test_signed_out_visitors_cannot_delete(self):
        self.client.logout()
        self.client.post(self.url, {"confirm_code": "AV-0001"})
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())

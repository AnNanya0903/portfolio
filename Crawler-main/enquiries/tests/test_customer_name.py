"""Edit customer (name and email) from the enquiry board."""
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from enquiries.models import EnquiryContact

STATIC = dict(STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage")


@override_settings(**STATIC)
class BoardEditCustomerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("boss", "b@example.com", "x")
        self.client.force_login(self.user)
        self.rows = [
            {"enquiry_id": "N1", "status": "open", "subject": "Valve RFQ", "customer_name": "Valve RFQ",
             "received_at_dt": datetime(2026, 5, 2)},
        ]
        patch("enquiries.views.services.list_enquiries",
              side_effect=lambda *a, **k: [dict(r) for r in self.rows]).start()
        self.update = patch("enquiries.views.services.update_enquiry", return_value={}).start()
        self.addCleanup(patch.stopall)

    def test_board_shows_name_saved_email_and_edit_form(self):
        EnquiryContact.objects.create(enquiry_id="N1", customer_email="buyer@acme.example")
        response = self.client.get(reverse("enquiries:board"))
        self.assertContains(response, "Edit customer")
        self.assertContains(response, 'value="buyer@acme.example"')
        self.assertNotContains(response, "Customer not set")

    def _save(self, **data):
        return self.client.post(reverse("enquiries:board"),
                                {"action": "update_customer", "enquiry_id": "N1", **data}, follow=True)

    def test_edit_customer_saves_name_and_email(self):
        response = self._save(customer_name="  Beta   Pumps ", customer_email="Ops@Beta.example")
        self.update.assert_called_once()
        self.assertEqual(self.update.call_args.args[1:3], ("N1", {"customer_name": "Beta Pumps"}))
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="N1").customer_email, "ops@beta.example")
        self.assertContains(response, "Customer for enquiry #N1 set to Beta Pumps (ops@beta.example).")

    def test_edit_customer_can_clear_a_saved_email(self):
        EnquiryContact.objects.create(enquiry_id="N1", customer_email="old@x.example")
        self._save(customer_name="Beta Pumps", customer_email="")
        self.assertEqual(EnquiryContact.objects.get(enquiry_id="N1").customer_email, "")

    def test_edit_customer_without_email_creates_no_contact_row(self):
        self._save(customer_name="Beta Pumps")
        self.assertFalse(EnquiryContact.objects.filter(enquiry_id="N1").exists())

    def test_edit_customer_rejects_empty_name_and_bad_email(self):
        response = self._save(customer_name="   ", customer_email="a@b.example")
        self.assertContains(response, "Please enter the customer name.")
        response = self._save(customer_name="Beta", customer_email="not-an-email")
        self.assertContains(response, "valid customer email")
        self.update.assert_not_called()
        self.assertFalse(EnquiryContact.objects.exists())

    def test_service_error_is_shown(self):
        from enquiries import services
        self.update.side_effect = services.EnquiryServiceError("down")
        response = self._save(customer_name="Beta", customer_email="ops@beta.example")
        self.assertContains(response, "Unable to update the customer: down")
        self.assertFalse(EnquiryContact.objects.exists())

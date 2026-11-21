"""Once an enquiry is Closed its share page cannot be accepted or rejected (it can still be read)."""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from enquiries.models import ShareLink
from enquiries.tests.share_helpers import make_share


class ClosedShareTests(TestCase):
    def setUp(self):
        url, self.password = make_share("E1")
        self.record = ShareLink.objects.get(enq_no="E1")
        self.url = reverse("enquiries:share_pretty", args=["magicwand", "E1", self.record.id])

    def _answer(self, status, action):
        with patch("enquiries.views.services.fetch_enquiry", return_value={"enquiry_id": "E1", "status": status}), \
                patch("enquiries.views.services.update_enquiry") as update, \
                patch("enquiries.views.status_emails.send_status_email"):
            response = self.client.post(reverse("enquiries:share_action"),
                                        {"token": self.record.token, "action": action, "reason": "no"})
        return response, update

    def test_closed_enquiry_refuses_accept_and_reject(self):
        for action in ("accept", "reject"):
            response, update = self._answer("closed", action)
            self.assertEqual(response.status_code, 403)
            update.assert_not_called()

    def test_open_enquiry_still_accepts(self):
        response, update = self._answer("open", "accept")
        self.assertEqual(response.status_code, 200)
        update.assert_called_once()

    def test_closed_share_page_is_read_only(self):
        with patch("enquiries.views.services.fetch_enquiry", return_value={"enquiry_id": "E1", "status": "closed"}):
            response = self.client.post(self.url, {"share_password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-share-action="accept"')
        self.assertContains(response, "is closed")

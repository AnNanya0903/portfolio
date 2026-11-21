"""Test helper: give an enquiry a share link (as if its quotation had been sent with one)."""
import secrets

from django.contrib.auth.hashers import make_password
from django.core import signing

from enquiries.models import EnquiryContact, ShareLink

BASE = "https://portal.example"


def make_share(enquiry_id, password="AG-testpass", save_on_contact=True, pdf=b"%PDF-1.4 test"):
    """Create the enquiry's latest share link. Returns (url, password)."""
    import base64

    data = {"enquiry_id": enquiry_id, "pdf_content_b64": base64.b64encode(pdf).decode(),
            "pdf_filename": "q.pdf", "requires_password": True, "password_hash": make_password(password)}
    token = signing.dumps(data, salt="enquiry-share", compress=True)
    record = ShareLink.objects.create(id=secrets.token_urlsafe(9), token=token, brand="magicwand",
                                      enq_no=str(enquiry_id).upper())
    url = f"{BASE}/enquiries/share/magicwand/{record.enq_no}/{record.id}/"
    if save_on_contact:
        contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=enquiry_id)
        contact.follow_up_share_url, contact.follow_up_share_password = url, password
        contact.save()
    return url, password

"""KYC portal: the customer master.

Each customer has one customer code, which is the identity everything else maps to (enquiries,
quotations, emails). Contacts are grouped by team so automatic emails reach the right people:
payment reminders to accounts, drawings to technical, escalations to top management.
"""
from django.conf import settings
from django.db import models

from docscanner.storage import document_scan_storage

User = settings.AUTH_USER_MODEL


class Customer(models.Model):
    KYC_DRAFT = "draft"
    KYC_SENT = "sent"
    KYC_VERIFIED = "verified"
    KYC_CHANGES = "changes"
    KYC_REJECTED = "rejected"
    KYC_STATUS_CHOICES = (
        (KYC_DRAFT, "Not verified"),
        (KYC_SENT, "Sent to customer"),
        (KYC_VERIFIED, "Verified by customer"),
        (KYC_CHANGES, "Customer asked for changes"),
        (KYC_REJECTED, "Rejected by customer"),
    )
    TRANSPORTER_CHOICES = [
        ('ARC', 'ARC'),
        ('VRL', 'VRL'),
        ('OTHER', 'Other'),
        ]
    
    DELIVERY_CHOICES = [
        ('DOOR', 'Door Delivery'),
        ('GODOWN', 'Go-Down Delivery'),
    ]

    # Stored in capitals without spaces, so "av-001" and "AV-001" are the same customer.
    code = models.CharField(
        "Customer code", max_length=32, unique=True,
        error_messages={"unique": "Another customer already has this customer code."},
    )
    name = models.CharField("Company name", max_length=255)
    address = models.TextField(blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state = models.CharField(max_length=120, blank=True, default="")
    pincode = models.CharField("PIN / postal code", max_length=20, blank=True, default="")
    country = models.CharField(max_length=120, blank=True, default="India")

# Add these inside your existing KYC model

    transportation = models.CharField( max_length=150, blank=True, null=True ) 
    preferred_transporter = models.CharField( max_length=100, choices=TRANSPORTER_CHOICES, default='ARC', blank=True) 
    other_transporter = models.CharField( max_length=100, blank=True, null=True)
    delivery_type = models.CharField( max_length=20, choices=DELIVERY_CHOICES, blank=True, null=True)
    msme_applicable = models.BooleanField(default=False)
    udyam_aadhar_no = models.CharField(max_length=50, blank=True, null=True )
    gstin = models.CharField("GSTIN", max_length=15, blank=True, default="")
    pan = models.CharField("PAN", max_length=10, blank=True, default="")
    phone = models.CharField("Company phone", max_length=40, blank=True, default="")
    website = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    kyc_status = models.CharField(max_length=10, choices=KYC_STATUS_CHOICES, default=KYC_DRAFT)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kyc_customer"
        ordering = ["name", "code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class CustomerContact(models.Model):
    CATEGORY_SALES = "sales_marketing"
    CATEGORY_TECHNICAL = "technical"
    CATEGORY_ACCOUNTS = "accounts"
    CATEGORY_MANAGEMENT = "top_management"
    CATEGORY_CHOICES = (
        (CATEGORY_SALES, "Sales and marketing"),
        (CATEGORY_TECHNICAL, "Technical / design"),
        (CATEGORY_ACCOUNTS, "Accounts"),
        (CATEGORY_MANAGEMENT, "Top management"),
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    category = models.CharField("Team", max_length=20, choices=CATEGORY_CHOICES)
    name = models.CharField(max_length=200)
    designation = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    # The purchaser is the person enquiry and quotation emails go to.
    is_purchaser = models.BooleanField("Purchaser", default=False)
    notes = models.CharField(max_length=500, blank=True, default="")

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kyc_customer_contact"
        ordering = ["-is_purchaser", "name"]

    def __str__(self) -> str:
        return f"{self.customer.code}:{self.category}:{self.name}"


class KycDocument(models.Model):
    """An existing KYC form (photo, PDF or pasted text) that a customer's details were read from.

    Uploaded first, read automatically, then checked by a person. `customer` is empty until that
    person saves the customer.
    """

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, null=True, blank=True, related_name="documents")
    file = models.FileField(upload_to="kyc/%Y/%m/", storage=document_scan_storage, blank=True)
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    pasted_text = models.TextField(blank=True, default="")
    # {"customer": {...}, "contacts": [{...}]} as read from the document; empty when it could not be read.
    extracted_data = models.JSONField(default=dict, blank=True)
    error_message = models.CharField(max_length=500, blank=True, default="")
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kyc_document"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"KycDocument:{self.pk}:{self.original_name or 'pasted text'}"

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")


class KycVerification(models.Model):
    """One email asking the customer to check the details we hold, and what they answered.

    The email has a private link (`token`) to a page with the details and three answers:
    all correct, changes needed, or reject.
    """

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_DRY_RUN = "dry_run"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Sending"),
        (STATUS_SENT, "Sent"),
        (STATUS_DRY_RUN, "Test mode (not sent)"),
        (STATUS_FAILED, "Failed"),
    )

    RESPONSE_CONFIRMED = "confirmed"
    RESPONSE_CHANGES = "changes"
    RESPONSE_REJECTED = "rejected"
    RESPONSE_CHOICES = (
        (RESPONSE_CONFIRMED, "All details are correct"),
        (RESPONSE_CHANGES, "Some details need to change"),
        (RESPONSE_REJECTED, "Rejected"),
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="verifications")
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    note = models.CharField(max_length=500, blank=True, default="")  # why it was not sent
    sent_to = models.CharField(max_length=1000, blank=True, default="")  # comma-separated addresses
    cc = models.CharField(max_length=1000, blank=True, default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")
    gmail_message_id = models.CharField(max_length=255, blank=True, default="")
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    response = models.CharField(max_length=10, choices=RESPONSE_CHOICES, blank=True, default="")
    response_comment = models.TextField(blank=True, default="")
    responded_at = models.DateTimeField(null=True, blank=True)
    # The details as the customer saw them when they answered.
    response_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "kyc_verification"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"KycVerification:{self.customer_id}:{self.status}:{self.response or 'no answer'}"


class EnquiryCustomer(models.Model):
    """Which KYC customer an enquiry belongs to. Enquiries live in the enquiry service, so only the id is kept."""

    enquiry_id = models.CharField(max_length=128, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="enquiry_links")
    linked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "kyc_enquiry_customer"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EnquiryCustomer:{self.enquiry_id}:{self.customer_id}"

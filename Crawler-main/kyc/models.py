"""KYC portal: the customer master.

Each customer has one customer code, which is the identity everything else maps to (enquiries,
quotations, emails). Contacts are grouped by team so automatic emails reach the right people:
payment reminders to accounts, drawings to technical, escalations to top management.
"""
from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class Customer(models.Model):
    KYC_DRAFT = "draft"
    KYC_SENT = "sent"
    KYC_VERIFIED = "verified"
    KYC_CHANGES = "changes"
    KYC_STATUS_CHOICES = (
        (KYC_DRAFT, "Not verified"),
        (KYC_SENT, "Sent to customer"),
        (KYC_VERIFIED, "Verified by customer"),
        (KYC_CHANGES, "Customer asked for changes"),
    )

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

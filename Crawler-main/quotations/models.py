"""Quotation and feasibility reports: every version worked out for a customer, and how it was approved.

Costing uploads the PDF (and the Excel working) as a new version. Management approves it or asks for a
revision. Only the latest version, once approved, is the "active" PDF that may go to the customer.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import models

from docscanner.storage import document_scan_storage

User = get_user_model()


class Quotation(models.Model):
    STATUS_PENDING = "pending"
    STATUS_REVISION = "revision"
    STATUS_APPROVED = "approved"
    STATUS_SENT = "sent"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Waiting for approval"),
        (STATUS_REVISION, "Needs revision"),
        (STATUS_APPROVED, "Approved, OK to send"),
        (STATUS_SENT, "Sent to customer"),
    )

    number = models.CharField(max_length=20, unique=True, blank=True, default="")  # QT-00001, set on first save
    customer = models.ForeignKey("kyc.Customer", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="quotations")
    customer_name = models.CharField(max_length=255)
    enquiry_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    title = models.CharField("Quotation for", max_length=255)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quotations_quotation"
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return f"{self.number or 'Quotation'}: {self.title}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"QT-{self.pk:05d}"
            super().save(update_fields=["number"])

    @property
    def latest_version(self):
        return self.versions.order_by("-number").first()

    @property
    def active_version(self):
        """The only PDF that may go to the customer: the latest version, and only once it is approved."""
        latest = self.latest_version
        return latest if latest is not None and latest.status == QuotationVersion.STATUS_APPROVED else None


class QuotationVersion(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REVISION = "revision"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Waiting for approval"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REVISION, "Needs revision"),
        (STATUS_SUPERSEDED, "Replaced by a newer version"),
    )

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    pdf = models.FileField(upload_to="quotations/%Y/%m/", storage=document_scan_storage)
    pdf_name = models.CharField(max_length=255, blank=True, default="")
    excel = models.FileField(upload_to="quotations/%Y/%m/", storage=document_scan_storage, blank=True)
    excel_name = models.CharField(max_length=255, blank=True, default="")
    price_note = models.CharField("Quoted price", max_length=255, blank=True, default="")
    note = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.TextField(blank=True, default="")

    class Meta:
        db_table = "quotations_version"
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["quotation", "number"], name="quotation_version_number")]

    def __str__(self) -> str:
        return f"{self.quotation.number} v{self.number}"


class QuotationEvent(models.Model):
    """The tracker: one line each time something happens to a quotation."""

    ACTION_UPLOADED = "uploaded"
    ACTION_APPROVED = "approved"
    ACTION_REVISION = "revision"
    ACTION_PRICE_MODIFIED = "price_modified"
    ACTION_SENT = "sent"
    ACTION_CHOICES = (
        (ACTION_UPLOADED, "New version uploaded"),
        (ACTION_APPROVED, "Price approved by management, OK to send to customer"),
        (ACTION_REVISION, "Price needs revision"),
        (ACTION_PRICE_MODIFIED, "Price has been modified by management"),
        (ACTION_SENT, "Sent to customer"),
    )

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="events")
    version = models.ForeignKey(QuotationVersion, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="events")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    comment = models.TextField(blank=True, default="")
    # Management's corrected Excel, attached when asking for a revision.
    file = models.FileField(upload_to="quotations/%Y/%m/", storage=document_scan_storage, blank=True)
    file_name = models.CharField(max_length=255, blank=True, default="")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotations_event"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.quotation.number}: {self.action}"

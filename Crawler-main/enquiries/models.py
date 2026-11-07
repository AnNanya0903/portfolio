from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class ShareLink(models.Model):
    id = models.CharField(primary_key=True, max_length=22)
    token = models.TextField()
    brand = models.CharField(max_length=50)
    enq_no = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "enquiries_sharelink"
        indexes = [
            models.Index(fields=["brand", "enq_no"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.brand}:{self.enq_no}:{self.id}"


class GmailLeadAnalysis(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="gmail_lead_analyses",
    )
    message_id = models.CharField(max_length=128)
    mailbox = models.EmailField(blank=True, null=True)
    subject = models.CharField(max_length=500, blank=True)
    sender = models.CharField(max_length=320, blank=True)
    received_at = models.DateTimeField(blank=True, null=True)

    is_lead = models.BooleanField(default=False)
    lead_score = models.PositiveSmallIntegerField(default=0)
    reason = models.TextField(blank=True)
    next_step = models.CharField(max_length=200, blank=True)

    model_name = models.CharField(max_length=64, blank=True)
    analyzed_at = models.DateTimeField(default=timezone.now)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "enquiries_gmail_lead_analysis"
        unique_together = [("user", "message_id")]
        indexes = [
            models.Index(fields=["user", "message_id"]),
            models.Index(fields=["user", "is_lead", "analyzed_at"]),
        ]
        ordering = ["-analyzed_at"]

    def __str__(self) -> str:
        return f"LeadAnalysis:{self.user_id}:{self.message_id}:{'lead' if self.is_lead else 'no'}"


class FollowUpSent(models.Model):
    """Record of a manual follow-up email sent to a customer for an enquiry."""

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
    )

    enquiry_id = models.CharField(max_length=128, db_index=True)
    customer_email = models.EmailField(blank=True)
    customer_name = models.CharField(max_length=320, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    gmail_message_id = models.CharField(max_length=255, blank=True, null=True)
    sent_at = models.DateTimeField(default=timezone.now)
    sent_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="follow_ups_sent")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    gmail_thread_id = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    # One-time token from the compose form; unique so a double-submitted form cannot send twice.
    submission_token = models.CharField(max_length=64, unique=True, null=True, blank=True)
    stage = models.CharField(max_length=32, blank=True, default="")  # template used, e.g. "waiting_po"
    next_follow_up_on = models.DateField(null=True, blank=True)  # when to follow up again

    SOURCE_MANUAL = "manual"
    SOURCE_AUTO = "auto"
    SOURCE_CHOICES = ((SOURCE_MANUAL, "Manual"), (SOURCE_AUTO, "Automatic"))
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)

    class Meta:
        db_table = "enquiries_follow_up_sent"
        indexes = [
            models.Index(fields=["enquiry_id"]),
            models.Index(fields=["sent_by", "-sent_at"]),
        ]
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"FollowUpSent:{self.enquiry_id}:{self.sent_at:%Y-%m-%d %H:%M}"


class EnquiryContact(models.Model):
    """Customer email entered on the enquiries board for an enquiry (the enquiry service has no email field)."""

    enquiry_id = models.CharField(max_length=128, unique=True)
    customer_email = models.EmailField(blank=True)
    auto_follow_up_paused = models.BooleanField(default=False)  # sales paused automatic follow-ups

    # Payment for an accepted order. Empty means "pending"; when it is paid the order becomes converted.
    PAYMENT_PENDING = "pending"
    PAYMENT_PARTIAL = "partial"
    PAYMENT_PAID = "paid"
    PAYMENT_STATUS_CHOICES = (
        (PAYMENT_PENDING, "Payment pending"),
        (PAYMENT_PARTIAL, "Partially paid"),
        (PAYMENT_PAID, "Paid"),
    )
    payment_status = models.CharField(max_length=16, choices=PAYMENT_STATUS_CHOICES, blank=True, default="")
    payment_amount_received = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payment_due_on = models.DateField(null=True, blank=True)
    payment_paid_on = models.DateField(null=True, blank=True)  # date the full payment was received
    payment_reference = models.CharField(max_length=120, blank=True, default="")  # UTR / cheque / receipt no.
    payment_updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    payment_updated_at = models.DateTimeField(null=True, blank=True)

    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "enquiries_contact"

    def __str__(self) -> str:
        return f"EnquiryContact:{self.enquiry_id}"


class CustomerStatusEmail(models.Model):
    """Automatic email to the customer: acknowledgement, rejection reason request, payment received, closing.

    One row per enquiry and event (`event_key`), so the same event never emails twice.
    """

    KIND_ACKNOWLEDGEMENT = "acknowledgement"
    KIND_REJECTION = "rejection"
    KIND_PAYMENT_RECEIVED = "payment_received"
    KIND_CLOSING = "closing"
    KIND_CHOICES = (
        (KIND_ACKNOWLEDGEMENT, "Enquiry acknowledgement"),
        (KIND_REJECTION, "Rejection reason request"),
        (KIND_PAYMENT_RECEIVED, "Payment received"),
        (KIND_CLOSING, "Closing (no reply)"),
    )

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_SKIPPED = "skipped"
    STATUS_FAILED = "failed"
    STATUS_DRY_RUN = "dry_run"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_SKIPPED, "Not sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_DRY_RUN, "Test mode"),
    )

    enquiry_id = models.CharField(max_length=128, db_index=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    event_key = models.CharField(max_length=64)  # the kind; one email per enquiry and kind
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    note = models.CharField(max_length=500, blank=True, default="")  # why it was not sent / the error
    customer_email = models.EmailField(blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    gmail_message_id = models.CharField(max_length=255, blank=True, default="")
    gmail_thread_id = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "enquiries_customer_status_email"
        constraints = [
            models.UniqueConstraint(fields=["enquiry_id", "event_key"], name="uniq_status_email_per_event"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CustomerStatusEmail:{self.enquiry_id}:{self.event_key}:{self.status}"

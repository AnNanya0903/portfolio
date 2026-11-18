from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from docscanner.storage import document_scan_storage


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
    cc = models.CharField(max_length=1000, blank=True, default="")  # comma-separated CC addresses

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


class FollowUpStart(models.Model):
    """Where an automatic follow-up sequence starts, and the Gmail thread every later email replies in.

    Created when the quotation or proforma invoice is emailed with "Send to customer"
    (stage "waiting_po" / "payment_pending"), and when the rejection email is sent (stage "marketing").
    """

    enquiry_id = models.CharField(max_length=128, db_index=True)
    stage = models.CharField(max_length=32)
    gmail_thread_id = models.CharField(max_length=255, blank=True, default="")
    gmail_message_id = models.CharField(max_length=255, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")
    cc = models.CharField(max_length=1000, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    document_no = models.CharField(max_length=64, blank=True, default="")  # quotation / PI voucher number
    started_at = models.DateTimeField(default=timezone.now)
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        db_table = "enquiries_follow_up_start"
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"FollowUpStart:{self.enquiry_id}:{self.stage}:{self.started_at:%Y-%m-%d}"


class EnquiryContact(models.Model):
    """Customer email entered on the enquiries board for an enquiry (the enquiry service has no email field)."""

    enquiry_id = models.CharField(max_length=128, unique=True)
    customer_email = models.EmailField(blank=True)
    auto_follow_up_paused = models.BooleanField(default=False)  # sales paused automatic follow-ups
    follow_up_cc = models.CharField(max_length=1000, blank=True, default="")  # CC used for this enquiry's follow-ups
    # More customer addresses besides customer_email (comma separated). CC'd on every email to the customer.
    other_customer_emails = models.CharField(max_length=1000, blank=True, default="")

    # Payment for an accepted order; when it is paid the order becomes converted. The amount, due date, paid-on
    # date and reference below are confidential: they are kept but no longer shown or asked for anywhere.
    PAYMENT_PENDING = "pending"
    PAYMENT_PARTIAL = "partial"
    PAYMENT_CREDIT = "credit"  # payment on credit terms: due credit_period_days after credit_starts_on
    PAYMENT_PAID = "paid"
    PAYMENT_STATUS_CHOICES = (
        (PAYMENT_PENDING, "Payment Pending"),
        (PAYMENT_PARTIAL, "Partially Paid"),
        (PAYMENT_CREDIT, "Credit Period"),
        (PAYMENT_PAID, "Paid"),
    )
    payment_status = models.CharField(max_length=16, choices=PAYMENT_STATUS_CHOICES, default=PAYMENT_PENDING)
    payment_amount_received = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    payment_due_on = models.DateField(null=True, blank=True)
    # Credit period agreed with the customer: payment is due credit_period_days after credit_starts_on
    # (invoice / dispatch date). Payment reminders wait until then.
    credit_period_days = models.PositiveSmallIntegerField(null=True, blank=True)
    credit_starts_on = models.DateField(null=True, blank=True)
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


class EnquiryNegotiation(models.Model):
    """Where a quotation stands when the customer neither accepts nor rejects it (asks a question instead).

    Kept next to the enquiry's own status (open / accepted / converted / rejected), which the enquiry
    service owns. An enquiry with no row here is not in negotiation.
    """

    STAGE_NONE = ""
    STAGE_NEGOTIATION = "under_negotiation"
    STAGE_REVISED = "revised_quotation_sent"
    STAGE_CHOICES = (
        (STAGE_NONE, "No negotiation"),
        (STAGE_NEGOTIATION, "Under negotiation"),
        (STAGE_REVISED, "Revised quotation sent"),
    )

    enquiry_id = models.CharField(max_length=64, unique=True)
    stage = models.CharField(max_length=24, choices=STAGE_CHOICES, blank=True, default=STAGE_NONE)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "enquiry_negotiation"

    def __str__(self) -> str:
        return f"{self.enquiry_id}: {self.get_stage_display()}"


class EnquiryStatusLog(models.Model):
    """Every status change of an enquiry: who, when, from what, to what."""

    enquiry_id = models.CharField(max_length=64, db_index=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    username = models.CharField(max_length=150, blank=True, default="")
    old_status = models.CharField(max_length=40, blank=True, default="")
    new_status = models.CharField(max_length=40, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    lost_reason = models.CharField(max_length=200, blank=True, default="")  # when the enquiry was rejected
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "enquiry_status_log"
        ordering = ["-created_at", "-id"]


class EnquiryAssignment(models.Model):
    """An enquiry forwarded to a department as a task. Forwarding does not change who owns the enquiry."""

    PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH = "LOW", "MEDIUM", "HIGH"
    PRIORITY_CHOICES = ((PRIORITY_LOW, "Low"), (PRIORITY_MEDIUM, "Medium"), (PRIORITY_HIGH, "High"))
    STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED = "PENDING", "IN_PROGRESS", "COMPLETED"
    STATUS_CHOICES = ((STATUS_PENDING, "Pending"), (STATUS_IN_PROGRESS, "In progress"), (STATUS_COMPLETED, "Completed"))

    enquiry_id = models.CharField(max_length=64, db_index=True)
    customer_name = models.CharField(max_length=255, blank=True, default="")  # as it was when forwarded
    from_department = models.ForeignKey("kyc.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    to_department = models.ForeignKey("kyc.Department", on_delete=models.PROTECT, related_name="enquiry_tasks")
    assigned_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Empty: any user in the target department can pick it up.
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="enquiry_tasks")
    comment = models.TextField()
    due_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=8, choices=PRIORITY_CHOICES, default=PRIORITY_MEDIUM)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "enquiry_assignments"
        ordering = ["-created_at", "-id"]

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_date and self.status != self.STATUS_COMPLETED and self.due_date < timezone.localdate())


class EnquiryFollowup(models.Model):
    """A follow-up with the customer (or about the enquiry), with the date of the next one."""

    TYPE_EMAIL, TYPE_CALL, TYPE_MEETING, TYPE_OTHER = "EMAIL", "CALL", "MEETING", "OTHER"
    TYPE_CHOICES = ((TYPE_EMAIL, "Email"), (TYPE_CALL, "Call"), (TYPE_MEETING, "Meeting"), (TYPE_OTHER, "Other"))

    enquiry_id = models.CharField(max_length=64, db_index=True)
    assignment = models.ForeignKey(EnquiryAssignment, on_delete=models.SET_NULL, null=True, blank=True, related_name="followups")
    department = models.ForeignKey("kyc.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.TextField()
    followup_type = models.CharField(max_length=8, choices=TYPE_CHOICES, default=TYPE_EMAIL)
    next_followup_date = models.DateField(null=True, blank=True)
    next_done = models.BooleanField(default=False)  # the next follow-up has been done, so it is no longer due
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "enquiry_followups"
        ordering = ["-created_at", "-id"]

    @property
    def is_due(self) -> bool:
        return bool(self.next_followup_date and not self.next_done and self.next_followup_date <= timezone.localdate())


class EnquiryAttachment(models.Model):
    """A file attached to an enquiry, optionally to a forwarded task or a follow-up."""

    CAT_CUSTOMER_EMAIL, CAT_COST_BREAKUP, CAT_QUOTATION, CAT_OTHER = "CUSTOMER_EMAIL", "COST_BREAKUP", "QUOTATION", "OTHER"
    CATEGORY_CHOICES = ((CAT_CUSTOMER_EMAIL, "Customer email"), (CAT_COST_BREAKUP, "Cost breakup"),
                        (CAT_QUOTATION, "Quotation"), (CAT_OTHER, "Other"))

    enquiry_id = models.CharField(max_length=64, db_index=True)
    assignment = models.ForeignKey(EnquiryAssignment, on_delete=models.SET_NULL, null=True, blank=True, related_name="attachments")
    followup = models.ForeignKey(EnquiryFollowup, on_delete=models.SET_NULL, null=True, blank=True, related_name="attachments")
    file = models.FileField(upload_to="enquiry-files/%Y/%m/", storage=document_scan_storage, max_length=300)
    file_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    category = models.CharField(max_length=14, choices=CATEGORY_CHOICES, default=CAT_OTHER)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        # Not "enquiry_attachments": the enquiry service already has a table of that name in the same database.
        db_table = "enquiry_files"
        ordering = ["-created_at", "-id"]


class AutomationRun(models.Model):
    """One run of the scheduled automations (follow-ups, customer emails, reply checks): when, what, how it ended.

    Also the lock that stops two runs from overlapping: a run that has not finished within
    AutomationRun.STALE_MINUTES is treated as dead.
    """

    STALE_MINUTES = 15

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=20, default="cron")  # cron, command or manual
    ok = models.BooleanField(default=False)
    summary = models.TextField(blank=True, default="")  # JSON: what each step did

    class Meta:
        db_table = "enquiry_automation_runs"
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"{self.started_at:%Y-%m-%d %H:%M} {self.trigger} {'ok' if self.ok else 'not ok'}"

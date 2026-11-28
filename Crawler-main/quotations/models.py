"""Quotation and feasibility reports: every version worked out for a customer, and how it was approved.

Costing uploads the PDF (and the Excel working) as a new version, as a draft or straight for approval.
Management approves it or asks for a revision. Only the latest version, once approved, is the "active" PDF
that may go to the customer: on approval it is frozen as quotations/approved/{number}_v{n}.pdf with its
SHA-256, and only that exact file can be emailed. The CRO emails it in the enquiry's own email thread and
records the customer's answer; an accepted quotation unlocks the proforma invoice.

    draft -> pending -> approved -> sent -> accepted (proforma invoice unlocked) / declined (reason)
    pending -> revision (rejected, with the reason) -> pending (a new version)
A new version after approval (an edit) puts the quotation back to pending.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import models

from docscanner.storage import document_scan_storage

User = get_user_model()


class Quotation(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_REVISION = "revision"
    STATUS_APPROVED = "approved"
    STATUS_SENT = "sent"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING, "Pending approval"),
        (STATUS_REVISION, "Rejected, needs revision"),
        (STATUS_APPROVED, "Approved, OK to send"),
        (STATUS_SENT, "Sent to customer"),
        (STATUS_ACCEPTED, "Customer accepted"),
        (STATUS_DECLINED, "Customer declined"),
    )

    number = models.CharField(max_length=20, unique=True, blank=True, default="")  # QT-00001, set on first save
    customer = models.ForeignKey("kyc.Customer", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="quotations")
    customer_name = models.CharField(max_length=255)
    # The enquiry this quotation answers: a real ENQ-NNNN number of the enquiry service (checked when it is set,
    # see quotations/enquiry_link.py), or empty. One enquiry has many quotations; a quotation has one enquiry.
    # The enquiries live in their own service and database, so this is a checked number, not a database key.
    enquiry_id = models.CharField("Enquiry number", max_length=64, blank=True, default="", db_index=True)
    # The customer's own reference for the enquiry, e.g. "AG-QR-ALSTOM-235" (free text, what used to be typed in
    # the enquiry box before quotations were linked to real enquiries).
    enquiry_reference = models.CharField("Customer reference (AG-QR)", max_length=120, blank=True, default="")
    title = models.CharField("Quotation for", max_length=255)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The last email to the customer (send or resend from the portal).
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sent_to = models.CharField(max_length=1000, blank=True, default="")  # comma separated
    sent_cc = models.CharField(max_length=1000, blank=True, default="")
    sent_message_id = models.CharField(max_length=255, blank=True, default="")  # RFC Message-ID header
    sent_thread_id = models.CharField(max_length=255, blank=True, default="")  # Gmail threadId / Graph conversation

    # The customer's answer, recorded by the CRO.
    customer_reference = models.CharField("PO / reference number", max_length=120, blank=True, default="")
    customer_response_note = models.TextField(blank=True, default="")  # note on acceptance, reason on decline
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

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

    @property
    def proforma_unlocked(self) -> bool:
        """The hook for the proforma invoice stage: open once the customer accepted the quotation."""
        return self.status == self.STATUS_ACCEPTED


class QuotationVersion(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REVISION = "revision"
    STATUS_SUPERSEDED = "superseded"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
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
    # Frozen on approval (decided_by / decided_at are the approver and the time): an immutable copy of the
    # approved PDF and its SHA-256. Only this file, unchanged, can be emailed to the customer.
    frozen_pdf = models.FileField(upload_to="quotations/approved/", storage=document_scan_storage, blank=True,
                                  max_length=300)
    sha256 = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "quotations_version"
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["quotation", "number"], name="quotation_version_number")]

    def __str__(self) -> str:
        return f"{self.quotation.number} v{self.number}"


class QuotationEvent(models.Model):
    """The tracker (audit trail): one line each time something happens to a quotation, or to the email
    thread of its enquiry. Shown on the quotation and on the enquiry, to every department."""

    ACTION_UPLOADED = "uploaded"
    ACTION_SUBMITTED = "submitted"
    ACTION_APPROVED = "approved"
    ACTION_REVISION = "revision"
    ACTION_PRICE_MODIFIED = "price_modified"
    ACTION_SENT = "sent"
    ACTION_RESENT = "resent"
    ACTION_SEND_FAILED = "send_failed"
    ACTION_ACCEPTED = "accepted"
    ACTION_DECLINED = "declined"
    ACTION_EMAIL_LINKED = "email_linked"
    ACTION_ENQUIRY_CHANGED = "enquiry_changed"
    ACTION_ENQUIRY_MERGED = "enquiry_merged"
    ACTION_LINKS_REVOKED = "links_revoked"
    ACTION_PASSWORD_SENT = "password_sent"
    ACTION_PASSWORD_FAILED = "password_failed"
    ACTION_CHOICES = (
        (ACTION_UPLOADED, "New version uploaded"),
        (ACTION_SUBMITTED, "Submitted for approval"),
        (ACTION_APPROVED, "Price approved by management, OK to send to customer"),
        (ACTION_REVISION, "Price needs revision"),
        (ACTION_PRICE_MODIFIED, "Price has been modified by management"),
        (ACTION_SENT, "Sent to customer"),
        (ACTION_RESENT, "Sent to customer again"),
        (ACTION_SEND_FAILED, "Email to customer failed"),
        (ACTION_ACCEPTED, "Customer accepted"),
        (ACTION_DECLINED, "Customer declined"),
        (ACTION_EMAIL_LINKED, "Customer's enquiry email linked"),
        (ACTION_ENQUIRY_CHANGED, "Enquiry changed"),
        (ACTION_ENQUIRY_MERGED, "Duplicate enquiry merged"),
        (ACTION_LINKS_REVOKED, "Older share links revoked"),
        (ACTION_PASSWORD_SENT, "Share link password sent"),
        (ACTION_PASSWORD_FAILED, "Share link password email failed"),
    )

    # Empty only for events of the enquiry itself (the customer's email linked before any quotation).
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    enquiry_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    version = models.ForeignKey(QuotationVersion, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="events")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    comment = models.TextField(blank=True, default="")
    # Management's corrected Excel, attached when asking for a revision.
    file = models.FileField(upload_to="quotations/%Y/%m/", storage=document_scan_storage, blank=True)
    file_name = models.CharField(max_length=255, blank=True, default="")
    # Recipients, Message-ID, thread, error... whatever explains the event.
    details = models.JSONField(default=dict, blank=True)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotations_event"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.quotation.number if self.quotation_id else self.enquiry_id}: {self.action}"

    # Comments on these are internal pricing talk (upload notes, approval and revision comments): only costing and
    # management see them. Everyone sees that the event happened.
    INTERNAL_COMMENT_ACTIONS = frozenset({ACTION_UPLOADED, ACTION_SUBMITTED, ACTION_APPROVED, ACTION_REVISION,
                                          ACTION_PRICE_MODIFIED})

    @property
    def has_internal_comment(self) -> bool:
        return self.action in self.INTERNAL_COMMENT_ACTIONS

    def save(self, *args, **kwargs):
        if not self.enquiry_id and self.quotation_id:
            self.enquiry_id = self.quotation.enquiry_id or ""
        super().save(*args, **kwargs)


class EnquiryLinkSuggestion(models.Model):
    """For a quotation entered before quotations were linked to enquiries: the enquiry it most likely answers.

    Made by `manage.py suggest_quotation_enquiries` (safe to run again). Nothing is linked until an administrator
    confirms the suggestion, or picks the right enquiry, on the review page.
    """

    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = ((STATUS_PENDING, "To review"), (STATUS_CONFIRMED, "Linked"), (STATUS_REJECTED, "Rejected"))

    CONFIDENCE_HIGH = "high"
    CONFIDENCE_MEDIUM = "medium"
    CONFIDENCE_LOW = "low"
    CONFIDENCE_CHOICES = ((CONFIDENCE_HIGH, "High"), (CONFIDENCE_MEDIUM, "Medium"), (CONFIDENCE_LOW, "Low"))

    quotation = models.OneToOneField(Quotation, on_delete=models.CASCADE, related_name="enquiry_suggestion")
    enquiry_id = models.CharField(max_length=64, blank=True, default="")  # empty: no match was found
    enquiry_customer = models.CharField(max_length=255, blank=True, default="")
    enquiry_subject = models.CharField(max_length=500, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    confidence = models.CharField(max_length=8, choices=CONFIDENCE_CHOICES, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    decided_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quotations_enquiry_suggestion"
        ordering = ["status", "-updated_at", "-id"]

    def __str__(self) -> str:
        return f"{self.quotation.number} -> {self.enquiry_id or '?'} ({self.confidence or 'none'})"

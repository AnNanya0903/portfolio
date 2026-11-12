"""Marketing portal: lost enquiries (Rejected or No response) and the work done to win them back.

The enquiries themselves live in the enquiry service. These tables only hold what marketing adds:
the reason the enquiry was lost, do-not-contact, notes and calls, and the revived enquiry.
"""
from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class MarketingLead(models.Model):
    REASON_CHOICES = (
        ("price", "Price too high"),
        ("delivery", "Delivery / lead time"),
        ("quality", "Quality / specification"),
        ("competitor", "Went to a competitor"),
        ("payment_terms", "Payment terms"),
        ("cancelled", "Project cancelled"),
        ("no_reply", "Never replied"),
        ("other", "Other"),
    )

    enquiry_id = models.CharField(max_length=128, unique=True)
    # Customer name typed by marketing, used when the enquiry's name is missing or is really an email subject.
    customer_name = models.CharField(max_length=255, blank=True, default="")
    lost_reason = models.CharField(max_length=20, choices=REASON_CHOICES, blank=True, default="")
    do_not_contact = models.BooleanField(default=False)
    revived_enquiry_id = models.CharField(max_length=128, blank=True, default="")
    revived_at = models.DateTimeField(null=True, blank=True)
    revived_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "marketing_lead"

    def __str__(self) -> str:
        return f"MarketingLead:{self.enquiry_id}"


class MarketingActivity(models.Model):
    KIND_NOTE = "note"
    KIND_CALL = "call"
    KIND_EMAIL = "email"
    KIND_SYSTEM = "system"  # reason changed, do-not-contact, revived
    KIND_CHOICES = (
        (KIND_NOTE, "Note"),
        (KIND_CALL, "Call"),
        (KIND_EMAIL, "Email"),
        (KIND_SYSTEM, "Update"),
    )

    lead = models.ForeignKey(MarketingLead, on_delete=models.CASCADE, related_name="activities")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_NOTE)
    text = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "marketing_activity"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"MarketingActivity:{self.lead.enquiry_id}:{self.kind}"


class Campaign(models.Model):
    """An email sent to a group of lost customers (chosen with the same filters as the Marketing list)."""

    STATUS_DRAFT = "draft"
    STATUS_SENT = "sent"
    STATUS_CHOICES = ((STATUS_DRAFT, "Draft"), (STATUS_SENT, "Sent"))

    name = models.CharField(max_length=200)
    subject = models.CharField(max_length=300)
    body = models.TextField()
    # Audience filters, same keys as the Marketing list: kind, reason, fin_year, q.
    filters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sent_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    replies_checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "marketing_campaign"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Campaign:{self.pk}:{self.name}"


class CampaignRecipient(models.Model):
    """One customer email address in a sent campaign, and what happened."""

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

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    customer_email = models.EmailField()
    customer_name = models.CharField(max_length=320, blank=True, default="")
    enquiry_id = models.CharField(max_length=128, db_index=True)  # the lost enquiry this customer came from
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    note = models.CharField(max_length=500, blank=True, default="")
    subject = models.CharField(max_length=300, blank=True, default="")
    body = models.TextField(blank=True, default="")
    gmail_message_id = models.CharField(max_length=255, blank=True, default="")
    gmail_thread_id = models.CharField(max_length=255, blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "marketing_campaign_recipient"
        constraints = [
            models.UniqueConstraint(fields=["campaign", "customer_email"], name="uniq_campaign_recipient_email"),
        ]
        ordering = ["customer_name", "customer_email"]

    def __str__(self) -> str:
        return f"CampaignRecipient:{self.campaign_id}:{self.customer_email}:{self.status}"

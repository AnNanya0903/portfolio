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

    enquiry_id = models.CharField(max_length=128, db_index=True)
    customer_email = models.EmailField(blank=True)
    customer_name = models.CharField(max_length=320, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    gmail_message_id = models.CharField(max_length=255, blank=True, null=True)
    sent_at = models.DateTimeField(default=timezone.now)
    sent_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="follow_ups_sent")

    class Meta:
        db_table = "enquiries_follow_up_sent"
        indexes = [
            models.Index(fields=["enquiry_id"]),
            models.Index(fields=["sent_by", "-sent_at"]),
        ]
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"FollowUpSent:{self.enquiry_id}:{self.sent_at:%Y-%m-%d %H:%M}"

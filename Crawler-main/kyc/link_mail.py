"""Emails for KYC by shareable link, and the pluggable senders that deliver them.

Email is the only working channel. `WhatsAppSender` is the interface for a later WhatsApp integration and does
nothing yet. Every email is signed by the AVIGIRI team (dashboard.follow_up_templates.sender_signature).

The copy kept in EmailLog never contains the private link or the OTP: both are blanked out before logging, so the
database holds only hashes of them.

Test mode (KYC_LINK_DRY_RUN, on by default) records what would be sent and sends nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string

from dashboard.follow_up_templates import sender_signature
from dashboard.gmail_service import GmailAPIError, GmailService

from . import mailer
from .models import EmailLog, KycRequest

logger = logging.getLogger(__name__)

SENT = "sent"
DRY_RUN = "dry_run"
FAILED = "failed"

SUBJECTS = {
    "link": "AVIGIRI: please complete your KYC details",
    "reminder": "Reminder: please complete your KYC details for AVIGIRI",
    "rejected": "AVIGIRI: a few KYC details need your attention",
    "confirm": "AVIGIRI: please confirm the KYC details we entered for you",
    "otp": "Your AVIGIRI KYC verification code",
    "submitted": "KYC submitted: {company}",
}


@dataclass
class SendResult:
    status: str
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (SENT, DRY_RUN)


def dry_run() -> bool:
    return bool(getattr(settings, "KYC_LINK_DRY_RUN", True))


def system_sender():
    """The user whose Gmail sends the emails nobody clicked a button for (KYC_LINK_SENDER)."""
    username = str(getattr(settings, "KYC_LINK_SENDER", "") or "").strip()
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


class LinkSender(Protocol):
    """One delivery channel. Must not raise for a delivery problem: the result says what happened."""

    channel: str

    def send(self, *, to: str, subject: str, body: str, log_body: str, user, customer) -> SendResult: ...


class EmailSender:
    channel = "email"

    def send(self, *, to: str, subject: str, body: str, log_body: str, user, customer) -> SendResult:
        msg, problems = mailer.check([to], [], [], subject, body)
        if problems:
            return SendResult(FAILED, "; ".join(problems)[:500])
        if dry_run():
            return SendResult(DRY_RUN, "test mode: nothing was sent")
        sender = user if getattr(user, "is_authenticated", False) else system_sender()
        if sender is None:
            return SendResult(FAILED, "KYC_LINK_SENDER is not set to an active user")
        try:
            gmail_id = mailer._send_raw(GmailService(sender), msg)
        except GmailAPIError as exc:
            self._log(customer, sender, to, subject, log_body, EmailLog.STATUS_FAILED, str(exc))
            return SendResult(FAILED, str(exc)[:500])
        except Exception as exc:  # network and library errors must not break the page
            logger.exception("KYC link email to %s failed", to)
            return SendResult(FAILED, f"send failed: {exc}"[:500])
        self._log(customer, sender, to, subject, log_body, EmailLog.STATUS_SENT, "", gmail_id)
        return SendResult(SENT)

    @staticmethod
    def _log(customer, sender, to, subject, body, status, error="", gmail_id=""):
        try:
            EmailLog.objects.create(customer=customer, sent_by=sender, to=to, subject=subject[:300], body=body,
                                    status=status, error=error[:500], gmail_message_id=(gmail_id or "")[:255])
        except Exception:
            logger.exception("KYC link email could not be logged")


class WhatsAppSender:
    """Interface only. A WhatsApp Business API integration goes here; until then it reports that it is off."""

    channel = "whatsapp"

    def send(self, *, to: str, subject: str, body: str, log_body: str, user, customer) -> SendResult:
        return SendResult(FAILED, "WhatsApp sending is not set up")


SENDERS = {"email": EmailSender(), "whatsapp": WhatsAppSender()}


def _render(name: str, context: dict) -> str:
    context = {**context, "signature": sender_signature()}
    return render_to_string(f"kyc/link/emails/{name}.txt", context).strip() + "\n"


def _deliver(req: KycRequest, kind: str, context: dict, *, user=None, secret: str = "", to: str = "",
             channel: str = "email") -> SendResult:
    to = to or req.recipient_email
    body = _render(kind, {"req": req, "customer": req.customer, **context})
    log_body = body.replace(secret, "(private)") if secret else body
    subject = SUBJECTS[kind].format(company=req.customer.name)
    return SENDERS[channel].send(to=to, subject=subject, body=body, log_body=log_body, user=user,
                                 customer=req.customer)


def send_link(req: KycRequest, link: str, token: str, *, user=None, kind: str = "link") -> SendResult:
    """kind: link (first or resent), rejected, or confirm."""
    return _deliver(req, kind, {"link": link, "valid_until": req.expires_at}, user=user, secret=token)


def send_otp(req: KycRequest, code: str, minutes: int) -> SendResult:
    return _deliver(req, "otp", {"code": code, "minutes": minutes}, secret=code)


def send_reminder(req: KycRequest) -> SendResult:
    return _deliver(req, "reminder", {"valid_until": req.expires_at, "sent_on": req.sent_at})


def notify_team(req: KycRequest, addresses, detail_url: str) -> SendResult:
    """Tell the customer's assigned team that the form was submitted (one email, every address in To)."""
    addresses = sorted({a.strip().lower() for a in addresses if a and "@" in a})
    if not addresses:
        return SendResult(FAILED, "the customer has no team email address")
    return _deliver(req, "submitted", {"detail_url": detail_url}, to=", ".join(addresses))

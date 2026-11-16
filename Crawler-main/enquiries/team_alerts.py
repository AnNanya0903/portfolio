"""Alert emails to the team about what happens to enquiries (Enquiries and Marketing portals).

One email per event, To TEAM_ALERT_TO with TEAM_ALERT_CC copied (kumar@avigiri.com, with manju@ and apai@ in
CC). Leave TEAM_ALERT_TO empty to turn alerts off (staging does).

Alerts never block the action that caused them: every problem is returned as text, never raised.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from dashboard.gmail_service import GmailService

from .models import TeamAlertSent

logger = logging.getLogger(__name__)


def alert_recipients() -> Tuple[str, List[str]]:
    """(To, [CC]) from TEAM_ALERT_TO / TEAM_ALERT_CC, lower-cased, never repeating the To address."""
    to = (getattr(settings, "TEAM_ALERT_TO", "") or "").strip().lower()
    cc: List[str] = []
    for part in str(getattr(settings, "TEAM_ALERT_CC", "") or "").replace(";", ",").split(","):
        address = part.strip().lower()
        if address and address != to and address not in cc:
            cc.append(address)
    return to, cc


def _sender(user):
    """The signed-in user whose Gmail sends the alert; for events with nobody signed in (a customer clicking
    Accept, the scheduled follow-ups) the FOLLOW_UP_AUTO_SENDER account."""
    if getattr(user, "is_authenticated", False):
        return user
    username = (getattr(settings, "FOLLOW_UP_AUTO_SENDER", "") or "").strip()
    return get_user_model().objects.filter(username=username, is_active=True).first() if username else None


def _first_time(key: str) -> bool:
    """True the first time this key is seen (so one event never alerts twice)."""
    try:
        with transaction.atomic():
            TeamAlertSent.objects.create(key=key[:200])
        return True
    except IntegrityError:
        return False


def notify_team(sender, subject: str, lines: List[str], *, url: str = "", once_key: str = "") -> str:
    """Email the team about one event. Returns "" when sent (or alerts are off), else the problem as text."""
    try:
        to, cc = alert_recipients()
        user = _sender(sender)
        if not to or user is None:
            return ""
        if once_key and not _first_time(once_key):
            return ""
        body = "\n\n".join(filter(None, [
            "Hello,",
            *[line for line in lines if line],
            f"Open it in MagicWand: {url}" if url else "",
            "Sent automatically by MagicWand.",
        ]))
        GmailService(user).send_email(to, subject, body, cc=cc or None)
    except Exception as exc:  # Gmail sign-in or network problem: shown, never raised
        logger.warning("Team alert %r not sent: %s", subject, exc)
        return f"the team alert was not sent: {exc}"
    return ""


def enquiry_label(enquiry_id: str, enquiry: Optional[dict]) -> str:
    """'ENQ-7 - Acme' (reference and customer) for alert subjects."""
    name = str((enquiry or {}).get("customer_name") or "").strip()
    return f"{enquiry_id} - {name}" if name else str(enquiry_id)

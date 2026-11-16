"""Telling people in the team about marketing events: an enquiry was revived, a customer replied.

Notices never block the action that caused them: every problem is returned as text, never raised.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q

from dashboard.gmail_service import GmailAPIError, GmailService

logger = logging.getLogger(__name__)


def display_name(user) -> str:
    return (user.get_full_name() or user.username or user.email or "").strip()


def team_members() -> List:
    return list(get_user_model().objects.filter(is_active=True).order_by("first_name", "last_name", "username"))


def find_user(assigned_to: str):
    """The MagicWand user an enquiry's free-text salesperson refers to: username, email or full name."""
    value = (assigned_to or "").strip()
    if not value:
        return None
    users = get_user_model().objects.filter(is_active=True)
    match = users.filter(Q(username__iexact=value) | Q(email__iexact=value)).first()
    if match:
        return match
    parts = value.split()
    if len(parts) >= 2:
        match = users.filter(first_name__iexact=parts[0], last_name__iexact=" ".join(parts[1:])).first()
        if match:
            return match
    same_first = list(users.filter(first_name__iexact=value)[:2])
    return same_first[0] if len(same_first) == 1 else None


def _push(user, title: str, body: str, url: str) -> int:
    try:
        from push_notifications.services import send_push_to_users
        return send_push_to_users([user], title=title, body=body, url=url)
    except Exception as exc:  # push is optional (Firebase may not be set up)
        logger.info("Push to %s skipped: %s", user.pk, exc)
        return 0


def notify_revived(*, sender, salesperson, customer: str, old_id: str, new_id: str, note: str,
                   enquiry_url: str) -> tuple:
    """Phone notification and email to the salesperson.

    Returns (how they were told, e.g. ["phone", "email"]; problems to show).
    """
    if salesperson is None:
        return [], []
    done: List[str] = []
    title = f"Revived enquiry {new_id}"
    short = f"{customer or 'A lost customer'} is interested again (was {old_id})."
    if _push(salesperson, title, short, enquiry_url):
        done.append("phone")

    if not getattr(settings, "MARKETING_REVIVE_NOTIFY_EMAIL", True):
        return done, []
    address = (salesperson.email or "").strip()
    if not address:
        return done, [f"{display_name(salesperson)} has no email address in MagicWand"]
    if address.lower() == (getattr(sender, "email", "") or "").strip().lower():
        return done, []  # reviving for yourself: no need to email yourself
    problems: List[str] = []
    body = "\n\n".join(filter(None, [
        f"Hi {salesperson.first_name or display_name(salesperson)},",
        f"Marketing has revived a lost enquiry for {customer or 'a customer'}. "
        f"It is now new enquiry {new_id} (the old one was {old_id}) and is assigned to you.",
        f"Note from marketing: {note}" if note else "",
        f"Open the enquiry: {enquiry_url}",
        f"Sent by {display_name(sender)} from the MagicWand Marketing portal.",
    ]))
    from .campaigns import send_new_email  # avoid an import loop
    try:
        send_new_email(GmailService(sender), address, f"Revived enquiry {new_id}: {customer}".strip(), body)
        done.append("email")
    except GmailAPIError as exc:
        problems.append(f"could not email {display_name(salesperson)}: {exc}")
    except Exception as exc:
        logger.exception("Revive email to %s failed", address)
        problems.append(f"could not email {display_name(salesperson)}: {exc}")
    return done, problems


def notify_accepted(*, sender, salesperson, customer: str, enquiry_id: str, note: str, enquiry_url: str) -> tuple:
    """Phone notification and email to the salesperson: their lost enquiry is now Accepted.

    Returns (how they were told, e.g. ["phone", "email"]; problems to show).
    """
    if salesperson is None:
        return [], []
    done: List[str] = []
    if _push(salesperson, f"Enquiry {enquiry_id} accepted",
             f"{customer or 'The customer'} accepted the quote. Please continue with the order.", enquiry_url):
        done.append("phone")
    if not getattr(settings, "MARKETING_REVIVE_NOTIFY_EMAIL", True):
        return done, []
    address = (salesperson.email or "").strip()
    if not address:
        return done, [f"{display_name(salesperson)} has no email address in MagicWand"]
    if address.lower() == (getattr(sender, "email", "") or "").strip().lower():
        return done, []
    body = "\n\n".join(filter(None, [
        f"Hi {salesperson.first_name or display_name(salesperson)},",
        f"Good news: {customer or 'the customer'} has accepted enquiry {enquiry_id}, which was lost earlier. "
        "Marketing has set it to Accepted, so please continue with the order and payment.",
        f"Note from marketing: {note}" if note else "",
        f"Open the enquiry: {enquiry_url}",
        f"Sent by {display_name(sender)} from the MagicWand Marketing portal.",
    ]))
    from .campaigns import send_new_email  # avoid an import loop
    problems: List[str] = []
    try:
        send_new_email(GmailService(sender), address, f"Enquiry {enquiry_id} accepted: {customer}".strip(), body)
        done.append("email")
    except GmailAPIError as exc:
        problems.append(f"could not email {display_name(salesperson)}: {exc}")
    except Exception as exc:
        logger.exception("Accepted email to %s failed", address)
        problems.append(f"could not email {display_name(salesperson)}: {exc}")
    return done, problems


def alert_recipients() -> tuple:
    """(To, [CC]) of the team alerts (shared with the Enquiries portal)."""
    from enquiries import team_alerts
    return team_alerts.alert_recipients()


def notify_team(sender, subject: str, lines: List[str], *, url: str = "", once_key: str = "") -> str:
    """Email the team (To kumar, CC manju and apai by default) about one Marketing event. Never raises."""
    from enquiries import team_alerts
    return team_alerts.notify_team(sender, subject, lines, url=url, once_key=once_key)


def notify_reply(user, *, customer: str, campaign_name: str, url: str) -> None:
    if user is not None:
        _push(user, "Customer replied", f"{customer or 'A customer'} replied to \"{campaign_name}\".", url)

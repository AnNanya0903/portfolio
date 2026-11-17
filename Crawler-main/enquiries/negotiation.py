"""Negotiation and forwarding: who may forward, the department task queue, the status log and the timeline."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from django.contrib.auth import get_user_model
from django.db.models import Q, QuerySet
from django.utils import timezone

from kyc.departments import departments_of
from kyc.models import Department, UserDepartment
from magicwand.access import is_super_admin

from .models import EnquiryAssignment, EnquiryNegotiation, EnquiryStatusLog

logger = logging.getLogger(__name__)

MANAGER_DEPARTMENT = "Top Management"
OPEN_TASK_STATUSES = (EnquiryAssignment.STATUS_PENDING, EnquiryAssignment.STATUS_IN_PROGRESS)
STAGE_LABELS = dict(EnquiryNegotiation.STAGE_CHOICES)


def can_forward(user) -> bool:
    """Administrators and Top Management may forward an enquiry to a department."""
    if not getattr(user, "is_authenticated", False):
        return False
    return is_super_admin(user) or any(d.name == MANAGER_DEPARTMENT for d in departments_of(user))


def _sees_every_task(user) -> bool:
    return can_forward(user)


def department_members(department: Department) -> List[Any]:
    """Users in a department: those an admin placed there, plus those whose email is one of its contacts."""
    User = get_user_model()
    emails = [c.email for c in department.contacts.all()]
    by_email = Q()
    for email in emails:
        by_email |= Q(email__iexact=email)
    placed = Q(kyc_department__department=department)
    return list(User.objects.filter(placed | by_email).distinct().order_by("username"))


def tasks_for(user) -> QuerySet:
    """The task queue this user may see: their department's tasks (everything for admins and Top Management)."""
    qs = EnquiryAssignment.objects.select_related("to_department", "from_department", "assigned_by", "assigned_to")
    if _sees_every_task(user):
        return qs
    ids = [d.pk for d in departments_of(user)]
    return qs.filter(Q(to_department_id__in=ids) | Q(assigned_to=user))


def pending_task_count(user) -> int:
    """Open tasks for the badge in the menu. Never raises: a menu must not break a page."""
    try:
        if not getattr(user, "is_authenticated", False):
            return 0
        return tasks_for(user).filter(status__in=OPEN_TASK_STATUSES).count()
    except Exception:
        return 0


def can_update_assignment(user, assignment: EnquiryAssignment) -> bool:
    if can_forward(user) or assignment.assigned_by_id == getattr(user, "pk", None):
        return True
    if assignment.assigned_to_id and assignment.assigned_to_id == user.pk:
        return True
    return assignment.to_department_id in {d.pk for d in departments_of(user)}


def log_status_change(user, enquiry_id: str, old: str, new: str, note: str = "") -> None:
    """Record a status change. Never raises, so a failed log cannot stop the change itself."""
    old, new = str(old or "").strip(), str(new or "").strip()
    if old == new:
        return
    try:
        EnquiryStatusLog.objects.create(
            enquiry_id=str(enquiry_id), user=user if getattr(user, "pk", None) else None,
            username=getattr(user, "username", ""), old_status=old, new_status=new, note=note[:500])
    except Exception:
        logger.exception("Enquiry status change could not be logged")


def current_stage(enquiry_id: str) -> str:
    row = EnquiryNegotiation.objects.filter(enquiry_id=str(enquiry_id)).first()
    return row.stage if row else EnquiryNegotiation.STAGE_NONE


def set_stage(user, enquiry_id: str, stage: str, note: str = "") -> bool:
    """Set the negotiation stage; False when it was already that stage."""
    enquiry_id = str(enquiry_id)
    old = current_stage(enquiry_id)
    if stage == old:
        return False
    EnquiryNegotiation.objects.update_or_create(
        enquiry_id=enquiry_id, defaults={"stage": stage, "updated_by": user if user.pk else None})
    log_status_change(user, enquiry_id, STAGE_LABELS.get(old, old) or "No negotiation",
                      STAGE_LABELS.get(stage, stage) or "No negotiation", note)
    return True


def _who(user) -> str:
    if not user:
        return "someone"
    return (user.get_full_name() or user.username) if hasattr(user, "get_full_name") else str(user)


def timeline(enquiry_id: str) -> List[Dict[str, Any]]:
    """Status changes and forwarded tasks of one enquiry, newest first."""
    enquiry_id = str(enquiry_id)
    items: List[Dict[str, Any]] = []
    for log in EnquiryStatusLog.objects.filter(enquiry_id=enquiry_id).select_related("user"):
        text = f"Status changed from {log.old_status or '—'} to {log.new_status or '—'}"
        items.append({"at": log.created_at, "kind": "status", "icon": "bi-arrow-repeat", "by": log.username or _who(log.user),
                      "text": text, "detail": log.note})
    for a in EnquiryAssignment.objects.filter(enquiry_id=enquiry_id).select_related("to_department", "assigned_by", "assigned_to"):
        target = a.to_department.name + (f" ({_who(a.assigned_to)})" if a.assigned_to else "")
        due = f", due {a.due_date:%d %b %Y}" if a.due_date else ""
        items.append({"at": a.created_at, "kind": "assignment", "icon": "bi-send", "by": _who(a.assigned_by),
                      "text": f"Forwarded to {target} (priority {a.get_priority_display().lower()}{due})", "detail": a.comment})
        if a.completed_at:
            items.append({"at": a.completed_at, "kind": "assignment_done", "icon": "bi-check-circle",
                          "by": _who(a.assigned_to) if a.assigned_to else a.to_department.name,
                          "text": f"{a.to_department.name} completed the task", "detail": ""})
    items.sort(key=lambda item: item["at"], reverse=True)
    return items


def parse_due_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None

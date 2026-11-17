"""Pages for an enquiry's negotiation (stage, forwarding to a department, timeline) and the department task queue."""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from kyc.departments import departments_of
from kyc.models import Department

from . import negotiation, services
from .models import EnquiryAssignment, EnquiryNegotiation


def _load_enquiry(request: HttpRequest, enquiry_id: str):
    from . import views as enquiry_views  # the board's own helper knows the service address

    try:
        return services.fetch_enquiry(enquiry_views._get_api_root(), enquiry_id, request=request)
    except services.EnquiryServiceError as exc:
        messages.error(request, f"Unable to load enquiry: {exc}")
        return None


def negotiation_page(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    enquiry = _load_enquiry(request, enquiry_id)
    if enquiry is None:
        return redirect("enquiries:board")
    departments = list(Department.objects.filter(is_active=True).prefetch_related("contacts"))
    members = [{"id": u.pk, "name": u.get_full_name() or u.username, "department": d.pk}
               for d in departments for u in negotiation.department_members(d)]
    return render(request, "enquiries/negotiation.html", {
        "enquiry": enquiry,
        "enquiry_id": enquiry_id,
        "stage": negotiation.current_stage(enquiry_id),
        "stage_choices": EnquiryNegotiation.STAGE_CHOICES,
        "can_forward": negotiation.can_forward(request.user),
        "departments": departments,
        "members": members,
        "priorities": EnquiryAssignment.PRIORITY_CHOICES,
        "assignments": EnquiryAssignment.objects.filter(enquiry_id=enquiry_id).select_related("to_department", "assigned_to"),
        "timeline": negotiation.timeline(enquiry_id),
        "today": timezone.localdate(),
    })


@require_POST
def set_stage(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    stage = request.POST.get("stage", "")
    if stage not in dict(EnquiryNegotiation.STAGE_CHOICES):
        messages.error(request, "Choose one of the negotiation statuses.")
    elif negotiation.set_stage(request.user, enquiry_id, stage, (request.POST.get("note") or "").strip()):
        messages.success(request, "Status updated.")
    return redirect("enquiries:negotiation", enquiry_id=enquiry_id)


@require_POST
def forward(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    if not negotiation.can_forward(request.user):
        return HttpResponseForbidden("Only a manager or Top Management can forward an enquiry.")
    back = redirect("enquiries:negotiation", enquiry_id=enquiry_id)
    department = Department.objects.filter(pk=request.POST.get("department") or 0, is_active=True).first()
    comment = (request.POST.get("comment") or "").strip()
    if not department:
        messages.error(request, "Choose the department to forward to.")
        return back
    if not comment:
        messages.error(request, "A comment is required, so the department knows what to do.")
        return back
    try:
        due = negotiation.parse_due_date(request.POST.get("due_date") or "")
    except ValueError:
        messages.error(request, "The due date is not a valid date.")
        return back
    priority = request.POST.get("priority") or EnquiryAssignment.PRIORITY_MEDIUM
    if priority not in dict(EnquiryAssignment.PRIORITY_CHOICES):
        priority = EnquiryAssignment.PRIORITY_MEDIUM
    assignee = None
    if request.POST.get("assigned_to"):
        assignee = next((u for u in negotiation.department_members(department) if str(u.pk) == request.POST["assigned_to"]), None)
        if assignee is None:
            messages.error(request, f"That person is not in {department.name}.")
            return back
    enquiry = _load_enquiry(request, enquiry_id) or {}
    mine = departments_of(request.user)
    EnquiryAssignment.objects.create(
        enquiry_id=enquiry_id, customer_name=str(enquiry.get("customer_name") or "")[:255],
        from_department=mine[0] if mine else None, to_department=department, assigned_by=request.user,
        assigned_to=assignee, comment=comment, due_date=due, priority=priority)
    messages.success(request, f"Forwarded to {department.name}. It is in their Pending Tasks.")
    return back


def tasks(request: HttpRequest) -> HttpResponse:
    """The task queue: the user's department's forwarded enquiries; overdue ones first and highlighted."""
    queue = negotiation.tasks_for(request.user)
    status = request.GET.get("status") or "open"
    if status == "open":
        queue = queue.filter(status__in=negotiation.OPEN_TASK_STATUSES)
    elif status in dict(EnquiryAssignment.STATUS_CHOICES):
        queue = queue.filter(status=status)
    department = request.GET.get("department") or ""
    if department.isdigit():
        queue = queue.filter(to_department_id=int(department))
    rows = sorted(queue, key=lambda a: (a.status == a.STATUS_COMPLETED, not a.is_overdue, a.due_date is None, a.due_date or timezone.localdate()))
    return render(request, "enquiries/tasks.html", {
        "tasks": rows,
        "status": status,
        "department": department,
        "departments": Department.objects.filter(is_active=True),
        "statuses": EnquiryAssignment.STATUS_CHOICES,
        "my_departments": [d.name for d in departments_of(request.user)],
        "sees_all": negotiation.can_forward(request.user),
    })


@require_POST
def task_update(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(EnquiryAssignment, pk=pk)
    if not negotiation.can_update_assignment(request.user, task):
        return HttpResponseForbidden("This task belongs to another department.")
    new_status = request.POST.get("status")
    if new_status in dict(EnquiryAssignment.STATUS_CHOICES) and new_status != task.status:
        task.status = new_status
        task.completed_at = timezone.now() if new_status == task.STATUS_COMPLETED else None
        if new_status == task.STATUS_IN_PROGRESS and not task.assigned_to_id:
            task.assigned_to = request.user  # whoever starts it picks it up
    if request.POST.get("pick_up") and not task.assigned_to_id:
        task.assigned_to = request.user
    task.save()
    messages.success(request, "Task updated.")
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/") else "enquiries:tasks")

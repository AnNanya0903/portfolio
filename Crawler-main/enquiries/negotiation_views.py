"""Pages for an enquiry's negotiation (stage, forwarding to a department, timeline) and the department task queue."""
from __future__ import annotations

from django.contrib import messages
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from kyc.departments import departments_of
from kyc.models import Department

from quotations import links as quotation_links

from . import attachments as attachment_rules
from . import negotiation, services
from .models import EnquiryAssignment, EnquiryContact, EnquiryAttachment, EnquiryFollowup, EnquiryNegotiation


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
        "quotations": quotation_links.for_enquiry(enquiry_id),
        "assignments": EnquiryAssignment.objects.filter(enquiry_id=enquiry_id).select_related("to_department", "assigned_to"),
        "followups": EnquiryFollowup.objects.filter(enquiry_id=enquiry_id).select_related("user", "department"),
        "attachments": EnquiryAttachment.objects.filter(enquiry_id=enquiry_id),
        "followup_types": EnquiryFollowup.TYPE_CHOICES,
        "attachment_categories": EnquiryAttachment.CATEGORY_CHOICES,
        "open_tasks": [a for a in EnquiryAssignment.objects.filter(enquiry_id=enquiry_id).exclude(status="COMPLETED")],
        "timeline": negotiation.timeline(enquiry_id),
        "contact": EnquiryContact.objects.filter(enquiry_id=str(enquiry_id)).first(),
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
        return HttpResponseForbidden("Only the CRO, Rabin or APai can forward an enquiry.")
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
    upload = request.FILES.get("file")
    if upload:
        problem = attachment_rules.check_upload(upload)
        if problem:
            messages.error(request, problem)
            return back
    enquiry = _load_enquiry(request, enquiry_id) or {}
    mine = departments_of(request.user)
    task = EnquiryAssignment.objects.create(
        enquiry_id=enquiry_id, customer_name=str(enquiry.get("customer_name") or "")[:255],
        from_department=mine[0] if mine else None, to_department=department, assigned_by=request.user,
        assigned_to=assignee, comment=comment, due_date=due, priority=priority)
    if upload:
        _store_attachment(request, enquiry_id, upload, request.POST.get("category"), assignment=task)
    messages.success(request, f"Forwarded to {department.name}. It is in their Pending Tasks.")
    return back


def _store_attachment(request: HttpRequest, enquiry_id: str, upload, category: str, assignment=None, followup=None):
    if category not in dict(EnquiryAttachment.CATEGORY_CHOICES):
        category = EnquiryAttachment.CAT_OTHER
    return EnquiryAttachment.objects.create(
        enquiry_id=enquiry_id, assignment=assignment, followup=followup, file=upload,
        file_name=upload.name.rsplit("/", 1)[-1][:255], content_type=(upload.content_type or "")[:120],
        size_bytes=upload.size, category=category, uploaded_by=request.user)


@require_POST
def add_followup(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    back = redirect("enquiries:negotiation", enquiry_id=enquiry_id)
    note = (request.POST.get("note") or "").strip()
    kind = request.POST.get("followup_type") or EnquiryFollowup.TYPE_EMAIL
    if not note:
        messages.error(request, "Write what was done in the follow-up.")
        return back
    if kind not in dict(EnquiryFollowup.TYPE_CHOICES):
        kind = EnquiryFollowup.TYPE_OTHER
    try:
        nxt = negotiation.parse_due_date(request.POST.get("next_followup_date") or "")
    except ValueError:
        messages.error(request, "The next follow-up date is not a valid date.")
        return back
    upload = request.FILES.get("file")
    if upload:
        problem = attachment_rules.check_upload(upload)
        if problem:
            messages.error(request, problem)
            return back
    task = None
    if request.POST.get("assignment"):
        task = EnquiryAssignment.objects.filter(pk=request.POST["assignment"], enquiry_id=enquiry_id).first()
    mine = departments_of(request.user)
    department = task.to_department if task else (mine[0] if mine else None)
    followup = EnquiryFollowup.objects.create(
        enquiry_id=enquiry_id, assignment=task, department=department, user=request.user, note=note,
        followup_type=kind, next_followup_date=nxt)
    if upload:
        _store_attachment(request, enquiry_id, upload, request.POST.get("category"), followup=followup)
    # An earlier follow-up that was waiting for this one is no longer due.
    EnquiryFollowup.objects.filter(enquiry_id=enquiry_id, next_done=False).exclude(pk=followup.pk).update(next_done=True)
    messages.success(request, "Follow-up added.")
    return back


@require_POST
def followup_done(request: HttpRequest, pk: int) -> HttpResponse:
    followup = get_object_or_404(EnquiryFollowup, pk=pk)
    followup.next_done = True
    followup.save(update_fields=["next_done"])
    messages.success(request, "Marked as done.")
    return redirect(request.POST.get("next") if (request.POST.get("next") or "").startswith("/") else "enquiries:tasks")


@require_POST
def upload_attachment(request: HttpRequest, enquiry_id: str) -> HttpResponse:
    back = redirect("enquiries:negotiation", enquiry_id=enquiry_id)
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Choose a file to attach.")
        return back
    problem = attachment_rules.check_upload(upload)
    if problem:
        messages.error(request, problem)
        return back
    task = None
    if request.POST.get("assignment"):
        task = EnquiryAssignment.objects.filter(pk=request.POST["assignment"], enquiry_id=enquiry_id).first()
    _store_attachment(request, enquiry_id, upload, request.POST.get("category"), assignment=task)
    messages.success(request, f"{upload.name} attached.")
    return back


def download_attachment(request: HttpRequest, pk: int) -> HttpResponse:
    """The attached file, only for signed-in users with Enquiries access (never a public link)."""
    attachment = get_object_or_404(EnquiryAttachment, pk=pk)
    try:
        handle = attachment.file.open("rb")
    except Exception as exc:
        raise Http404() from exc
    response = FileResponse(handle, content_type=attachment.content_type or "application/octet-stream",
                            filename=attachment.file_name, as_attachment=True)
    response["X-Content-Type-Options"] = "nosniff"
    return response


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
        "due_followups": negotiation.due_followups(request.user),
        "today": timezone.localdate(),
        "status": status,
        "department": department,
        "departments": Department.objects.filter(is_active=True),
        "statuses": EnquiryAssignment.STATUS_CHOICES,
        "my_departments": [d.name for d in departments_of(request.user)],
        "sees_all": negotiation.sees_every_task(request.user),
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

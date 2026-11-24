"""The scheduled automations, run by Django itself (no outside tool).

Once an hour (App Engine cron, see cron.yaml) or by hand (python manage.py run_automations):
  1. start sequences   - quotations / PIs sent straight from Gmail (auto_follow_up.detect_gmail_quotes)
  2. Marketing leads   - lost enquiries without a lead or without Marketing's sequence get them (idempotent;
                         this is also the one-off catch-up for enquiries lost before this version)
  3. follow-ups        - every due email of both portals, at most FOLLOW_UP_SEND_CAP_PER_RUN per run (the rest
                         go on the next runs, so nothing goes out in a burst); the closing email is the 5th
  4. reply checks      - replies to Marketing campaign emails (marketing/campaigns.py)

Each step is independent: a problem in one is recorded and the others still run. FOLLOW_UP_AUTO_ENABLED and
FOLLOW_UP_AUTO_DRY_RUN decide whether anything is sent; a repeated run is harmless.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, Optional

from django.conf import settings
from django.utils import timezone

from . import auto_follow_up, services
from .models import AutomationRun

logger = logging.getLogger(__name__)

# One run must finish well inside App Engine's request time limit; the follow-up step stops sending when this
# many seconds have passed and leaves the rest for the next hourly run.
TIME_BUDGET_SECONDS = 8 * 60


def _flags() -> Dict[str, bool]:
    return {
        "enabled": bool(getattr(settings, "FOLLOW_UP_AUTO_ENABLED", False)),
        "dry_run": bool(getattr(settings, "FOLLOW_UP_AUTO_DRY_RUN", True)),
    }


def start_step(enquiries) -> Dict[str, Any]:
    """Start the sequence of quotations / PIs sent straight from Gmail."""
    result: Dict[str, Any] = {**_flags(), "started": []}
    if result["enabled"]:
        result["started"] = auto_follow_up.detect_gmail_quotes(enquiries)
    return result


def marketing_lead_step(enquiries) -> Dict[str, Any]:
    """Every lost enquiry has one Ready lead with its source, and Marketing's sequence (idempotent)."""
    from django.conf import settings as conf

    from marketing import lost
    from marketing.models import MarketingLead

    from .models import CustomerStatusEmail, EnquiryContact

    result: Dict[str, Any] = {**_flags(), "created": [], "started": []}
    if not result["enabled"]:
        return result
    by_id = {str(e.get("enquiry_id")): e for e in enquiries}
    leads = {lead.enquiry_id: lead for lead in MarketingLead.objects.filter(enquiry_id__in=list(by_id))}
    closed_no_response = set(EnquiryContact.objects.filter(close_reason=EnquiryContact.CLOSE_NO_RESPONSE)
                             .values_list("enquiry_id", flat=True))
    closing = dict(CustomerStatusEmail.objects.filter(kind=CustomerStatusEmail.KIND_CLOSING,
                                                      status=CustomerStatusEmail.STATUS_SENT)
                   .values_list("enquiry_id", "sent_at"))
    delay = timedelta(days=int(getattr(conf, "MARKETING_NO_RESPONSE_FIRST_DELAY_DAYS", 2) or 0))
    now = timezone.now()
    for enquiry_id, enquiry in by_id.items():
        status = str(enquiry.get("status") or "").strip().lower()
        if status == "rejected":
            source, first_due = lost.REJECTED, now
        elif status == "closed" and (enquiry_id in closed_no_response or enquiry_id in closing):
            source = lost.NO_RESPONSE
            first_due = max((closing.get(enquiry_id) or now) + delay, now)
        else:
            continue
        lead = leads.get(enquiry_id)
        had_start = auto_follow_up.FollowUpStart.objects.filter(enquiry_id=enquiry_id,
                                                                stage=auto_follow_up.MARKETING_STAGE).exists()
        if lead is not None and lead.lead_source and had_start:
            continue
        lost.create_lead(enquiry_id, source, customer_email=auto_follow_up.customer_email(enquiry_id, enquiry),
                         first_due=first_due)
        (result["created"] if lead is None else result["started"]).append(enquiry_id)
    return result


def follow_up_step(today: Optional[date] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    """Send the due emails, at most FOLLOW_UP_SEND_CAP_PER_RUN. Returns {"enabled", "dry_run", "due", "sent"}."""
    from django.conf import settings as conf

    result: Dict[str, Any] = {**_flags(), "due": [], "sent": []}
    if not result["enabled"]:
        return result
    try:
        due = auto_follow_up.due_follow_ups(today)
    except services.EnquiryServiceError as exc:
        result["error"] = f"could not load enquiries: {exc}"
        return result
    result["due"] = due
    cap = int(getattr(conf, "FOLLOW_UP_SEND_CAP_PER_RUN", 20) or 0)
    emailed = 0
    for item in due:
        if deadline is not None and time.monotonic() > deadline:
            result["stopped"] = "time budget used up; the rest follow on the next run"
            break
        if cap and emailed >= cap:
            result["stopped"] = f"{cap} emails this run (FOLLOW_UP_SEND_CAP_PER_RUN); the rest follow on the next runs"
            break
        enquiry_id = str(item["enquiry_id"])
        try:
            outcome = auto_follow_up.run_auto_follow_up(enquiry_id, today)
            result["sent"].append(outcome)
            if outcome.get("result") in (auto_follow_up.SENT, auto_follow_up.DRY_RUN, auto_follow_up.FAILED):
                emailed += 1
        except Exception as exc:  # one enquiry's problem must not stop the others
            logger.exception("Automatic follow-up failed for %s", enquiry_id)
            result["sent"].append({"enquiry_id": enquiry_id, "result": auto_follow_up.FAILED, "note": str(exc)[:300]})
    return result


def reply_check_step() -> Dict[str, Any]:
    """Look for customer replies to Marketing campaign emails."""
    from marketing import campaigns

    campaign_results = campaigns.check_all_replies()
    return {
        "campaigns": campaign_results,
        "replied": sum(r.get("replied", 0) for r in campaign_results),
        "failed": sum(1 for r in campaign_results if r.get("result") == "failed"),
    }


def _claim(trigger: str) -> Optional[AutomationRun]:
    """Start a run, unless another one is still going (then None)."""
    cutoff = timezone.now() - timedelta(minutes=AutomationRun.STALE_MINUTES)
    if AutomationRun.objects.filter(finished_at__isnull=True, started_at__gte=cutoff).exists():
        return None
    return AutomationRun.objects.create(trigger=trigger)


def run_all(trigger: str = "cron", today: Optional[date] = None) -> Dict[str, Any]:
    """Run every step once and record the run. Never raises."""
    run = _claim(trigger)
    if run is None:
        return {"skipped": "another run is still in progress"}
    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    enquiries: list = []

    def load() -> Dict[str, Any]:
        enquiries[:] = services.list_enquiries(auto_follow_up._api_root()) if _flags()["enabled"] else []
        return {"enquiries": len(enquiries)}

    steps: Dict[str, Callable[[], Dict[str, Any]]] = {
        "load": load,
        "start_sequences": lambda: start_step(enquiries),
        "marketing_leads": lambda: marketing_lead_step(enquiries),
        "follow_ups": lambda: follow_up_step(today, deadline),
        "reply_checks": reply_check_step,
    }
    summary: Dict[str, Any] = {}
    ok = True
    for name, step in steps.items():
        try:
            summary[name] = step()
            if summary[name].get("error"):
                ok = False
        except Exception as exc:
            logger.exception("Scheduled step %s failed", name)
            summary[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            ok = False
    run.finished_at = timezone.now()
    run.ok = ok
    run.summary = json.dumps(summary, default=str)[:60000]
    run.save(update_fields=["finished_at", "ok", "summary"])
    logger.info("Scheduled automations finished (%s): ok=%s", trigger, ok)
    return {"ok": ok, "run_id": run.pk, **summary}

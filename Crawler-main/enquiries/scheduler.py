"""The scheduled automations, run by Django itself (no outside tool).

Once an hour (App Engine cron, see cron.yaml) or by hand (python manage.py run_automations):
  1. automatic follow-ups   - every enquiry that is due, one email each (enquiries/auto_follow_up.py)
  2. customer emails        - acknowledgements and closing emails (enquiries/status_emails.py)
  3. reply checks           - replies to Marketing campaign emails (marketing/campaigns.py)

Each step is independent: a problem in one is recorded and the others still run. Nothing here changes
a rule: the switches FOLLOW_UP_AUTO_ENABLED and FOLLOW_UP_AUTO_DRY_RUN still decide whether anything is
sent, and the follow-up code itself sends at most one per enquiry per day, so a repeated run is harmless.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, Optional

from django.conf import settings
from django.utils import timezone

from . import auto_follow_up, services, status_emails
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


def follow_up_step(today: Optional[date] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    """Send every automatic follow-up that is due. Returns {"enabled", "dry_run", "due": [...], "sent": [...]}."""
    result: Dict[str, Any] = {**_flags(), "due": [], "sent": []}
    if not result["enabled"]:
        return result
    try:
        due = auto_follow_up.due_follow_ups(today)
    except services.EnquiryServiceError as exc:
        result["error"] = f"could not load enquiries: {exc}"
        return result
    result["due"] = due
    for item in due:
        if deadline is not None and time.monotonic() > deadline:
            result["stopped"] = "time budget used up; the rest follow on the next run"
            break
        enquiry_id = str(item["enquiry_id"])
        try:
            result["sent"].append(auto_follow_up.run_auto_follow_up(enquiry_id, today))
        except Exception as exc:  # one enquiry's problem must not stop the others
            logger.exception("Automatic follow-up failed for %s", enquiry_id)
            result["sent"].append({"enquiry_id": enquiry_id, "result": auto_follow_up.FAILED, "note": str(exc)[:300]})
    return result


def customer_email_step(today: Optional[date] = None) -> Dict[str, Any]:
    """Acknowledge new enquiries and send the closing email where the customer never replied."""
    result: Dict[str, Any] = {**_flags(), "acknowledgement": [], "closing": []}
    if not result["enabled"]:
        return result
    try:
        result.update(status_emails.run_scheduled(auto_follow_up._api_root(), today))
    except services.EnquiryServiceError as exc:
        result["error"] = f"could not load enquiries: {exc}"
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
    steps: Dict[str, Callable[[], Dict[str, Any]]] = {
        "follow_ups": lambda: follow_up_step(today, deadline),
        "customer_emails": lambda: customer_email_step(today),
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

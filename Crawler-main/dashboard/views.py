"""
Dashboard views powering the Gmail workspace experience.
Handles authentication, keyword-driven ingestion, and Kanban presentation.
"""

import base64
import json
import logging
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta
from collections import Counter
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, Http404, StreamingHttpResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils.dateparse import parse_date
from django.urls import reverse, NoReverseMatch
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model, logout
import requests
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import Count
from django.utils import timezone

from magicwand.views import resolve_frontend_base

from .gmail_service import GmailService, GmailAPIError
from .priority_engine import PriorityEngine
from .automation import AutomationService
from .forms import ComposeReplyForm
from .follow_up_templates import (
    DEFAULT_STAGE as DEFAULT_FOLLOW_UP_STAGE,
    PAYMENT_STAGE,
    STAGE_CHOICES as FOLLOW_UP_STAGE_CHOICES,
    STAGE_KEYS as FOLLOW_UP_STAGE_KEYS,
    all_stage_bodies,
    build_follow_up_body,
)
from .ai_service import AIReplyService, AIReplyRequest, AIServiceError
from . import realtime
from .models import (
    UserProfile,
    EmailResult,
    CrawlJob,
    UserKeywords,
    KeywordPriorityMapping,
)

from enquiries import payment_terms
from enquiries.models import EnquiryContact, FollowUpSent


def _latest_email_record(user, email_id: str):
    return (
        EmailResult.objects.filter(user=user, email_id=email_id)
        .order_by('-updated_at')
        .first()
    )


def _build_transient_email_record(user, email_id: str, message: dict):
    return SimpleNamespace(
        user=user,
        email_id=email_id,
        sender=message.get('sender') or '',
        subject=message.get('subject') or '',
        snippet=message.get('snippet') or '',
        date=message.get('date') or '',
        parsed_date=message.get('parsed_date'),
        thread_id=message.get('thread_id') or '',
        message_id_header=message.get('message_id_header') or '',
        thread_participants='Multiple',
    )


def _resolve_email_context(user, email_id: str, gmail_service: GmailService):
    email_record = _latest_email_record(user, email_id)
    lookup_id = email_record.email_id if email_record else email_id
    try:
        message = gmail_service.get_message(lookup_id)
    except GmailAPIError:
        if email_record:
            raise
        raise Http404("Email not found")

    if not email_record:
        email_record = _build_transient_email_record(user, email_id, message)

    return email_record, message


def _build_keywords_context(user):
    try:
        user_keywords = UserKeywords.objects.get(user=user)
        existing_keywords_text = user_keywords.keywords_text
    except UserKeywords.DoesNotExist:
        existing_keywords_text = ''

    existing_mappings = {}
    try:
        mappings = KeywordPriorityMapping.objects.filter(user=user)
        for mapping in mappings:
            existing_mappings[mapping.keyword] = mapping.priority
    except Exception:
        pass

    return {
        'existing_keywords_text': existing_keywords_text,
        'existing_mappings': existing_mappings,
        'existing_mappings_json': json.dumps(existing_mappings),
    }


def _should_force_logout(message: str) -> bool:
    if not message:
        return False
    lowered = message.lower()
    keywords = [
        'credential',
        'token',
        'authenticate',
        'authentication',
        're-auth',
        'auth ',
    ]
    return any(key in lowered for key in keywords)


def _priority_from_gmail_labels(labels):
    """Map Gmail label metadata to board priority buckets."""
    if not labels:
        return None

    label_set = {label.upper() for label in labels}

    if label_set & {'IMPORTANT', 'STARRED'}:
        return 'high'

    if label_set & {
        'CATEGORY_PROMOTIONS',
        'CATEGORY_SOCIAL',
        'CATEGORY_FORUMS',
    }:
        return 'low'

    if label_set & {
        'CATEGORY_UPDATES',
        'CATEGORY_PERSONAL',
        'CATEGORY_PRIMARY',
    }:
        return 'medium'

    return None


def _is_local_environment():
    deploy_env = (getattr(settings, "DEPLOY_ENV", "") or "").lower()
    if deploy_env in {"local", "dev", "development"}:
        return True
    return bool(getattr(settings, "DEBUG", False))


def _demo_datetime(year, month, day, hour, minute):
    naive = datetime(year, month, day, hour, minute)
    return timezone.make_aware(naive, timezone.utc)


def _build_demo_email(*, email_id, sender, subject, snippet, received_at, priority, labels=None, matched_keywords=None, thread_id=None):
    dt = received_at
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.utc)
    label_list = list(labels or ["INBOX"])
    keyword_list = list(matched_keywords or [])
    return {
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "snippet": snippet,
        "date": dt.strftime("%a, %d %b %Y %H:%M:%S %z"),
        "parsed_date": dt,
        "thread_id": thread_id or f"{email_id}-thread",
        "message_id_header": f"<{email_id}@magicwand.local>",
        "labels": label_list,
        "priority": priority,
        "matched_keywords": keyword_list,
    }


_LOCAL_GMAIL_DEMO_EMAILS = [
    _build_demo_email(
        email_id="demo-high-001",
        sender="kr.piyush0509@gmail.com",
        subject="test",
        snippet="Hi",
        received_at=_demo_datetime(2025, 10, 16, 12, 4),
        priority="high",
        labels=["INBOX", "IMPORTANT"],
        matched_keywords=["customer", "urgent"],
    ),
    _build_demo_email(
        email_id="demo-medium-001",
        sender="piyushdude56@gmail.com",
        subject="Fwd: CAD files from similar past inquiries & list our Avigiri Gaskets",
        snippet="Thanks and Regards Piyush Kumar +91-8546843038 ----------- Forwarded message ----------- From: Piyush Kumar...",
        received_at=_demo_datetime(2025, 10, 23, 22, 18),
        priority="medium",
        labels=["INBOX", "CATEGORY_UPDATES"],
        matched_keywords=["cad", "gasket"],
    ),
    _build_demo_email(
        email_id="demo-medium-002",
        sender="googleaistudio-noreply@google.com",
        subject="Just shipped: New API key page, playground revamp, and more!",
        snippet="Because the best developer experiences don't take weekends off. Explore what's new in Google AI Studio this week.",
        received_at=_demo_datetime(2025, 10, 18, 18, 30),
        priority="medium",
        labels=["INBOX", "CATEGORY_UPDATES"],
        matched_keywords=["ai", "update"],
    ),
    _build_demo_email(
        email_id="demo-medium-003",
        sender="support@magicwand.biz",
        subject="New API key page, playground revamp, and more!",
        snippet="Team update: new playground revamp rolled out across staging. Let us know if you spot regressions.",
        received_at=_demo_datetime(2025, 10, 20, 9, 45),
        priority="medium",
        labels=["INBOX", "CATEGORY_PERSONAL"],
        matched_keywords=["playground", "update"],
    ),
]


def _ensure_local_demo_emails(user):
    """Seed demo Gmail data for local development if the inbox is empty."""
    if not user or not getattr(user, "is_authenticated", False):
        return
    if not _is_local_environment():
        return
    if os.getenv("ENABLE_LOCAL_GMAIL_DEMO", "1").lower() not in {"1", "true", "yes"}:
        return
    if EmailResult.objects.filter(user=user).exists():
        return

    user_profile, _ = UserProfile.objects.get_or_create(user=user)
    if not user_profile.gmail_address:
        user_profile.gmail_address = f"{user.username or 'demo'}@example.com"
        user_profile.save(update_fields=["gmail_address", "updated_at"])

    with transaction.atomic():
        for index, sample in enumerate(_LOCAL_GMAIL_DEMO_EMAILS):
            EmailResult.objects.get_or_create(
                user=user,
                email_id=sample["email_id"],
                defaults={
                    "sender": sample["sender"],
                    "subject": sample["subject"],
                    "snippet": sample["snippet"],
                    "date": sample["date"],
                    "parsed_date": sample["parsed_date"],
                    "thread_id": sample["thread_id"],
                    "message_id_header": sample["message_id_header"],
                    "labels": sample["labels"],
                    "priority": sample["priority"],
                    "keyword_matches": len(sample["matched_keywords"]),
                    "matched_keywords": sample["matched_keywords"],
                    "board_column": sample["priority"],
                    "board_position": index,
                    "is_done": False,
                    "created_at": sample["parsed_date"],
                    "updated_at": sample["parsed_date"],
                },
            )


def _encode_attachment_metadata(metadata):
    packed = json.dumps(metadata).encode('utf-8')
    return base64.urlsafe_b64encode(packed).decode('utf-8')


def _decode_attachment_metadata(token):
    try:
        data = base64.urlsafe_b64decode(token.encode('utf-8'))
        return json.loads(data.decode('utf-8'))
    except Exception:
        return None


def _build_thread_summary(messages):
    """Create a compact text summary used for AI prompt generation."""
    summary_lines = []
    for msg in messages:
        sender = msg.get('sender') or 'Unknown sender'
        to_field = msg.get('to') or 'Unknown recipient'
        date_value = msg.get('parsed_date')
        if isinstance(date_value, datetime):
            date_text = date_value.strftime('%Y-%m-%d %H:%M')
        else:
            date_text = msg.get('date') or 'Unknown date'
        body_text = (msg.get('body_text') or '').strip()
        body_preview = body_text[:1200]
        summary_lines.append(
            f"From: {sender}\nTo: {to_field}\nDate: {date_text}\n---\n{body_preview}"
        )
    return '\n\n'.join(summary_lines)


def _serialize_email(email):
    return {
        'email_id': email.email_id,
        'sender': email.sender,
        'subject': email.subject,
        'snippet': email.snippet,
        'date': email.date,
        'thread_id': email.thread_id or '',
        'message_id_header': email.message_id_header or '',
        'matched_keywords': email.matched_keywords or [],
        'priority': email.priority,
    }


def _process_history_update(user_profile, history_id):
    user = user_profile.user
    gmail_service = GmailService(user)
    try:
        gmail_service.ensure_watch(user_profile)
    except GmailAPIError as exc:
        logger.warning("Unable to ensure Gmail watch for %s: %s", user.username, exc)

    start_history_id = user_profile.gmail_history_id or history_id
    if not start_history_id:
        user_profile.gmail_history_id = history_id
        user_profile.save(update_fields=['gmail_history_id', 'updated_at'])
        return 0

    try:
        history_messages, latest_id = gmail_service.fetch_history(start_history_id)
    except GmailAPIError as exc:
        logger.error("History processing failed for %s: %s", user.username, exc)
        return 0

    if not history_messages:
        if latest_id and latest_id != user_profile.gmail_history_id:
            user_profile.gmail_history_id = latest_id
            user_profile.save(update_fields=['gmail_history_id', 'updated_at'])
        return 0

    message_ids = {msg.get('id') for msg in history_messages if msg.get('id')}

    keywords = []
    try:
        user_keywords = UserKeywords.objects.get(user=user)
        keywords = user_keywords.get_keywords_list()
    except UserKeywords.DoesNotExist:
        pass

    priority_engine = PriorityEngine(user)
    automation_service = AutomationService(user)

    collected = []
    for message_id in message_ids:
        try:
            message = gmail_service.get_message(message_id)
        except GmailAPIError:
            continue
        if not message:
            continue

        message['matched_keywords'] = gmail_service._analyze_keyword_matches(  # pylint: disable=protected-access
            message,
            keywords,
        )
        collected.append(message)

    if not collected:
        if latest_id and latest_id != user_profile.gmail_history_id:
            user_profile.gmail_history_id = latest_id
            user_profile.save(update_fields=['gmail_history_id', 'updated_at'])
        return 0

    saved = _persist_email_batch(user, collected, keywords, priority_engine, automation_service)

    if latest_id and latest_id != user_profile.gmail_history_id:
        user_profile.gmail_history_id = latest_id
        user_profile.save(update_fields=['gmail_history_id', 'updated_at'])

    return saved


def _schedule_async_gmail_refresh(user_profile_id: int) -> None:
    """Refresh Gmail metadata in the background so HTTP requests stay fast."""

    def worker() -> None:
        close_old_connections()
        try:
            profile = (
                UserProfile.objects.select_related('user')
                .filter(pk=user_profile_id)
                .first()
            )
            if not profile or not profile.user_id:
                return

            gmail_service = GmailService(profile.user)

            profile_info = None
            try:
                profile_info = gmail_service.get_user_profile()
            except GmailAPIError as exc:
                logger.debug(
                    "Async Gmail profile fetch skipped for %s: %s",
                    profile.user.username,
                    exc,
                )
            except Exception as exc:
                logger.debug(
                    "Unexpected async Gmail profile error for %s: %s",
                    profile.user.username,
                    exc,
                )

            if isinstance(profile_info, dict):
                try:
                    raw_history = profile_info.get('history_id')
                    if isinstance(raw_history, (str, int)) and not profile.gmail_history_id:
                        profile.gmail_history_id = str(raw_history)
                        profile.save(update_fields=['gmail_history_id', 'updated_at'])
                except Exception as exc:
                    logger.debug(
                        "Async Gmail history update skipped for %s: %s",
                        profile.user.username,
                        exc,
                    )

            try:
                gmail_service.ensure_watch(profile)
            except GmailAPIError as exc:
                logger.debug(
                    "Async Gmail watch not registered for %s: %s",
                    profile.user.username,
                    exc,
                )
            except Exception as exc:
                logger.debug(
                    "Unexpected async Gmail watch error for %s: %s",
                    profile.user.username,
                    exc,
                )
        finally:
            close_old_connections()

    try:
        threading.Thread(
            target=worker,
            name=f"gmail-refresh-{user_profile_id}",
            daemon=True,
        ).start()
    except Exception as exc:
        logger.debug(
            "Failed to schedule async Gmail refresh for profile %s: %s",
            user_profile_id,
            exc,
        )


def _persist_email_batch(user, email_batch, keywords, priority_engine, automation_service):
    saved_count = 0
    for email_data in email_batch:
        try:
            email_result = EmailResult(
                user=user,
                email_id=email_data['id'],
                sender=email_data['sender'],
                subject=email_data['subject'],
                snippet=email_data['snippet'],
                date=email_data['date'],
                parsed_date=email_data['parsed_date'],
                thread_id=email_data.get('thread_id'),
                labels=email_data.get('labels', []),
                message_id_header=email_data.get('message_id_header'),
            )

            gmail_priority = _priority_from_gmail_labels(email_result.labels)
            if gmail_priority:
                email_result.priority = gmail_priority
                email_result.board_column = gmail_priority

            email_result.assign_priority_by_keywords(
                keywords,
                email_body=email_data.get('body_text', ''),
                matched_keywords=email_data.get('matched_keywords'),
                base_priority=gmail_priority or email_result.priority,
            )

            try:
                recommendation = priority_engine.recommend(email_result.matched_keywords)
                if recommendation.priority and recommendation.confidence >= 0.35:
                    email_result.priority = recommendation.priority
                    email_result.board_column = recommendation.priority
            except Exception as learning_exc:
                logger.debug("Priority engine skipped: %s", learning_exc)

            email_result.save()
            try:
                realtime.publish_event(
                    user.id,
                    {
                        'type': 'email_added',
                        'priority': email_result.priority,
                        'email': _serialize_email(email_result),
                    },
                )
            except Exception:
                pass
            saved_count += 1

            try:
                automation_service.evaluate(email_result, email_data)
            except Exception as automation_exc:
                logger.error(
                    "Automation evaluation failed for user %s email %s: %s",
                    user.username,
                    email_result.email_id,
                    automation_exc,
                )

        except Exception as exc:
            logger.warning("Failed to persist email %s: %s", email_data.get('id'), exc)
            continue

    return saved_count


def _start_background_crawl(job, user_id, keywords, include_read, primary_only, max_results, next_token, fetched_count):
    job.status = 'running'
    job.save(update_fields=['status', 'updated_at'])

    thread = threading.Thread(
        target=_continue_crawl,
        args=(
            job.job_id,
            user_id,
            keywords,
            include_read,
            primary_only,
            max_results,
            next_token,
            fetched_count,
        ),
        daemon=True,
    )
    thread.start()


def _run_initial_email_sync(request, profile):
    """Fetch unread inbox emails for first-time users."""
    user = request.user
    gmail_service = GmailService(user)

    if not gmail_service.authenticate():
        raise GmailAPIError("Authentication required. Please reconnect your Google account.")

    priority_engine = PriorityEngine(user)
    automation_service = AutomationService(user)

    include_read = False
    primary_only = settings.GMAIL_ONLY_PRIMARY_DEFAULT
    max_results = settings.DASHBOARD_MAX_EMAIL_RESULTS
    initial_batch = min(settings.DASHBOARD_INITIAL_BATCH, max_results)

    try:
        unread_estimate = gmail_service.get_unread_count()
    except GmailAPIError as exc:
        logger.debug("Unread count unavailable during initial sync for %s: %s", user.username, exc)
        unread_estimate = None

    emails, next_token, estimate = gmail_service.fetch_email_batch(
        keywords=[],
        include_read=include_read,
        primary_only=primary_only,
        limit=initial_batch,
    )

    saved_count = _persist_email_batch(user, emails, [], priority_engine, automation_service)
    logger.info("Initial sync saved %s emails for user %s", saved_count, user.username)

    job_created = False
    if next_token and saved_count < max_results:
        remaining = max_results - saved_count
        if remaining > 0:
            job = CrawlJob.objects.create(
                user=user,
                status='queued',
                fetched_count=saved_count,
                total_estimate=unread_estimate or estimate or 0,
                max_results=max_results,
                params={
                    'keywords': [],
                    'include_read': include_read,
                    'primary_only': primary_only,
                },
                last_page_token=next_token,
            )

            _start_background_crawl(
                job,
                user.id,
                [],
                include_read,
                primary_only,
                max_results,
                next_token,
                saved_count,
            )

            request.session['active_crawl_job_id'] = str(job.job_id)
            request.session.modified = True
            job_created = True
            logger.info(
                "Queued background crawl job %s for user %s during initial sync",
                job.job_id,
                user.username,
            )

    total_found = unread_estimate or estimate or saved_count

    try:
        profile_info = gmail_service.get_user_profile()
        if profile_info and profile_info.get('history_id'):
            profile.gmail_history_id = profile_info['history_id']
            profile.save(update_fields=['gmail_history_id', 'updated_at'])
    except Exception:
        pass

    try:
        gmail_service.ensure_watch(profile)
    except GmailAPIError as exc:
        logger.warning("Unable to register Gmail watch for user %s: %s", user.username, exc)

    return saved_count, total_found, job_created


def _continue_crawl(job_uuid, user_id, keywords, include_read, primary_only, max_results, page_token, fetched_count):
    from django.contrib.auth import get_user_model

    try:
        job = CrawlJob.objects.get(job_id=job_uuid)
    except CrawlJob.DoesNotExist:
        return

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        job.mark('failed', 'User no longer exists')
        return

    if job.status == 'cancelled':
        return

    gmail_service = GmailService(user)
    priority_engine = PriorityEngine(user)
    automation_service = AutomationService(user)

    remaining = max(max_results - fetched_count, 0)
    token = page_token

    try:
        job.status = 'running'
        job.save(update_fields=['status', 'updated_at'])

        while remaining > 0 and token:
            job.refresh_from_db(fields=['status'])
            if job.status == 'cancelled':
                return

            batch_limit = min(remaining, 50)
            emails, token, estimate = gmail_service.fetch_email_batch(
                keywords=keywords,
                include_read=include_read,
                primary_only=primary_only,
                limit=batch_limit,
                page_token=token,
            )

            if estimate and not job.total_estimate:
                job.total_estimate = estimate

            if not emails:
                break

            saved = _persist_email_batch(user, emails, keywords, priority_engine, automation_service)
            fetched_count += saved
            remaining = max(max_results - fetched_count, 0)

            job.fetched_count = fetched_count
            job.last_page_token = token
            job.save(update_fields=['fetched_count', 'last_page_token', 'total_estimate', 'updated_at'])

        if remaining <= 0 or not token:
            job.mark('completed')
        else:
            job.mark('failed', 'Job stopped unexpectedly')

    except Exception as exc:
        logger.exception("Background crawl failed for user %s", user.username)
        job.mark('failed', str(exc))
logger = logging.getLogger(__name__)


def _get_google_reauth_url() -> str:
    """Return the Google OAuth login URL, with a safe fallback."""
    try:
        return reverse('socialaccount_login', args=('google',))
    except NoReverseMatch:
        logger.warning("socialaccount_login URL reverse failed; using fallback path")
        return '/accounts/google/login/?process=login'


def home(request):
    """Home page - redirects authenticated users to frontend, shows login otherwise."""
    if request.user.is_authenticated:
        frontend_base = resolve_frontend_base(request)
        return redirect(f'{frontend_base}/')
    return render(request, 'dashboard/home.html')


@login_required
def initial_sync(request):
    """Trigger initial unread inbox sync for first-time users."""
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if user_profile.initial_sync_completed:
        return redirect('dashboard:kanban_dashboard')

    try:
        saved_count, total_found, job_created = _run_initial_email_sync(request, user_profile)
    except GmailAPIError as exc:
        logger.error("Initial sync Gmail error for user %s: %s", request.user.username, exc)
        messages.error(request, str(exc))
        reauth_url = _get_google_reauth_url()
        messages.info(request, f"Please reconnect Google: {reauth_url}")
        return redirect('dashboard:dashboard')
    except Exception as exc:
        logger.exception("Initial sync failed for user %s", request.user.username)
        messages.error(request, "We couldn't import your inbox right now. Please try again.")
        return redirect('dashboard:dashboard')

    user_profile.initial_sync_completed = True
    user_profile.save(update_fields=['initial_sync_completed', 'updated_at'])

    if saved_count > 0:
        if job_created:
            messages.success(
                request,
                f"Imported {saved_count} unread emails. We'll keep fetching the rest in the background.",
            )
        else:
            messages.success(
                request,
                f"Imported {saved_count} unread emails from your inbox.",
            )
    else:
        if total_found > 0 and job_created:
            messages.info(
                request,
                "Fetching unread emails in the background. They will appear here shortly.",
            )
        else:
            messages.info(
                request,
                "No unread inbox emails found. We'll show new ones as they arrive.",
            )

    return redirect('dashboard:kanban_dashboard')


@login_required
def dashboard(request):
    """Legacy dashboard view showing account info and connection status."""
    try:
        reauth_url = _get_google_reauth_url()
        # Get or create user profile
        user_profile, created = UserProfile.objects.get_or_create(user=request.user)
        
        # Get user's Gmail address from social account
        gmail_address = None
        try:
            social_account = SocialAccount.objects.get(
                user=request.user,
                provider='google'
            )
            gmail_address = social_account.extra_data.get('email')
            
            # Update profile if needed
            if gmail_address and user_profile.gmail_address != gmail_address:
                user_profile.gmail_address = gmail_address
                user_profile.save()
                
        except SocialAccount.DoesNotExist:
            logger.warning(f"No Google social account found for user {request.user.username}")
        
        # Initialize Gmail service to check connection
        gmail_service = GmailService(request.user)
        connection_status = False
        gmail_profile = None
        
        try:
            # Check if user has Google social account
            social_account = SocialAccount.objects.filter(
                user=request.user,
                provider='google'
            ).first()
            
            if social_account:
                try:
                    connection_status = gmail_service.authenticate()
                    if connection_status:
                        gmail_profile = gmail_service.get_user_profile()
                    else:
                        logger.warning(f"Gmail authentication returned False for user {request.user.username}")
                except GmailAPIError as auth_error:
                    logger.warning(
                        "Gmail authentication requires reauthorization for user %s: %s",
                        request.user.username,
                        auth_error,
                    )
                    messages.warning(request, str(auth_error))
                    connection_status = False
            else:
                logger.info(f"No Google social account found for user {request.user.username}")
                
        except Exception as e:
            logger.error(f"Gmail connection check failed for user {request.user.username}: {str(e)}")
            connection_status = False
        
        context = {
            'user_profile': user_profile,
            'gmail_address': gmail_address,
            'connection_status': connection_status,
            'gmail_profile': gmail_profile,
            'reauth_url': reauth_url,
        }
        
        return render(request, 'dashboard/dashboard.html', context)
        
    except Exception as e:
        logger.error(f"Dashboard error for user {request.user.username}: {str(e)}")
        messages.error(request, "An error occurred while loading the dashboard.")
        return render(request, 'dashboard/dashboard.html', {'reauth_url': _get_google_reauth_url()})


@login_required
def connection_status(request):
    """AJAX endpoint to check Gmail connection status."""
    reauth_url = _get_google_reauth_url()
    try:
        gmail_service = GmailService(request.user)
        is_connected = gmail_service.authenticate()

        gmail_profile = None
        if is_connected:
            gmail_profile = gmail_service.get_user_profile()

        return JsonResponse({
            'connected': is_connected,
            'profile': gmail_profile,
            'error': None,
            'needs_reauth': not is_connected,
            'reauth_url': reauth_url,
            'redirect': None,
        })

    except GmailAPIError as e:
        logger.error(
            "Connection status Gmail API error for user %s: %s",
            request.user.username,
            e,
        )
        error_message = str(e)
        needs_reauth = 'auth' in error_message.lower()
        redirect_url = None
        if _should_force_logout(error_message):
            logout(request)
            request.session.flush()
            redirect_url = reverse('account_login')

        return JsonResponse({
            'connected': False,
            'profile': None,
            'error': error_message,
            'needs_reauth': needs_reauth,
            'reauth_url': reauth_url,
            'redirect': redirect_url,
        })
    except Exception as e:
        logger.error(f"Connection status error for user {request.user.username}: {str(e)}")
        return JsonResponse({
            'connected': False,
            'profile': None,
            'error': str(e),
            'needs_reauth': True,
            'reauth_url': reauth_url,
            'redirect': None,
        })

# New Keyword-based Flow Views

@login_required
def keywords_entry(request):
    """Keywords entry page - first step in the new flow."""
    
    if request.method == 'POST':
        # Handle cancellation
        if request.POST.get('cancel'):
            messages.info(request, "Search cancelled.")
            return redirect('dashboard:dashboard')
        
        # Get keywords from form (comma-separated)
        keywords_text = request.POST.get('keywords', '').strip()
        
        if not keywords_text:
            messages.error(request, "Keywords are required.")
            return render(request, 'dashboard/keywords.html', _build_keywords_context(request.user))
        
        # Split by comma and clean up
        keywords = [kw.strip() for kw in keywords_text.split(',') if kw.strip()]
        
        # Validate keywords
        if not keywords:
            messages.error(request, "Please enter at least one keyword.")
            return render(request, 'dashboard/keywords.html', _build_keywords_context(request.user))
        
        # Check for duplicates (case insensitive)
        unique_keywords = []
        seen_lower = set()
        for kw in keywords:
            if kw.lower() not in seen_lower:
                unique_keywords.append(kw)
                seen_lower.add(kw.lower())
        
        keywords = unique_keywords
        
        try:
            # Save keywords to database
            user_keywords, created = UserKeywords.objects.get_or_create(user=request.user)
            user_keywords.set_keywords_from_list(keywords)
            user_keywords.save()
            
            # Save keyword-priority mappings

            # Clear existing mappings for this user
            KeywordPriorityMapping.objects.filter(user=request.user).delete()

            # Normalize posted priority map by scanning form keys with prefix 'priority_'
            posted_priorities = {}
            for key, value in request.POST.items():
                if key.startswith('priority_'):
                    raw_kw = key[len('priority_'):]
                    norm_kw = ' '.join(raw_kw.strip().lower().split())
                    posted_priorities[norm_kw] = value.strip().lower()

            # Save new mappings (only for keywords with assigned priorities)
            valid_values = {'high', 'medium', 'low'}
            for keyword in keywords:
                norm_kw = ' '.join(keyword.strip().lower().split())
                priority = posted_priorities.get(norm_kw, '')
                if priority in valid_values:
                    KeywordPriorityMapping.objects.create(
                        user=request.user,
                        keyword=norm_kw,
                        priority=priority
                    )
            
            logger.info(f"Keywords and priorities saved for user {request.user.username}: {keywords}")
            
            # Redirect to loading page with keywords in session
            request.session['search_keywords'] = keywords
            return redirect('dashboard:loading_page')
            
        except Exception as e:
            logger.error(f"Error saving keywords for user {request.user.username}: {str(e)}")
            messages.error(request, "Error saving keywords. Please try again.")
            return render(request, 'dashboard/keywords.html', _build_keywords_context(request.user))
    
    # GET request - show the form
    # Pre-populate with existing keywords if available
    context = _build_keywords_context(request.user)
    return render(request, 'dashboard/keywords.html', context)


@login_required
def loading_page(request):
    """Loading page - shows progress while crawling emails."""
    keywords = request.session.get('search_keywords')
    
    if not keywords:
        messages.error(request, "No search keywords found. Please start over.")
        return redirect('dashboard:keywords_entry')
    
    context = {
        'keywords': keywords,
        'max_results': settings.DASHBOARD_MAX_EMAIL_RESULTS,
    }
    
    return render(request, 'dashboard/loading.html', context)


@login_required
@require_http_methods(["POST"])
def crawl_emails_api(request):
    """API endpoint to crawl emails based on keywords."""
    import json
    
    try:
        # Get keywords from request
        data = json.loads(request.body)
        keywords = data.get('keywords', [])
        
        if not keywords:
            return JsonResponse({
                'success': False,
                'error': 'Keywords are required'
            })

        CrawlJob.objects.filter(
            user=request.user,
            status__in=['queued', 'running'],
        ).update(status='cancelled', message='Superseded by new crawl')

        request.session.pop('active_crawl_job_id', None)

        max_results = data.get('max_results', settings.DASHBOARD_MAX_EMAIL_RESULTS)
        include_read = data.get('include_read', settings.GMAIL_INCLUDE_READ_DEFAULT)

        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = settings.DASHBOARD_MAX_EMAIL_RESULTS

        if max_results <= 0:
            max_results = settings.DASHBOARD_MAX_EMAIL_RESULTS

        gmail_service = GmailService(request.user)
        priority_engine = PriorityEngine(request.user)
        automation_service = AutomationService(request.user)

        unread_estimate = None
        try:
            unread_estimate = gmail_service.get_unread_count()
        except GmailAPIError as exc:
            logger.debug("Unread count unavailable for user %s: %s", request.user.username, exc)

        initial_limit = min(settings.DASHBOARD_INITIAL_BATCH, max_results)
        initial_emails, next_token, estimate = gmail_service.fetch_email_batch(
            keywords=keywords,
            include_read=include_read,
            primary_only=data.get('primary_only', settings.GMAIL_ONLY_PRIMARY_DEFAULT),
            limit=initial_limit,
        )

        EmailResult.objects.filter(user=request.user).delete()

        saved_count = _persist_email_batch(
            request.user,
            initial_emails,
            keywords,
            priority_engine,
            automation_service,
        )

        job_payload = None

        if next_token and saved_count < max_results:
            remaining = max_results - saved_count
            job = CrawlJob.objects.create(
                user=request.user,
                status='queued',
                fetched_count=saved_count,
                total_estimate=unread_estimate or estimate or 0,
                max_results=max_results,
                params={
                    'keywords': keywords,
                    'include_read': include_read,
                    'primary_only': data.get('primary_only', settings.GMAIL_ONLY_PRIMARY_DEFAULT),
                },
                last_page_token=next_token,
            )

            job_payload = {
                'id': str(job.job_id),
                'status': job.status,
                'fetched': saved_count,
                'estimate': unread_estimate or estimate or 0,
                'max_results': max_results,
            }

            _start_background_crawl(
                job,
                request.user.id,
                keywords,
                include_read,
                data.get('primary_only', settings.GMAIL_ONLY_PRIMARY_DEFAULT),
                max_results,
                next_token,
                saved_count,
            )

            request.session['active_crawl_job_id'] = str(job.job_id)
            request.session.modified = True

        else:
            logger.info(
                "Crawling complete for user %s: %s emails saved",
                request.user.username,
                saved_count,
            )

        return JsonResponse({
            'success': True,
            'email_count': saved_count,
            'total_found': unread_estimate or estimate or saved_count,
            'job': job_payload,
            'redirect': None,
        })
        
    except GmailAPIError as e:
        message = str(e)
        logger.error("Gmail API error during crawling for user %s: %s", request.user.username, message)

        redirect_url = None
        if _should_force_logout(message):
            logout(request)
            request.session.flush()
            redirect_url = reverse('account_login')

        return JsonResponse({
            'success': False,
            'error': message,
            'redirect': redirect_url,
        })
    except Exception as e:
        logger.error(f"Unexpected error during crawling for user {request.user.username}: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': 'An unexpected error occurred. Please try again.'
        })


@login_required
def crawl_status_api(request, job_id):
    """Return crawl job progress for the current user."""
    try:
        job = CrawlJob.objects.get(job_id=job_id, user=request.user)
    except CrawlJob.DoesNotExist:
        raise Http404("Job not found")

    return JsonResponse({
        'id': str(job.job_id),
        'status': job.status,
        'fetched': job.fetched_count,
        'estimate': job.total_estimate,
        'max_results': job.max_results,
        'message': job.message or '',
    })


@login_required
def kanban_dashboard(request):
    """Kanban dashboard showing emails organized by priority."""
    import json

    # Ensure first-time users get an initial unread import automatically
    try:
        user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if not user_profile.initial_sync_completed:
            # Avoid duplicate heavy fetches if the user already has data
            has_any_email = EmailResult.objects.filter(user=request.user).exists()
            if not has_any_email:
                try:
                    saved_count, total_found, job_created = _run_initial_email_sync(request, user_profile)
                    user_profile.initial_sync_completed = True
                    user_profile.save(update_fields=['initial_sync_completed', 'updated_at'])
                    if saved_count > 0:
                        if job_created:
                            messages.success(
                                request,
                                f"Imported {saved_count} unread emails. We'll keep fetching the rest in the background.",
                            )
                        else:
                            messages.success(
                                request,
                                f"Imported {saved_count} unread emails from your inbox.",
                            )
                except GmailAPIError as exc:
                    logger.warning("Initial sync requires re-auth for %s: %s", request.user.username, exc)
                    messages.warning(request, str(exc))
                except Exception as exc:
                    logger.debug("Initial sync skipped for %s: %s", request.user.username, exc)
    except Exception:
        pass

    active_job_dict = None
    job_id = request.session.get('active_crawl_job_id')
    active_job = None

    if job_id:
        try:
            active_job = CrawlJob.objects.get(job_id=job_id, user=request.user)
            if active_job.status in ['completed', 'failed', 'cancelled']:
                request.session.pop('active_crawl_job_id', None)
                if active_job.status != 'completed':
                    active_job = None
        except CrawlJob.DoesNotExist:
            request.session.pop('active_crawl_job_id', None)
            active_job = None

    if active_job is None:
        pending_job = CrawlJob.objects.filter(
            user=request.user,
            status__in=['queued', 'running'],
        ).order_by('-created_at').first()
        if pending_job:
            request.session['active_crawl_job_id'] = str(pending_job.job_id)
            request.session.modified = True
            active_job = pending_job

    if active_job:
        estimate_value = (
            active_job.total_estimate
            or active_job.max_results
            or active_job.fetched_count
            or 0
        )
        active_job_dict = {
            'id': str(active_job.job_id),
            'status': active_job.status,
            'fetched': active_job.fetched_count,
            'estimate': estimate_value,
            'max_results': active_job.max_results,
            'message': active_job.message or '',
        }


    # Handle POST requests for drag-and-drop updates
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            action = data.get('action')
            service_base = getattr(settings, 'KANBAN_SERVICE_URL', '')
            
            if action == 'update_priority':
                if service_base:
                    update_url = f"{service_base.rstrip('/')}/kanban/update"
                    try:
                        response = requests.post(
                            update_url,
                            json={
                                'email_id': data.get('email_id'),
                                'priority': data.get('priority'),
                                'position': data.get('position', 0),
                            },
                            headers={'X-User-Id': str(request.user.id)},
                            timeout=5,
                        )
                        response.raise_for_status()
                    except requests.RequestException as exc:
                        logger.error(
                            "Kanban service update failed for user %s: %s",
                            request.user.username,
                            exc,
                        )
                        return JsonResponse({'success': False, 'error': 'Kanban service unavailable'}, status=503)
                    return JsonResponse({'success': True})

                email_id = data.get('email_id')
                new_priority = data.get('priority')
                new_position = data.get('position')
                
                email = EmailResult.objects.get(user=request.user, email_id=email_id)
                email.priority = new_priority
                email.board_column = new_priority
                email.board_position = new_position
                email.save()

                try:
                    engine = PriorityEngine(request.user)
                    engine.record_feedback(email.matched_keywords or [], new_priority)
                except Exception as learning_exc:
                    logger.debug("Priority feedback skipped: %s", learning_exc)
                
                return JsonResponse({'success': True})
                
            elif action == 'mark_done':
                email_id = data.get('email_id')
                email = EmailResult.objects.get(user=request.user, email_id=email_id)

                service_success = False
                if service_base:
                    mark_url = f"{service_base.rstrip('/')}/kanban/mark-done"
                    try:
                        response = requests.post(
                            mark_url,
                            json={'email_id': email_id},
                            headers={'X-User-Id': str(request.user.id)},
                            timeout=5,
                        )
                        response.raise_for_status()
                        service_success = True
                    except requests.RequestException as exc:
                        logger.error(
                            "Kanban service mark-done failed for user %s: %s",
                            request.user.username,
                            exc,
                        )

                if service_success:
                    email.refresh_from_db()
                else:
                    email.is_done = True
                    email.save()

                try:
                    engine = PriorityEngine(request.user)
                    engine.record_feedback(email.matched_keywords or [], email.priority)
                except Exception as learning_exc:
                    logger.debug("Priority feedback skipped: %s", learning_exc)
                
                logger.info(f"Email {email_id} marked as done by user {request.user.username}")
                return JsonResponse({'success': True})
            
            elif action == 'bulk_mark_done':
                email_ids = data.get('email_ids', [])
                if not email_ids:
                    return JsonResponse({'success': False, 'error': 'No emails selected'})
                
                updated_count = 0
                service_success = False
                if service_base:
                    bulk_url = f"{service_base.rstrip('/')}/kanban/bulk-mark-done"
                    try:
                        response = requests.post(
                            bulk_url,
                            json={'email_ids': email_ids},
                            headers={'X-User-Id': str(request.user.id)},
                            timeout=5,
                        )
                        response.raise_for_status()
                        service_success = True
                    except requests.RequestException as exc:
                        logger.error(
                            "Kanban service bulk mark-done failed for user %s: %s",
                            request.user.username,
                            exc,
                        )

                if service_success:
                    updated_count = EmailResult.objects.filter(
                        user=request.user,
                        email_id__in=email_ids
                    ).update(is_done=True)
                else:
                    updated_count = EmailResult.objects.filter(
                        user=request.user, 
                        email_id__in=email_ids
                    ).update(is_done=True)

                try:
                    engine = PriorityEngine(request.user)
                    for email in EmailResult.objects.filter(user=request.user, email_id__in=email_ids):
                        engine.record_feedback(email.matched_keywords or [], email.priority)
                except Exception as learning_exc:
                    logger.debug("Priority feedback skipped: %s", learning_exc)
                
                logger.info(f"Bulk marked {updated_count} emails as done by user {request.user.username}")
                return JsonResponse({'success': True, 'updated_count': updated_count})
                
            elif action == 'archive_email':
                email_id = data.get('email_id')
                service_success = False
                if service_base:
                    archive_url = f"{service_base.rstrip('/')}/kanban/archive"
                    try:
                        response = requests.post(
                            archive_url,
                            json={'email_id': email_id},
                            headers={'X-User-Id': str(request.user.id)},
                            timeout=5,
                        )
                        response.raise_for_status()
                        service_success = True
                    except requests.RequestException as exc:
                        logger.error(
                            "Kanban service archive failed for user %s: %s",
                            request.user.username,
                            exc,
                        )

                if not service_success:
                    email = EmailResult.objects.get(user=request.user, email_id=email_id)
                    email.delete()
                else:
                    EmailResult.objects.filter(user=request.user, email_id=email_id).delete()

                return JsonResponse({'success': True})
                
        except EmailResult.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Email not found'})
        except Exception as e:
            logger.error(f"Kanban action error for user {request.user.username}: {str(e)}")
            return JsonResponse({'success': False, 'error': str(e)})
    
    # GET request - show the dashboard
    try:
        user_profile, _ = UserProfile.objects.get_or_create(user=request.user)
        # Ensure we have the Gmail address on the profile for Pub/Sub webhook mapping
        try:
            social_account = SocialAccount.objects.filter(user=request.user, provider='google').first()
            social_email = social_account.extra_data.get('email') if social_account else None
            if social_email and user_profile.gmail_address != social_email:
                user_profile.gmail_address = social_email
                user_profile.save(update_fields=['gmail_address', 'updated_at'])
        except Exception:
            pass

        _schedule_async_gmail_refresh(user_profile.id)

        # Get user's keywords
        keywords = []
        try:
            user_keywords = UserKeywords.objects.get(user=request.user)
            keywords = user_keywords.get_keywords_list()
        except UserKeywords.DoesNotExist:
            pass
        
        # Get emails organized by priority (exclude done emails)
        high_priority_emails = EmailResult.objects.filter(
            user=request.user, 
            priority='high',
            is_done=False
        ).order_by('-parsed_date')
        
        medium_priority_emails = EmailResult.objects.filter(
            user=request.user, 
            priority='medium',
            is_done=False
        ).order_by('-parsed_date')
        
        low_priority_emails = EmailResult.objects.filter(
            user=request.user, 
            priority='low',
            is_done=False
        ).order_by('-parsed_date')
        
        # Get unique senders for filtering (remove duplicates and empty values)
        unique_senders_raw = EmailResult.objects.filter(
            user=request.user,
            sender__isnull=False
        ).exclude(sender='').values_list('sender', flat=True).distinct()
        
        # Clean and deduplicate senders
        unique_senders = sorted(set(sender.strip() for sender in unique_senders_raw if sender.strip()))
        
        # Calculate stats (exclude done emails)
        total_emails = EmailResult.objects.filter(user=request.user, is_done=False).count()
        high_priority_count = high_priority_emails.count()
        medium_priority_count = medium_priority_emails.count()
        low_priority_count = low_priority_emails.count()
        
        context = {
            'keywords': keywords,
            'high_priority_emails': high_priority_emails,
            'medium_priority_emails': medium_priority_emails,
            'low_priority_emails': low_priority_emails,
            'unique_senders': unique_senders,
            'total_emails': total_emails,
            'high_priority_count': high_priority_count,
            'medium_priority_count': medium_priority_count,
            'low_priority_count': low_priority_count,
            'active_crawl_job': active_job_dict,
            'use_sse': bool(getattr(settings, 'GMAIL_PUBSUB_TOPIC', '')),
        }
        
        return render(request, 'dashboard/kanban_dashboard.html', context)
        
    except Exception as e:
        logger.error(f"Kanban dashboard error for user {request.user.username}: {str(e)}")
        messages.error(request, "Error loading dashboard. Please try again.")
        return redirect('dashboard:keywords_entry')


def _generate_gmail_summary(user, *, senders_page=1, keywords_page=1, page_size=5):
    emails = EmailResult.objects.filter(user=user)
    total_emails = emails.count()

    active_emails_qs = emails.filter(is_done=False)
    active_count = active_emails_qs.count()
    resolved_count = max(total_emails - active_count, 0)

    priority_counts = {"high": 0, "medium": 0, "low": 0}
    for row in active_emails_qs.values("priority").annotate(count=Count("id")):
        priority_counts[row["priority"]] = row["count"]

    senders_qs = (
        active_emails_qs.exclude(sender="")
        .values("sender")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    senders_total = senders_qs.count()
    max_sender_pages = max((senders_total + page_size - 1) // page_size, 1)
    senders_page = max(1, min(senders_page, max_sender_pages))
    sender_offset = (senders_page - 1) * page_size
    top_senders = list(senders_qs[sender_offset:sender_offset + page_size])

    keywords_counter = Counter()
    for keyword_list in active_emails_qs.values_list("matched_keywords", flat=True):
        if not keyword_list:
            continue
        for keyword in keyword_list:
            normalized = (keyword or "").strip()
            if normalized:
                keywords_counter[normalized] += 1
    total_keywords_distinct = len(keywords_counter)
    max_keyword_pages = max((total_keywords_distinct + page_size - 1) // page_size, 1)
    keywords_page = max(1, min(keywords_page, max_keyword_pages))
    keyword_offset = (keywords_page - 1) * page_size
    top_keywords = [
        {"keyword": keyword, "count": count}
        for keyword, count in keywords_counter.most_common()[keyword_offset:keyword_offset + page_size]
    ]

    recent_window = timezone.now() - timedelta(days=7)
    recent_inflow = active_emails_qs.filter(parsed_date__gte=recent_window).count()
    keyword_focus = active_emails_qs.filter(keyword_matches__gt=0).count()

    recent_samples = []
    for email in active_emails_qs.order_by("-parsed_date", "-created_at")[:5]:
        display_date = email.parsed_date or email.created_at
        recent_samples.append(
            {
                "subject": email.subject[:120] if email.subject else "(No subject)",
                "sender": email.sender,
                "date": display_date,
            }
        )

    highlights = []
    if total_emails:
        backlog_ratio = (active_count / total_emails) * 100 if total_emails else 0
        highlights.append(
            {
                "title": "Active Workload",
                "detail": f"{active_count} live conversations with {resolved_count} resolved ({backlog_ratio:.1f}% still open).",
            }
        )
    if priority_counts.get("high"):
        highlights.append(
            {
                "title": "High-Priority Focus",
                "detail": f"{priority_counts['high']} threads flagged high priority need attention first.",
            }
        )
    if top_senders:
        lead_sender = top_senders[0]
        highlights.append(
            {
                "title": "Top Sender",
                "detail": f"{lead_sender['sender']} accounts for {lead_sender['count']} active emails.",
            }
        )
    if top_keywords:
        top_keyword = top_keywords[0]
        highlights.append(
            {
                "title": "Hot Keyword",
                "detail": f"'{top_keyword['keyword']}' appears in {top_keyword['count']} tracked threads.",
            }
        )

    narrative_lines = ["Gmail operations overview:"]
    narrative_lines.append(
        f"• Active load: {active_count} active, {resolved_count} completed conversations tracked." if total_emails else "• Inbox is quiet – no tracked emails yet."
    )
    narrative_lines.append(
        "• Priority mix – "
        + ", ".join(
            [
                f"{priority.title()}: {priority_counts.get(priority, 0)}"
                for priority in ["high", "medium", "low"]
            ]
        )
    )
    if recent_inflow:
        narrative_lines.append(f"• {recent_inflow} conversations landed in the last 7 days.")
    if keyword_focus:
        narrative_lines.append(
            f"• {keyword_focus} threads reference tracked keywords – align with ops follow-ups."
        )
    if top_senders:
        sender_line = ", ".join(
            f"{item['sender']} ({item['count']})" for item in top_senders[:3]
        )
        narrative_lines.append(f"• Frequent senders – {sender_line}.")
    if top_keywords:
        keyword_line = ", ".join(
            f"{item['keyword']} ({item['count']})" for item in top_keywords[:3]
        )
        narrative_lines.append(f"• Keyword hotspots – {keyword_line}.")
    if recent_samples:
        sample_line = "; ".join(
            f"{sample['subject']} ({sample['sender']})" for sample in recent_samples
        )
        narrative_lines.append(f"• Watchlist – {sample_line}.")

    priority_breakdown = [
        {
            "priority": key,
            "label": key.title(),
            "count": priority_counts.get(key, 0),
        }
        for key in ["high", "medium", "low"]
    ]

    return {
        "generated_at": timezone.now(),
        "has_data": total_emails > 0,
        "total_emails": total_emails,
        "active_emails": active_count,
        "resolved_emails": resolved_count,
        "recent_inflow": recent_inflow,
        "keyword_focus": keyword_focus,
        "priority_breakdown": priority_breakdown,
        "top_senders": top_senders,
        "senders_page": senders_page,
        "senders_pages": max_sender_pages,
        "top_keywords": top_keywords,
        "keywords_page": keywords_page,
        "keywords_pages": max_keyword_pages,
        "highlights": highlights,
        "narrative": "\n".join(narrative_lines),
        "recent_samples": recent_samples,
    }


@login_required
def gmail_summary(request):
    try:
        senders_page = int(request.GET.get("senders_page", "1"))
    except ValueError:
        senders_page = 1
    try:
        keywords_page = int(request.GET.get("keywords_page", "1"))
    except ValueError:
        keywords_page = 1

    summary = _generate_gmail_summary(
        request.user,
        senders_page=senders_page,
        keywords_page=keywords_page,
    )
    return render(
        request,
        "dashboard/gmail_summary.html",
        {
            "summary": summary,
            "current_senders_page": senders_page,
            "current_keywords_page": keywords_page,
        },
    )

def _follow_up_context_for(request, *message_ids):
    """Return the enquiry follow-up context only if it was started for one of these Gmail messages."""
    context = request.session.get('follow_up_context')
    if not isinstance(context, dict):
        return None
    wanted = {str(m) for m in message_ids if m}
    if str(context.get('gmail_message_id') or '') not in wanted:
        return None
    return context


def _follow_up_recipient(context):
    """Return the customer address confirmed for an enquiry follow-up, or '' if it is not valid.

    There is deliberately no fallback to a guessed address: without a confirmed customer
    email the follow-up cannot be sent.
    """
    address = str((context or {}).get('customer_email') or '').strip()
    if not address:
        return ''
    try:
        validate_email(address)
    except ValidationError:
        return ''
    return address


def _payment_terms_kwargs(context):
    """due_on / credit_days / today for payment reminder text (credit period on the enquiry)."""
    enquiry_id = str((context or {}).get('enquiry_id') or '')
    due_on, credit_days = payment_terms.payment_terms_for(enquiry_id)
    kwargs = {'due_on': due_on, 'credit_days': credit_days, 'today': timezone.localdate(),
              'customer_name': (context or {}).get('customer_name') or ''}
    # Manual follow-ups use the same templates as the automatic ones, including the enquiry's own share link
    # and password for its latest quotation / proforma invoice.
    try:
        from enquiries import follow_up_links
        kwargs['share_url'], kwargs['share_password'] = follow_up_links.for_follow_up(enquiry_id, context)
    except Exception as exc:  # no share link yet: the email is shown without the link paragraph
        logger.warning('No share link for the follow-up of enquiry %s: %s', enquiry_id, exc)
    return kwargs


def _follow_up_default_body(context, user=None, stage=DEFAULT_FOLLOW_UP_STAGE):
    return build_follow_up_body(
        stage,
        reference=(context or {}).get('enquiry_id') or '',
        subject=(context or {}).get('subject') or '',
        user=user,
        attempt=FollowUpSent.objects.filter(enquiry_id=str((context or {}).get('enquiry_id') or ''),
                                            status=FollowUpSent.STATUS_SENT).count() + 1,
        **_payment_terms_kwargs(context),
    )


def _parse_next_follow_up(raw):
    """Return (date or None, error message or '') for the 'next follow-up' date on the compose form."""
    raw = (raw or '').strip()
    if not raw:
        return None, ''
    value = parse_date(raw)
    if value is None:
        return None, "Please enter a valid next follow-up date."
    if value < timezone.localdate():
        return None, "The next follow-up date cannot be in the past."
    return value, ''


def _auto_follow_up_plan(context, user):
    """What automation would send after this manual follow-up, per stage.

    Returns {'this_attempt', 'limit', 'paused', 'enabled', 'stages': {stage: [{'attempt', 'when', 'body'}]}}.
    """
    enquiry_id = str((context or {}).get('enquiry_id') or '')
    sent_count = FollowUpSent.objects.filter(enquiry_id=enquiry_id, status=FollowUpSent.STATUS_SENT).count()
    this_attempt = sent_count + 1
    limit = int(getattr(settings, 'FOLLOW_UP_MAX_COUNT', 4) or 0)
    gaps = list(getattr(settings, 'FOLLOW_UP_AUTO_GAP_DAYS', []) or [7])
    later_attempts = list(range(this_attempt + 1, limit + 1)) if limit else []
    terms = _payment_terms_kwargs(context)
    stages = {}
    for key, _label in FOLLOW_UP_STAGE_CHOICES:
        plan = []
        for attempt in later_attempts:
            if attempt == this_attempt + 1:
                when = "On the “Next follow-up on” date"
            else:
                gap = gaps[attempt - 2] if attempt - 2 < len(gaps) else gaps[-1]
                when = f"{gap} days after follow-up #{attempt - 1}"
            plan.append({
                'attempt': attempt,
                'when': when,
                'final': attempt == limit,
                'body': build_follow_up_body(
                    key, attempt=attempt, reference=enquiry_id,
                    subject=(context or {}).get('subject') or '', user=user, **terms,
                ),
            })
        stages[key] = plan
    sender_username = (getattr(settings, 'FOLLOW_UP_AUTO_SENDER', '') or '').strip()
    sender = get_user_model().objects.filter(username=sender_username).first() if sender_username else None
    return {
        'this_attempt': this_attempt,
        'limit': limit,
        'sender': (sender.email or sender.username) if sender else '',
        'paused': EnquiryContact.objects.filter(enquiry_id=enquiry_id, auto_follow_up_paused=True).exists(),
        'enabled': bool(getattr(settings, 'FOLLOW_UP_AUTO_ENABLED', False)),
        'stages': stages,
    }


def _save_auto_follow_up_choice(enquiry_id, enabled, user):
    """Remember on the enquiry whether automation may continue after this manual follow-up."""
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    if contact.auto_follow_up_paused == (not enabled):
        return
    contact.auto_follow_up_paused = not enabled
    contact.updated_by = user
    contact.save(update_fields=['auto_follow_up_paused', 'updated_by', 'updated_at'])


def _email_address(value):
    from email.utils import parseaddr
    return (parseaddr(value or '')[1] or '').strip().lower()


def _routed_cc_by_stage(enquiry_id, base_cc, recipient):
    """CC to suggest per follow-up stage, and a line saying who was added (enquiries linked to a KYC customer).

    Returns ({stage: "a@x, b@y"}, {stage: note}); without a KYC link every stage keeps `base_cc`.
    """
    from kyc import routing as kyc_routing

    base, _ = payment_terms.parse_cc(base_cc, exclude=recipient)
    sent = FollowUpSent.objects.filter(enquiry_id=str(enquiry_id), status=FollowUpSent.STATUS_SENT)
    total, payment = sent.count(), sent.filter(stage=PAYMENT_STAGE).count()
    cc, notes = {}, {}
    for key, _label in FOLLOW_UP_STAGE_CHOICES:
        attempt = (payment if key == PAYMENT_STAGE else total) + 1  # payment reminders are numbered on their own
        cc[key] = ', '.join(kyc_routing.add_cc(base, enquiry_id, key, attempt, to=recipient,
                                               limit=payment_terms.MAX_CC))
        notes[key] = kyc_routing.describe(enquiry_id, key, attempt)
    return cc, notes


def _save_follow_up_cc(enquiry_id, cc_list, user):
    """Remember the CC so the next follow-ups (manual and automatic) use it too."""
    value = ", ".join(cc_list)
    contact, _ = EnquiryContact.objects.get_or_create(enquiry_id=str(enquiry_id))
    if contact.follow_up_cc == value:
        return
    contact.follow_up_cc = value
    contact.updated_by = user
    contact.save(update_fields=['follow_up_cc', 'updated_by', 'updated_at'])


def _start_follow_up_record(request, context, *, recipient, subject, body, stage='', next_follow_up_on=None,
                            cc_list=()):
    """Create the Pending history row. Returns None if this form was already submitted."""
    try:
        with transaction.atomic():
            return FollowUpSent.objects.create(
                enquiry_id=str(context.get('enquiry_id') or ''),
                customer_email=recipient,
                customer_name=context.get('customer_name') or '',
                subject=subject or context.get('subject') or '',
                body=body or '',
                gmail_message_id=context.get('gmail_message_id') or '',
                gmail_thread_id=context.get('gmail_thread_id') or '',
                sent_by=request.user,
                status=FollowUpSent.STATUS_PENDING,
                submission_token=context.get('token') or None,
                stage=stage or '',
                next_follow_up_on=next_follow_up_on,
                cc=", ".join(cc_list),
            )
    except IntegrityError:
        return None


@login_required
@require_http_methods(["GET", "POST"])
def compose_email(request, email_id):
    """Show the compose surface for replying to an email."""
    _ensure_local_demo_emails(request.user)
    gmail_service = GmailService(request.user)

    try:
        email_record, message = _resolve_email_context(request.user, email_id, gmail_service)
    except GmailAPIError as exc:
        messages.error(request, str(exc))
        return redirect('gmail_ai')

    try:
        thread_messages = gmail_service.get_thread_messages(message.get('thread_id'), limit=15)
    except GmailAPIError as exc:
        logger.warning("Thread fetch failed for user %s: %s", request.user.username, exc)
        thread_messages = [message]

    conversation = thread_messages or [message]

    # Follow-ups started from the enquiries board go to the confirmed customer address.
    follow_up_context = _follow_up_context_for(request, email_id, message.get('id'))
    follow_up_recipient = _follow_up_recipient(follow_up_context) if follow_up_context else ''

    own_addresses = set()
    if follow_up_context:
        try:
            own_addresses = {a.lower() for a in gmail_service.own_addresses()}
        except Exception:  # profile lookup is best effort
            own_addresses = {(request.user.email or '').lower()} - {''}

    def _is_pdf(meta):
        return 'pdf' in (meta.get('mimeType') or '').lower() or (meta.get('filename') or '').lower().endswith('.pdf')

    our_pdf_message = None
    if follow_up_context:
        # Re-send our own quote, not the customer's files: the latest message we sent with a PDF.
        our_pdf_message = next(
            (msg for msg in reversed(conversation)
             if _email_address(msg.get('sender')) in own_addresses and any(_is_pdf(a) for a in msg.get('attachments') or [])),
            None,
        )

    latest_with_attachments = our_pdf_message or next(
        (msg for msg in reversed(conversation) if msg.get('attachments')),
        None,
    )
    attachment_metadata = latest_with_attachments.get('attachments', []) if latest_with_attachments else []

    # Step 9: warn if the customer's latest message has not been answered yet.
    customer_waiting, customer_waiting_since = False, None
    if follow_up_context and conversation:
        last_message = conversation[-1]
        last_sender = _email_address(last_message.get('sender'))
        if last_sender and own_addresses and last_sender not in own_addresses:
            customer_waiting = True
            customer_waiting_since = last_message.get('parsed_date')

    attachment_lookup = {}
    attachment_choices = []
    for meta in attachment_metadata:
        token = _encode_attachment_metadata(
            {
                'message_id': meta.get('message_id'),
                'attachment_id': meta.get('attachment_id'),
                'filename': meta.get('filename'),
                'mime_type': meta.get('mimeType'),
            }
        )
        attachment_lookup[token] = meta
        size = meta.get('size') or 0
        label = f"{meta.get('filename')} ({max(size // 1024, 1)} KB)"
        attachment_choices.append((token, label))

    subject = message.get('subject') or ''
    if subject and not subject.lower().startswith('re:'):
        subject = f"Re: {subject}"

    initial = {'subject': subject}
    # Stage suggested by the enquiry (payment pending for accepted orders).
    suggested_stage = (follow_up_context or {}).get('stage') or DEFAULT_FOLLOW_UP_STAGE
    if suggested_stage not in FOLLOW_UP_STAGE_KEYS:
        suggested_stage = DEFAULT_FOLLOW_UP_STAGE
    if follow_up_context:
        initial['body'] = _follow_up_default_body(follow_up_context, user=request.user, stage=suggested_stage)
        # Tick only our own quote PDFs; never re-send the customer's files by default.
        initial['include_attachments'] = [
            token for token, meta in attachment_lookup.items()
            if our_pdf_message is not None and _is_pdf(meta)
        ]

    follow_up_stage = suggested_stage
    next_follow_up_on, next_follow_up_error = None, ''
    auto_follow_up_choice = None  # None = not posted yet; True/False = the employee's choice
    follow_up_cc, cc_list, cc_error = '', [], ''
    follow_up_stage_cc, follow_up_routing_notes = {}, {}
    credit_hold = None
    if follow_up_context:
        follow_up_cc = payment_terms.default_cc(follow_up_context.get('enquiry_id') or '')
        follow_up_stage_cc, follow_up_routing_notes = _routed_cc_by_stage(
            follow_up_context.get('enquiry_id') or '', follow_up_cc, follow_up_recipient)
        follow_up_cc = follow_up_stage_cc.get(suggested_stage, follow_up_cc)
        credit_hold = payment_terms.credit_hold_until(follow_up_context.get('enquiry_id') or '')
    if request.method == 'POST' and follow_up_context:
        follow_up_cc = (request.POST.get('follow_up_cc') or '').strip()
        cc_list, cc_error = payment_terms.parse_cc(follow_up_cc, exclude=follow_up_recipient)
        follow_up_stage = request.POST.get('follow_up_stage') or DEFAULT_FOLLOW_UP_STAGE
        if follow_up_stage not in FOLLOW_UP_STAGE_KEYS:
            follow_up_stage = DEFAULT_FOLLOW_UP_STAGE
        next_follow_up_on, next_follow_up_error = _parse_next_follow_up(request.POST.get('next_follow_up_on'))
        if request.POST.get('auto_follow_up_shown') == '1':
            auto_follow_up_choice = request.POST.get('auto_follow_up') == '1'
        if auto_follow_up_choice and not next_follow_up_on and not next_follow_up_error:
            next_follow_up_error = "Choose a “Next follow-up on” date so automatic follow-ups know when to start."

    if request.method == 'POST':
        posted_token = (request.POST.get('follow_up_token') or '').strip()
        expected_token = (follow_up_context or {}).get('token') or ''
        if (posted_token or follow_up_context) and posted_token != expected_token:
            messages.warning(request, "This follow-up was already submitted, so it was not sent again.")
            return redirect('enquiries:board')

    form = ComposeReplyForm(
        request.POST or None,
        attachments=attachment_choices,
        initial=initial,
    )

    if request.method == 'POST' and follow_up_context and not follow_up_recipient:
        messages.error(request, "No confirmed customer email for this follow-up, so it was not sent.")
    elif request.method == 'POST' and follow_up_context and next_follow_up_error:
        messages.error(request, f"{next_follow_up_error} The follow-up was not sent.")
    elif request.method == 'POST' and follow_up_context and cc_error:
        messages.error(request, f"{cc_error} The follow-up was not sent.")
    elif request.method == 'POST' and form.is_valid():
        selected_tokens = form.cleaned_data.get('include_attachments') or []
        attachment_payloads = []
        for token in selected_tokens:
            decoded = _decode_attachment_metadata(token) or {}
            message_id_ref = decoded.get('message_id')
            attachment_id = decoded.get('attachment_id')
            filename = decoded.get('filename')
            mime_type = decoded.get('mime_type')
            if not (message_id_ref and attachment_id):
                continue
            try:
                content = gmail_service.download_attachment(message_id_ref, attachment_id)
            except GmailAPIError as exc:
                logger.warning(
                    "Attachment download failed for user %s message %s: %s",
                    request.user.username,
                    message_id_ref,
                    exc,
                )
                continue
            attachment_payloads.append({
                'filename': filename,
                'mime_type': mime_type,
                'content': content,
            })

        if not follow_up_context:
            try:
                gmail_service.send_reply(
                    original_message=message,
                    body=form.cleaned_data['body'],
                    attachments=attachment_payloads,
                    subject=form.cleaned_data.get('subject'),
                )
            except GmailAPIError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Reply sent successfully.")
                return redirect('gmail_ai')
        else:
            record = _start_follow_up_record(
                request,
                follow_up_context,
                recipient=follow_up_recipient,
                subject=form.cleaned_data.get('subject') or '',
                body=form.cleaned_data['body'],
                stage=follow_up_stage,
                next_follow_up_on=next_follow_up_on,
                cc_list=cc_list,
            )
            if record is None:
                messages.warning(request, "This follow-up was already submitted, so it was not sent again.")
                return redirect('enquiries:board')
            try:
                sent = gmail_service.send_reply(
                    original_message=message,
                    body=form.cleaned_data['body'],
                    attachments=attachment_payloads,
                    subject=form.cleaned_data.get('subject'),
                    to=follow_up_recipient,
                    cc=cc_list or None,
                )
            except Exception as exc:  # Gmail API or network failure
                logger.warning("Follow-up send failed for enquiry %s: %s", record.enquiry_id, exc)
                record.status = FollowUpSent.STATUS_FAILED
                record.error_message = str(exc)[:1000]
                record.save(update_fields=['status', 'error_message'])
                # New token so the employee can retry from the same page.
                follow_up_context['token'] = secrets.token_urlsafe(24)
                request.session.modified = True
                detail = str(exc) if isinstance(exc, GmailAPIError) else "a network or Gmail error occurred"
                messages.error(request, f"Follow-up was not sent: {detail}. Nothing was delivered; you can try again.")
            else:
                record.status = FollowUpSent.STATUS_SENT
                record.gmail_message_id = (sent or {}).get('id') or record.gmail_message_id
                record.gmail_thread_id = (sent or {}).get('threadId') or record.gmail_thread_id
                record.sent_at = timezone.now()
                record.save(update_fields=['status', 'gmail_message_id', 'gmail_thread_id', 'sent_at'])
                if auto_follow_up_choice is not None:
                    _save_auto_follow_up_choice(record.enquiry_id, auto_follow_up_choice, request.user)
                _save_follow_up_cc(record.enquiry_id, cc_list, request.user)
                request.session.pop('follow_up_context', None)
                messages.success(
                    request,
                    f"Follow-up sent to {follow_up_recipient}"
                    + (f" (CC: {', '.join(cc_list)})" if cc_list else "")
                    + f" and recorded for enquiry #{record.enquiry_id}."
                    + (" Next follow-ups will be sent automatically." if auto_follow_up_choice
                       else " Automatic follow-ups are off for this enquiry." if auto_follow_up_choice is False
                       else ""),
                )
                return redirect('enquiries:board')

    conversation_for_template = []
    for msg in conversation:
        conversation_for_template.append(
            {
                'id': msg.get('id'),
                'sender': msg.get('sender'),
                'to': msg.get('to'),
                'date': msg.get('parsed_date') or msg.get('date'),
                'subject': msg.get('subject'),
                'body': msg.get('body_text'),
            }
        )

    context = {
        'email': email_record,
        'form': form,
        'conversation': conversation_for_template,
        'attachment_choices': attachment_choices,
        'attachment_entries': [
            {
                'input_html': subwidget.tag(),
                'id': subwidget.id_for_label,
                'label': subwidget.choice_label,
                'preview_url': reverse(
                    'dashboard:compose_attachment_preview',
                    args=[email_record.email_id, subwidget.data['value']],
                ),
                'filename': attachment_lookup.get(subwidget.data['value'], {}).get('filename')
                or subwidget.choice_label,
                'mime_type': attachment_lookup.get(subwidget.data['value'], {}).get('mime_type')
                or 'application/octet-stream',
            }
            for subwidget in form['include_attachments']
        ],
        'ai_reply_url': reverse('dashboard:generate_ai_reply', args=[email_record.email_id]),
        'follow_up_context': follow_up_context,
        'follow_up_recipient': follow_up_recipient,
    }
    if follow_up_context:
        posted_next = (request.POST.get('next_follow_up_on') or '').strip() if request.method == 'POST' else ''
        context.update({
            'follow_up_stage_choices': FOLLOW_UP_STAGE_CHOICES,
            'follow_up_stage': follow_up_stage,
            'follow_up_stage_bodies': all_stage_bodies(
                reference=follow_up_context.get('enquiry_id') or '',
                subject=follow_up_context.get('subject') or '',
                user=request.user,
                **_payment_terms_kwargs(follow_up_context),
            ),
            # Inside a credit period the next payment reminder defaults to the due date.
            'next_follow_up_default': posted_next or (
                credit_hold if credit_hold and follow_up_stage == PAYMENT_STAGE else
                timezone.localdate() + timedelta(days=getattr(settings, 'FOLLOW_UP_DEFAULT_GAP_DAYS', 3))
            ).isoformat(),
            'follow_up_cc': follow_up_cc,
            'follow_up_stage_cc': follow_up_stage_cc,
            'follow_up_routing_notes': follow_up_routing_notes,
            'follow_up_routing_note': follow_up_routing_notes.get(follow_up_stage, ''),
            'credit_hold': credit_hold,
            'next_follow_up_min': timezone.localdate().isoformat(),
            'customer_waiting': customer_waiting,
            'customer_waiting_since': customer_waiting_since,
            'quote_attachment_from_us': our_pdf_message is not None,
        })
        auto_plan = _auto_follow_up_plan(follow_up_context, request.user)
        context['auto_plan'] = auto_plan
        context['auto_follow_up_checked'] = (
            auto_follow_up_choice if auto_follow_up_choice is not None else not auto_plan['paused']
        )

    return render(request, 'dashboard/compose_email.html', context)


@login_required
@require_http_methods(["GET"])
def compose_attachment_preview(request, email_id: str, token: str):
    """Stream a Gmail attachment for in-browser preview."""
    metadata = _decode_attachment_metadata(token) or {}
    email_record = _latest_email_record(request.user, email_id)
    message_id = metadata.get('message_id') or (email_record.email_id if email_record else email_id)
    attachment_id = metadata.get('attachment_id')
    if not attachment_id:
        return HttpResponse(status=400)

    gmail_service = GmailService(request.user)

    local_content = gmail_service._get_local_attachment_content(message_id, attachment_id)
    if local_content is not None:
        filename = metadata.get('filename') or "attachment"
        mime_type = metadata.get('mime_type') or "application/octet-stream"
        safe_filename = filename.replace('"', "")
        response = HttpResponse(local_content, content_type=mime_type)
        response["Content-Disposition"] = f'inline; filename="{safe_filename}"'
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response

    try:
        content = gmail_service.download_attachment(message_id, attachment_id)
    except GmailAPIError as exc:
        fallback_content = gmail_service._get_local_attachment_content(message_id, attachment_id)
        if fallback_content is not None:
            filename = metadata.get('filename') or "attachment"
            mime_type = metadata.get('mime_type') or "application/octet-stream"
            safe_filename = filename.replace('"', "")
            response = HttpResponse(fallback_content, content_type=mime_type)
            response["Content-Disposition"] = f'inline; filename="{safe_filename}"'
            response["X-Robots-Tag"] = "noindex, nofollow"
            return response
        return HttpResponse(str(exc), status=502)

    filename = metadata.get('filename') or "attachment"
    mime_type = metadata.get('mime_type') or "application/octet-stream"
    safe_filename = filename.replace('"', "")

    response = HttpResponse(content, content_type=mime_type)
    response["Content-Disposition"] = f'inline; filename="{safe_filename}"'
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


@login_required
@require_http_methods(["POST"])
def generate_ai_reply_api(request, email_id):
    """Generate an AI reply for a given email thread."""
    data = json.loads(request.body or '{}')
    tone = data.get('tone')

    gmail_service = GmailService(request.user)
    try:
        _email_record, message = _resolve_email_context(request.user, email_id, gmail_service)
        thread_messages = gmail_service.get_thread_messages(message.get('thread_id'), limit=10)
    except Http404:
        return JsonResponse({'error': 'Email not found'}, status=404)
    except GmailAPIError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    summary = _build_thread_summary(thread_messages or [message])

    # On a follow-up started from the enquiries board, draft a follow-up rather than a generic reply.
    follow_up_context = _follow_up_context_for(request, email_id, message.get('id'))
    if follow_up_context:
        ai_request = AIReplyRequest(
            thread_summary=summary,
            tone=tone,
            purpose='follow_up',
            customer_name=follow_up_context.get('customer_name') or '',
            enquiry_subject=follow_up_context.get('subject') or '',
        )
    else:
        ai_request = AIReplyRequest(thread_summary=summary, tone=tone)

    try:
        ai_service = AIReplyService()
        reply_text = ai_service.generate_reply(ai_request)
    except AIServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=400)

    return JsonResponse({'reply': reply_text, 'is_sample': bool(ai_service.last_reply_was_sample)})


@login_required
def kanban_snapshot_api(request):
    """Return latest board data for incremental refresh."""
    _ensure_local_demo_emails(request.user)

    service_base = getattr(settings, 'KANBAN_SERVICE_URL', '')
    if service_base:
        snapshot_url = f"{service_base.rstrip('/')}/kanban/snapshot"
        try:
            response = requests.get(
                snapshot_url,
                headers={'X-User-Id': str(request.user.id)},
                timeout=5,
            )
            response.raise_for_status()
            return JsonResponse(response.json())
        except requests.RequestException as exc:
            logger.error(
                "Kanban service snapshot failed for user %s: %s",
                request.user.username,
                exc,
            )
            return JsonResponse({'error': 'Kanban service unavailable'}, status=503)

    # Local fallback: opportunistically fetch new emails since last known history
    # when Pub/Sub push is not configured. This makes the board show new mail
    # during periodic polling in local/dev environments.
    try:
        if not getattr(settings, 'GMAIL_PUBSUB_TOPIC', ''):
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            # Ensure we have a starting history id
            if not profile.gmail_history_id:
                try:
                    gs = GmailService(request.user)
                    profile_info = gs.get_user_profile()
                    if profile_info and profile_info.get('history_id'):
                        profile.gmail_history_id = str(profile_info['history_id'])
                        profile.save(update_fields=['gmail_history_id', 'updated_at'])
                except GmailAPIError:
                    pass
                except Exception:
                    pass
            # Fetch and persist new messages incrementally
            if profile.gmail_history_id:
                try:
                    _process_history_update(profile, profile.gmail_history_id)
                except GmailAPIError:
                    pass
                except Exception:
                    pass
    except Exception:
        # Never break snapshot rendering due to background refresh issues
        pass

    emails = EmailResult.objects.filter(user=request.user, is_done=False)

    priorities = {'high': [], 'medium': [], 'low': []}
    for email in emails.order_by('-parsed_date'):
        bucket = priorities.setdefault(email.priority, [])
        bucket.append(_serialize_email(email))

    counts = {
        'high': len(priorities['high']),
        'medium': len(priorities['medium']),
        'low': len(priorities['low']),
        'total': emails.count(),
    }

    return JsonResponse({'emails': priorities, 'counts': counts})


@login_required
def kanban_stream(request):
    """Server-sent events endpoint emitting board changes."""
    subscriber_queue = realtime.subscribe(request.user.id)

    def stream():
        try:
            yield ': connected\n\n'
            while True:
                try:
                    message = subscriber_queue.get(timeout=30)
                    yield f"data: {message}\n\n"
                except queue.Empty:
                    yield ': keep-alive\n\n'
        except GeneratorExit:
            pass
        finally:
            realtime.unsubscribe(request.user.id, subscriber_queue)

    response = StreamingHttpResponse(stream(), content_type='text/event-stream; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


@csrf_exempt
@require_http_methods(["POST"])
def gmail_push_webhook(request):
    """Handle Gmail Pub/Sub push notifications."""
    try:
        envelope = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    message = envelope.get('message', {})
    if not message:
        return HttpResponse(status=204)

    attributes = message.get('attributes', {})
    verification_token = attributes.get('token')
    expected_token = getattr(settings, 'GMAIL_PUBSUB_VERIFICATION_TOKEN', '')
    if expected_token and verification_token != expected_token:
        return HttpResponse(status=403)

    data = message.get('data')
    if not data:
        return HttpResponse(status=204)

    try:
        decoded = base64.b64decode(data).decode('utf-8')
        payload = json.loads(decoded)
    except Exception:
        return HttpResponse(status=400)

    email_address = payload.get('emailAddress')
    history_id = payload.get('historyId')
    if not email_address or not history_id:
        return HttpResponse(status=204)

    try:
        profile = UserProfile.objects.get(gmail_address=email_address)
    except UserProfile.DoesNotExist:
        logger.warning("Received Gmail push for unknown address %s", email_address)
        return HttpResponse(status=204)

    try:
        processed = _process_history_update(profile, history_id)
        logger.info("Processed %s new messages for user %s", processed, profile.user.username)
    except Exception as exc:
        logger.exception("Push processing failed for user %s", profile.user.username)

    return HttpResponse(status=204)

@login_required
def magicwand_auth_complete(request):
    """Redirect back to the MagicWand shell once Google auth completes."""

    frontend_base = resolve_frontend_base(request)

    # Optional return_to lets the frontend request a specific path
    return_to = request.GET.get('return_to', '')
    if return_to and return_to.startswith('/'):
        target = f'{frontend_base}{return_to}'
    else:
        target = f'{frontend_base}/'

    response = redirect(target)
    response['Cache-Control'] = 'no-store'
    return response

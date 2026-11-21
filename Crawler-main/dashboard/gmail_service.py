"""
Gmail API service for secure email retrieval and processing.
Handles OAuth token management, API rate limiting, and error handling.
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from allauth.socialaccount.models import SocialToken, SocialApp
from django.contrib.auth.models import User
from django.conf import settings
from django.utils import timezone
from cryptography.fernet import InvalidToken

from .models import EmailResult, EncryptedTokenStorage

logger = logging.getLogger(__name__)

_LOCAL_DEMO_ATTACHMENTS: Dict[str, List[Dict[str, str]]] = {
    "demo-medium-001": [
        {
            "attachment_id": "demo-medium-001-quote",
            "filename": "avicad-quote.txt",
            "mime_type": "text/plain",
            "content_b64": (
                "RGVtbyBDQUQgcXVvdGUgZm9yIGxvY2FsIHByZXZpZXcuCkxpbmUgMjogQmFzaWMgcHJv"
                "ZmlsZSBzdW1tYXJ5Lgo="
            ),
        },
        {
            "attachment_id": "demo-medium-001-summary",
            "filename": "supplier-summary.csv",
            "mime_type": "text/csv",
            "content_b64": (
                "c3VwcGxpZXIsY29udGFjdCxzdGF0dXMK"
                "QXZpZ2lyaSBTdXBwbHksc3VwcG9ydEBhdmlnaXJpLmNvbSxBY3RpdmUK"
                "VGVjaCBJbm5vdmF0aW9ucyxibHVlcHJpbnRAZXhhbXBsZS5jb20sT25nb2luZw=="
            ),
        },
    ],
    "demo-high-001": [
        {
            "attachment_id": "demo-high-001-summary",
            "filename": "expedite-summary.txt",
            "mime_type": "text/plain",
            "content_b64": (
                "RXhwZWRpdGUgcmVxdWVzdCBzdW1tYXJ5IGZvciB1cmdlbnQgZ2Fza2V0IG9yZGVyLgpS"
                "ZXF1aXJlZCBzaGlwIGRhdGU6IE9jdCAyOCwgMjAyNS4K"
            ),
        },
        {
            "attachment_id": "demo-high-001-pricing",
            "filename": "pricing-breakdown.csv",
            "mime_type": "text/csv",
            "content_b64": (
                "aXRlbSxxdHksdW5pdF9wcmljZSx0b3RhbApQcm9maWxlLTQyLDUwMCwyLjk1LDE0NzUu"
                "MDAK"
            ),
        },
    ],
}

class GmailAPIError(Exception):
    """Custom exception for Gmail API related errors."""
    pass


class GmailService:
    """Service class for Gmail API operations."""
    
    def __init__(self, user: User):
        self.user = user
        self.service = None
        self.credentials = None

    @staticmethod
    def _is_local_environment() -> bool:
        deploy_env = (getattr(settings, "DEPLOY_ENV", "") or "").lower()
        if deploy_env in {"local", "dev", "development"}:
            return True
        return bool(getattr(settings, "DEBUG", False))

    def _local_demo_enabled(self) -> bool:
        if not getattr(self.user, "is_authenticated", False):
            return False
        if os.getenv("ENABLE_LOCAL_GMAIL_DEMO", "1").lower() not in {"1", "true", "yes"}:
            return False
        return self._is_local_environment()

    def _get_local_attachment_content(self, message_id: Optional[str], attachment_id: str) -> Optional[bytes]:
        if not self._local_demo_enabled():
            return None

        lookup_id = message_id or ""

        def decode(entry: Dict[str, str]) -> Optional[bytes]:
            data = entry.get("content_b64")
            if not data:
                return b""
            try:
                return base64.b64decode(data)
            except Exception:  # pragma: no cover - defensive for malformed data
                return b""

        for entry in _LOCAL_DEMO_ATTACHMENTS.get(lookup_id, []):
            if entry.get("attachment_id") == attachment_id:
                return decode(entry)

        for attachment_list in _LOCAL_DEMO_ATTACHMENTS.values():
            for entry in attachment_list:
                if entry.get("attachment_id") == attachment_id:
                    return decode(entry)

        return None

    def _local_email_to_message(self, email: EmailResult) -> Dict:
        parsed_date = email.parsed_date or timezone.now()
        if timezone.is_naive(parsed_date):
            parsed_date = timezone.make_aware(parsed_date, timezone.utc)

        to_address = self.user.email or f"{self.user.username or 'user'}@example.com"

        attachment_payloads: List[Dict[str, Any]] = []
        for attachment in _LOCAL_DEMO_ATTACHMENTS.get(email.email_id, []):
            decoded = self._get_local_attachment_content(email.email_id, attachment["attachment_id"]) or b""
            attachment_payloads.append(
                {
                    "message_id": email.email_id,
                    "attachment_id": attachment["attachment_id"],
                    "filename": attachment["filename"],
                    "mimeType": attachment.get("mime_type", "application/octet-stream"),
                    "size": len(decoded),
                }
            )

        return {
            "id": email.email_id,
            "thread_id": email.thread_id or f"{email.email_id}-thread",
            "history_id": None,
            "snippet": email.snippet,
            "labels": email.labels or [],
            "subject": email.subject,
            "date": email.date or parsed_date.strftime("%a, %d %b %Y %H:%M:%S %z"),
            "parsed_date": parsed_date,
            "sender": email.sender,
            "reply_to": email.sender,
            "to": to_address,
            "cc": "",
            "bcc": "",
            "body_text": email.snippet,
            "message_id_header": email.message_id_header or f"<{email.email_id}@magicwand.local>",
            "references": "",
            "in_reply_to": "",
            "attachments": attachment_payloads,
            "payload": {},
            "headers": [],
            "matched_keywords": email.matched_keywords or [],
            "raw": {
                "id": email.email_id,
                "threadId": email.thread_id or f"{email.email_id}-thread",
            },
        }

    def _get_local_message(self, message_id: str) -> Optional[Dict]:
        if not self._local_demo_enabled():
            return None
        try:
            email = EmailResult.objects.get(user=self.user, email_id=message_id)
        except EmailResult.DoesNotExist:
            return None
        return self._local_email_to_message(email)

    def _get_local_thread(self, thread_id: str, limit: int) -> Optional[List[Dict]]:
        if not self._local_demo_enabled():
            return None
        if not thread_id:
            return []

        emails = list(
            EmailResult.objects.filter(user=self.user, thread_id=thread_id).order_by("parsed_date")
        )
        if not emails:
            # Fallback: try to infer from single message whose thread_id is blank.
            lookup_id = thread_id
            if thread_id.endswith("-thread"):
                lookup_id = thread_id[: -len("-thread")]
            single = EmailResult.objects.filter(user=self.user, email_id=lookup_id).first()
            if not single:
                return []
            emails = [single]

        messages = [self._local_email_to_message(email) for email in emails]
        if limit and len(messages) > limit:
            return messages[-limit:]
        return messages
    
    def _get_social_token(self) -> Optional[SocialToken]:
        """Get the user's Google social token from django-allauth."""
        try:
            # Try to get the SocialApp
            try:
                google_app = SocialApp.objects.get(provider='google')
            except SocialApp.DoesNotExist:
                logger.error(f"No Google SocialApp found - OAuth may be configured in settings only")
                return None
            
            # Try to get the token
            return SocialToken.objects.get(
                account__user=self.user,
                account__provider='google',
                app=google_app
            )
        except SocialToken.DoesNotExist:
            logger.error(f"No Google social token found for user {self.user.username}")
            return None
    
    def _refresh_token_if_needed(self, credentials: Credentials) -> Credentials:
        """Refresh OAuth token if it's expired or about to expire."""
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                try:
                    credentials.refresh(Request())
                    # Update the stored token
                    self._store_refreshed_token(credentials)
                    logger.info(f"Token refreshed for user {self.user.username}")
                except Exception as e:
                    logger.error(f"Token refresh failed for user {self.user.username}: {str(e)}")
                    self._invalidate_tokens()
                    raise GmailAPIError("Authentication failed. Please re-authenticate.")
            else:
                # Without a refresh token we cannot recover, so force re-auth
                self._invalidate_tokens()
                raise GmailAPIError("Authentication required. Please re-authenticate.")
        return credentials
    
    def _store_refreshed_token(self, credentials: Credentials):
        """Store refreshed token securely."""
        try:
            token_storage, created = EncryptedTokenStorage.objects.get_or_create(
                user=self.user
            )
            if credentials.token:
                token_storage.set_access_token(credentials.token)
            if credentials.refresh_token:
                token_storage.set_refresh_token(credentials.refresh_token)
            if credentials.expiry:
                # Convert naive UTC datetime to timezone-aware datetime for Django
                if credentials.expiry.tzinfo is None:
                    token_storage.token_expires_at = timezone.make_aware(credentials.expiry, timezone.utc)
                else:
                    token_storage.token_expires_at = credentials.expiry
            token_storage.save()
        except Exception as e:
            logger.error(f"Failed to store refreshed token for user {self.user.username}: {str(e)}")
    
    def _build_credentials(self) -> Optional[Credentials]:
        """Build Google OAuth2 credentials from stored tokens."""
        social_token = self._get_social_token()
        
        # Try to get tokens from EncryptedTokenStorage as fallback
        token_storage = None
        access_token = None
        refresh_token = None
        expires_at = None
        
        try:
            token_storage = EncryptedTokenStorage.objects.get(user=self.user)
            access_token = token_storage.get_access_token()
            refresh_token = token_storage.get_refresh_token()
            expires_at = token_storage.token_expires_at
            logger.debug(f"Found EncryptedTokenStorage for user {self.user.username}")
        except EncryptedTokenStorage.DoesNotExist:
            logger.debug(f"No EncryptedTokenStorage found for user {self.user.username}")
        except InvalidToken:
            logger.warning(
                "Encrypted tokens invalid for user %s; clearing stored credentials",
                self.user.username,
            )
            if token_storage:
                try:
                    token_storage.delete()
                except Exception as exc:
                    logger.error(
                        "Failed to delete invalid token storage for user %s: %s",
                        self.user.username,
                        exc,
                    )
            # Do not invalidate SocialToken here — the encrypted cache can be stale
            # if ENCRYPTION_KEY changed, but the allauth token may still be usable.
            access_token = None
            refresh_token = None
            expires_at = None
        
        # If we have SocialToken, prefer its access token (more likely to be fresh)
        # but use EncryptedTokenStorage's refresh token as fallback
        if social_token:
            access_token = social_token.token
            if not refresh_token:
                refresh_token = social_token.token_secret
            if not expires_at:
                expires_at = social_token.expires_at
            logger.debug(f"Using SocialToken for user {self.user.username}")
        elif not access_token:
            # No tokens available at all
            logger.error(f"No credentials available for user {self.user.username}")
            return None
        
        # Get OAuth credentials from database SocialApp
        try:
            google_app = SocialApp.objects.get(provider='google')
        except SocialApp.DoesNotExist:
            logger.error(f"No Google SocialApp configured")
            raise GmailAPIError("Google OAuth is not properly configured. Please contact support.")
        
        # Build credentials
        credentials = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=google_app.client_id,
            client_secret=google_app.secret,
            scopes=settings.SOCIALACCOUNT_PROVIDERS['google']['SCOPE']
        )
        
        if expires_at:
            # Convert timezone-aware datetime to UTC naive datetime for Google credentials
            if timezone.is_aware(expires_at):
                credentials.expiry = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                credentials.expiry = expires_at
        
        logger.info(f"Built credentials for user {self.user.username} (has_refresh={bool(refresh_token)})")
        return credentials

    def build_authorized_credentials(self) -> Credentials:
        """
        Build and refresh credentials so other integrations can re-use the
        Google OAuth session.
        """
        credentials = self._build_credentials()
        if not credentials:
            logger.warning("No Google credentials found for user %s", self.user.username)
            raise GmailAPIError("Google authentication is required. Please reconnect your account.")

        credentials = self._refresh_token_if_needed(credentials)
        self.credentials = credentials
        return credentials
    
    def authenticate(self) -> bool:
        """Authenticate and build Gmail service."""
        try:
            self.credentials = self.build_authorized_credentials()
            
            # Build Gmail service
            self.service = build(
                settings.GMAIL_API_SERVICE_NAME,
                settings.GMAIL_API_VERSION,
                credentials=self.credentials
            )
            
            # Test the connection by making a simple API call
            try:
                profile = self.service.users().getProfile(userId='me').execute()
                logger.info(f"Gmail service authenticated for user {self.user.username}, email: {profile.get('emailAddress')}")
                return True
            except HttpError as api_error:
                self._handle_http_error(api_error)
                return False
            except Exception as api_error:
                logger.error(f"Gmail API test call failed for user {self.user.username}: {str(api_error)}")
                return False

        except GmailAPIError:
            raise
        except Exception as e:
            logger.error(
                "Gmail authentication failed for user %s: %s",
                self.user.username,
                e,
                exc_info=True,
            )
            return False

    def _handle_http_error(self, api_error: HttpError) -> None:
        """Handle known Gmail API errors and take corrective action."""
        status = getattr(api_error, 'resp', None)
        status_code = status.status if status else None

        if status_code == 401:
            logger.warning(
                "Gmail API returned 401 for user %s; invalidating tokens",
                self.user.username,
            )
            self._invalidate_tokens()
            raise GmailAPIError("Authentication required. Please re-authenticate.")

        if status_code == 403:
            try:
                error_payload = json.loads(api_error.content.decode('utf-8'))
            except Exception:
                error_payload = {}

            reasons = []
            errors = error_payload.get('error', {}).get('errors', [])
            for error in errors:
                reason = error.get('reason')
                if reason:
                    reasons.append(reason)

            if 'insufficientPermissions' in reasons:
                logger.warning(f"Insufficient Gmail scopes for user {self.user.username}; invalidating tokens to force re-authentication")
                self._invalidate_tokens()
                raise GmailAPIError("Additional Gmail permissions are required. Please reconnect your Google account.")

        logger.error(f"Gmail API test call failed for user {self.user.username}: {api_error}")

    def _invalidate_tokens(self) -> None:
        """Remove stored tokens so the user is prompted to re-authorize."""
        try:
            social_app = SocialApp.objects.get(provider='google')
        except SocialApp.DoesNotExist:
            logger.warning("Attempted to invalidate tokens but no Google SocialApp is configured")
            social_app = None

        try:
            tokens_qs = SocialToken.objects.filter(
                account__user=self.user,
                account__provider='google',
            )
            if social_app:
                tokens_qs = tokens_qs.filter(app=social_app)
            deleted_tokens = tokens_qs.delete()
            if deleted_tokens[0]:
                logger.info(
                    "Removed %s social tokens for user %s",
                    deleted_tokens[0],
                    self.user.username,
                )
        except Exception as exc:
            logger.error(
                "Failed to remove social tokens for user %s: %s",
                self.user.username,
                exc,
            )

        try:
            deleted_storage = EncryptedTokenStorage.objects.filter(user=self.user).delete()
            if deleted_storage[0]:
                logger.info(
                    "Removed encrypted token storage for user %s",
                    self.user.username,
                )
        except Exception as exc:
            logger.error(
                "Failed to remove encrypted token storage for user %s: %s",
                self.user.username,
                exc,
            )
    
    def _build_search_query(self, filters: Dict) -> str:
        """Build Gmail search query from filters.

        Notes:
            - By default we restrict to unread messages (is:unread).
            - You can override by passing unread_only=False in filters.
            - Optionally restrict to inbox with inbox_only=True.
        """
        query_parts = []

        # Keywords
        if filters.get('keywords'):
            if isinstance(filters['keywords'], list):
                # Multiple keywords - use OR to find emails containing any of them
                keyword_query = ' OR '.join([f'"{keyword}"' for keyword in filters['keywords']])
                query_parts.append(f"({keyword_query})")
            else:
                query_parts.append(filters['keywords'])

        # Common filters
        if filters.get('sender'):
            query_parts.append(f"from:{filters['sender']}")

        if filters.get('subject'):
            query_parts.append(f"subject:{filters['subject']}")

        if filters.get('date_from'):
            query_parts.append(f"after:{filters['date_from']}")

        if filters.get('date_to'):
            query_parts.append(f"before:{filters['date_to']}")

        # Unread only (default True)
        if filters.get('unread_only', True):
            query_parts.append('is:unread')

        # Restrict to inbox if explicitly requested
        if filters.get('inbox_only'):
            query_parts.append('in:inbox')

        if filters.get('primary_only'):
            query_parts.append('category:primary')

        # If nothing else specified, default to unread across mailbox
        return ' '.join(query_parts) if query_parts else 'is:unread'
    
    def fetch_email_batch(
        self,
        *,
        keywords: List[str],
        include_read: bool,
        primary_only: bool,
        limit: int,
        page_token: Optional[str] = None,
    ) -> Tuple[List[Dict], Optional[str], int]:
        """Fetch a single batch of emails and return (emails, next_token, total_estimate)."""

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        filters = {
            'keywords': keywords,
            'unread_only': not include_read,
            'inbox_only': True,
            'primary_only': primary_only,
        }
        query = self._build_search_query(filters)
        logger.debug(
            "Email batch query for user %s: %s (limit=%s, token=%s)",
            self.user.username,
            query,
            limit,
            page_token,
        )

        batch_size = min(max(limit, 1), 50)

        response = self.service.users().messages().list(
            userId='me',
            q=query,
            maxResults=batch_size,
            pageToken=page_token,
        ).execute()

        messages = response.get('messages', []) or []
        total_estimate = response.get('resultSizeEstimate', 0)

        try:
            label_counts = self._get_label_stats('INBOX')
            total_estimate = (
                label_counts['messages_total']
                if include_read
                else label_counts['messages_unread']
            )
        except GmailAPIError:
            logger.debug(
                "Falling back to Gmail estimate for user %s", self.user.username
            )
        next_token = response.get('nextPageToken')

        emails: List[Dict] = []

        for msg in messages:
            if len(emails) >= limit:
                break
            try:
                email_data = self._get_email_details(msg['id'])
                if email_data:
                    email_data['matched_keywords'] = self._analyze_keyword_matches(
                        email_data, keywords
                    )
                    email_data['keyword_match_count'] = len(email_data['matched_keywords'])
                    emails.append(email_data)
            except Exception as exc:
                logger.warning(
                    "Failed to get details for message %s: %s",
                    msg.get('id'),
                    exc,
                )
                continue

        return emails, next_token, total_estimate

    def search_messages(self, query: str, *, limit: int = 20) -> List[Dict]:
        """Search Gmail using a raw query string and return parsed email details.

        Unlike ``fetch_email_batch``, this does not default to unread-only and does not
        force ``in:inbox``. The caller controls the query entirely.
        """
        cleaned = (query or "").strip()
        if not cleaned:
            return []

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        batch_size = min(max(int(limit or 0), 1), 100)

        try:
            response = self.service.users().messages().list(
                userId="me",
                q=cleaned,
                maxResults=batch_size,
            ).execute()
        except HttpError as error:
            self._handle_http_error(error)
            raise GmailAPIError("Unable to search Gmail right now. Please try again.")

        messages = response.get("messages", []) or []
        results: List[Dict] = []
        for msg in messages[:batch_size]:
            message_id = (msg or {}).get("id")
            if not message_id:
                continue
            try:
                email_data = self._get_email_details(message_id)
            except Exception as exc:
                logger.warning(
                    "Failed to get details for search message %s: %s",
                    message_id,
                    exc,
                )
                continue
            if email_data:
                results.append(email_data)

        return results
    
    def _analyze_keyword_matches(self, email_data: Dict, keywords: List[str]) -> List[str]:
        """Analyze which keywords match in the email subject, snippet, or body."""
        matched_keywords = []
        subject_lower = email_data.get('subject', '').lower()
        snippet_lower = email_data.get('snippet', '').lower()
        body_lower = email_data.get('body_text', '').lower()

        for keyword in keywords:
            keyword_lower = keyword.lower()
            if (
                keyword_lower in subject_lower
                or keyword_lower in snippet_lower
                or keyword_lower in body_lower
            ):
                matched_keywords.append(keyword)

        return matched_keywords

    def _extract_plain_text_body(self, payload: Dict) -> str:
        """Extract a readable plain text body from a Gmail message payload."""
        if not payload:
            return ''

        parts: List[str] = []
        mime_type = (payload.get('mimeType') or '').lower()
        body = payload.get('body', {}) or {}
        data = body.get('data')

        if data:
            decoded = self._decode_body_data(data)
            if mime_type == 'text/html':
                parts.append(self._strip_html_tags(decoded))
            else:
                parts.append(decoded)

        for part in payload.get('parts', []) or []:
            part_text = self._extract_plain_text_body(part)
            if part_text:
                parts.append(part_text)

        combined = ' '.join(filter(None, parts))
        return re.sub(r'\s+', ' ', combined).strip()

    def _decode_body_data(self, data: str) -> str:
        """Decode Gmail message body data from base64url to text."""
        if not data:
            return ''

        padding = '=' * (-len(data) % 4)
        try:
            decoded_bytes = base64.urlsafe_b64decode(data + padding)
            return decoded_bytes.decode('utf-8', errors='replace')
        except Exception as exc:
            logger.debug("Failed to decode Gmail body data: %s", exc)
            return ''

    def _strip_html_tags(self, value: str) -> str:
        """Convert basic HTML content to plain text for keyword analysis."""
        if not value:
            return ''

        no_script = re.sub(r'(?is)<(script|style).*?>.*?(</\1>)', '', value)
        text_only = re.sub(r'(?s)<[^>]*>', ' ', no_script)
        unescaped = unescape(text_only)
        return re.sub(r'\s+', ' ', unescaped)

    def _collect_attachments(self, payload: Dict, message_id: str) -> List[Dict]:
        """Traverse payload tree to gather attachment metadata."""
        attachments: List[Dict] = []

        def _walk(part: Optional[Dict]) -> None:
            if not part:
                return
            body = part.get('body', {}) or {}
            filename = (part.get('filename') or '').strip()
            attachment_id = body.get('attachmentId')
            mime_type = part.get('mimeType') or 'application/octet-stream'
            if filename and attachment_id:
                attachments.append({
                    'message_id': message_id,
                    'attachment_id': attachment_id,
                    'filename': filename,
                    'mimeType': mime_type,
                    'size': body.get('size', 0),
                })
            for child in part.get('parts', []) or []:
                _walk(child)

        _walk(payload)
        return attachments

    def _parse_message(self, message: Dict) -> Dict:
        """Normalize Gmail message payload into application structure."""
        payload = message.get('payload', {}) or {}
        headers = payload.get('headers', []) or []
        message_id = message.get('id')

        def header(name: str) -> str:
            return self._get_header_value(headers, name)

        subject = header('Subject')
        date_header = header('Date')

        parsed = {
            'id': message_id,
            'thread_id': message.get('threadId'),
            'history_id': message.get('historyId'),
            'snippet': message.get('snippet', ''),
            'labels': message.get('labelIds', []),
            'subject': subject,
            'date': date_header,
            'parsed_date': self._parse_email_date(date_header),
            'sender': header('From'),
            'reply_to': header('Reply-To'),
            'to': header('To'),
            'cc': header('Cc'),
            'bcc': header('Bcc'),
            'body_text': self._extract_plain_text_body(payload),
            'message_id_header': header('Message-ID'),
            'references': header('References'),
            'in_reply_to': header('In-Reply-To'),
            'attachments': self._collect_attachments(payload, message_id),
            'payload': payload,
            'headers': headers,
        }

        return parsed

    def _get_email_details(self, message_id: str) -> Optional[Dict]:
        """Get detailed information for a specific email."""
        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            parsed = self._parse_message(message)
            return parsed
        except Exception as e:
            logger.error(f"Failed to get email details for message {message_id}: {str(e)}")
            return None
    
    def _get_header_value(self, headers: List[Dict], name: str) -> str:
        """Extract header value by name."""
        for header in headers:
            if header.get('name', '').lower() == name.lower():
                value = header.get('value', '')
                # Clean sender field to extract just the email address
                if name.lower() == 'from':
                    return self._clean_sender_address(value)
                return value
        return ''
    
    def _clean_sender_address(self, sender: str) -> str:
        """Extract and clean email address from sender field."""
        import re
        if not sender:
            return ''
        
        # Extract email from "Name <email@domain.com>" format
        email_match = re.search(r'<([^>]+)>', sender)
        if email_match:
            return email_match.group(1).strip()
        
        # If no angle brackets, check if it's already just an email
        if '@' in sender and '<' not in sender:
            return sender.strip()
        
        # Fallback: return original sender
        return sender.strip()
    
    def _parse_email_date(self, date_str: str) -> Optional[datetime]:
        """Parse email date string to datetime object."""
        if not date_str:
            return None
        
        try:
            # Gmail date format examples:
            # "Mon, 1 Jan 2024 12:00:00 +0000"
            # "1 Jan 2024 12:00:00 +0000"
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(date_str)
        except Exception as e:
            logger.warning(f"Failed to parse date '{date_str}': {str(e)}")
            return None
    
    def get_user_profile(self) -> Optional[Dict]:
        """Get user's Gmail profile information."""
        if not self.service:
            if not self.authenticate():
                return None

        try:
            profile = self.service.users().getProfile(userId='me').execute()
            return {
                'email': profile.get('emailAddress'),
                'messages_total': profile.get('messagesTotal', 0),
                'threads_total': profile.get('threadsTotal', 0),
                'history_id': profile.get('historyId')
            }
        except Exception as e:
            logger.error(f"Failed to get user profile for {self.user.username}: {str(e)}")
            return None

    def _get_label_stats(self, label_id: str) -> Dict[str, int]:
        """Fetch Gmail label stats and return total and unread counts."""
        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        try:
            label = self.service.users().labels().get(userId='me', id=label_id).execute()
            return {
                'messages_total': int(label.get('messagesTotal', 0) or 0),
                'messages_unread': int(label.get('messagesUnread', 0) or 0),
            }
        except HttpError as error:
            logger.error(
                "Failed to fetch label %s stats for %s: %s",
                label_id,
                self.user.username,
                error,
            )
            raise GmailAPIError("Unable to fetch label stats") from error
        except Exception as error:
            logger.error(
                "Unexpected error retrieving label %s stats for %s: %s",
                label_id,
                self.user.username,
                error,
            )
            raise GmailAPIError("Unable to fetch label stats") from error

    def get_unread_count(self) -> int:
        """Return the number of unread messages in the user's inbox."""
        label_counts = self._get_label_stats('INBOX')
        return label_counts['messages_unread']

    def ensure_watch(self, profile) -> None:
        """Ensure a Gmail watch is active for this user."""
        topic_name = getattr(settings, 'GMAIL_PUBSUB_TOPIC', '')
        if not topic_name:
            logger.debug("Gmail watch skipped: GMAIL_PUBSUB_TOPIC not configured")
            return

        needs_watch = False
        now = timezone.now()
        if not profile.gmail_history_id:
            needs_watch = True
        elif not profile.gmail_watch_expires_at or profile.gmail_watch_expires_at <= now:
            needs_watch = True

        if not needs_watch:
            return

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed while registering watch")

        body = {
            'topicName': topic_name,
            'labelIds': ['INBOX'],
        }

        try:
            response = self.service.users().watch(userId='me', body=body).execute()
            logger.info("Registered Gmail watch for user %s", self.user.username)
        except HttpError as error:
            logger.error("Failed to register Gmail watch for %s: %s", self.user.username, error)
            raise GmailAPIError("Unable to register Gmail watch")

        history_id = response.get('historyId')
        expiration = response.get('expiration')
        expires_at = None
        if expiration:
            try:
                expires_at = timezone.datetime.fromtimestamp(int(expiration) / 1000, tz=timezone.utc)
            except Exception:
                expires_at = None

        profile.gmail_history_id = history_id or profile.gmail_history_id
        profile.gmail_watch_expires_at = expires_at
        profile.save(update_fields=['gmail_history_id', 'gmail_watch_expires_at', 'updated_at'])

    def fetch_history(self, start_history_id: str):
        """Fetch history records since the provided ID."""
        if not start_history_id:
            return [], None

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed fetching history")

        messages = []
        page_token = None
        latest_history_id = start_history_id

        try:
            while True:
                response = self.service.users().history().list(
                    userId='me',
                    startHistoryId=start_history_id,
                    historyTypes=['messageAdded'],
                    pageToken=page_token,
                ).execute()

                history = response.get('history', [])
                for record in history:
                    latest_history_id = record.get('id', latest_history_id)
                    for added in record.get('messagesAdded', []):
                        messages.append(added.get('message', {}))

                page_token = response.get('nextPageToken')
                if not page_token:
                    break
        except HttpError as error:
            logger.error("History fetch failed for user %s: %s", self.user.username, error)
            raise GmailAPIError("Unable to fetch mailbox history")

        return messages, latest_history_id

    def get_message(self, message_id: str) -> Dict:
        """Return full metadata for a Gmail message."""
        local_message = self._get_local_message(message_id)
        if local_message is not None:
            return local_message

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        try:
            message = self.service.users().messages().get(
                userId='me',
                id=message_id,
                format='full'
            ).execute()
            parsed = self._parse_message(message)
            parsed['thread_id'] = message.get('threadId')
            parsed['raw'] = message
            return parsed
        except HttpError as error:
            logger.error("Failed to fetch message %s for user %s: %s", message_id, self.user.username, error)
            raise GmailAPIError("Unable to load message details")

    def get_thread_messages(self, thread_id: str, limit: int = 20) -> List[Dict]:
        """Fetch messages belonging to a thread for context display."""
        if not thread_id:
            return []

        local_thread = self._get_local_thread(thread_id, limit)
        if local_thread is not None:
            return local_thread

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        try:
            thread = self.service.users().threads().get(
                userId='me',
                id=thread_id,
                format='full'
            ).execute()
        except HttpError as error:
            logger.error("Failed to fetch thread %s for user %s: %s", thread_id, self.user.username, error)
            raise GmailAPIError("Unable to load conversation")

        messages = [self._parse_message(msg) for msg in thread.get('messages', [])]
        if limit and len(messages) > limit:
            return messages[-limit:]
        return messages

    # --- Enquiry follow-up conversation matching -------------------------------------------

    def own_addresses(self) -> set:
        """Email addresses that belong to the signed-in user (used to tell customers apart)."""
        addresses = set()
        user_email = (getattr(self.user, 'email', '') or '').strip().lower()
        if user_email:
            addresses.add(user_email)
        try:
            profile = self.get_user_profile() or {}
        except Exception:
            profile = {}
        profile_email = (profile.get('email') or '').strip().lower()
        if profile_email:
            addresses.add(profile_email)
        return addresses

    def describe_conversation(self, thread_id: str, own: Optional[set] = None) -> Optional[Dict]:
        """Summarise a Gmail conversation for an enquiry follow-up.

        Returns the customer's address (the first participant who is not the signed-in user),
        the latest message to reply to, and display details. Returns None if the thread is empty.
        """
        from email.utils import getaddresses, parseaddr

        messages = self.get_thread_messages(thread_id, limit=50)
        if not messages:
            return None
        own = {a.lower() for a in (own if own is not None else self.own_addresses())}

        customer_email = ''
        for msg in messages:
            address = parseaddr(msg.get('reply_to') or msg.get('sender') or '')[1].strip().lower()
            if address and address not in own:
                customer_email = address
                break
        if not customer_email:
            # Every message is ours (e.g. we wrote first): use the first outside recipient.
            for msg in messages:
                for _, address in getaddresses([msg.get('to') or '']):
                    address = address.strip().lower()
                    if address and address not in own:
                        customer_email = address
                        break
                if customer_email:
                    break

        first, latest = messages[0], messages[-1]
        return {
            'thread_id': thread_id,
            'subject': first.get('subject') or latest.get('subject') or '',
            'customer_email': customer_email,
            'latest_message_id': latest.get('id'),
            'last_date': latest.get('parsed_date'),
            'message_count': len(messages),
            'snippet': (latest.get('snippet') or '')[:160],
        }

    def find_enquiry_conversations(
        self,
        *,
        subject: str,
        customer_email: str = '',
        customer_name: str = '',
        max_threads: int = 5,
    ) -> List[Dict]:
        """Find Gmail conversations that may belong to an enquiry, one entry per thread.

        Searches by customer email (sent or received) plus subject when an email is known,
        otherwise by subject plus customer name. The caller decides whether a single result
        is confident enough; this method never picks one.
        """
        subject = (subject or '').replace('"', '').strip()
        customer_email = (customer_email or '').replace('"', '').strip()
        customer_name = (customer_name or '').replace('"', '').strip()
        if not subject and not customer_email:
            return []

        local = self._find_local_demo_conversations(subject, customer_email, max_threads)
        if local:
            return local

        terms = []
        if customer_email:
            terms.append(f'(from:{customer_email} OR to:{customer_email})')
        if subject:
            terms.append(f'subject:"{subject}"')
        if not customer_email and customer_name and customer_name.lower() not in subject.lower():
            terms.append(f'"{customer_name}"')

        matches = self.search_messages(' '.join(terms), limit=25)
        thread_ids: List[str] = []
        for match in matches:
            thread_id = (match or {}).get('thread_id')
            if thread_id and thread_id not in thread_ids:
                thread_ids.append(thread_id)

        own = self.own_addresses()
        conversations = []
        for thread_id in thread_ids[:max_threads]:
            summary = self.describe_conversation(thread_id, own=own)
            if not summary:
                continue
            if customer_email:
                # The search required this address as sender or recipient, so it is the customer.
                summary['customer_email'] = customer_email.lower()
            conversations.append(summary)
        return conversations

    def _find_local_demo_conversations(self, subject: str, customer_email: str, max_threads: int) -> List[Dict]:
        """Local development only: match the built-in "demo-" sample emails instead of searching Gmail,
        so the follow-up page can be opened without a connected Google account."""
        if not self._local_demo_enabled():
            return []
        from django.db.models import Q

        match = Q()
        if subject:
            match |= Q(subject__icontains=subject)
        if customer_email:
            match |= Q(sender__icontains=customer_email)
        rows = EmailResult.objects.filter(match, user=self.user, email_id__startswith="demo-").order_by("-parsed_date")

        thread_ids: List[str] = []
        for row in rows:
            thread_id = row.thread_id or f"{row.email_id}-thread"
            if thread_id not in thread_ids:
                thread_ids.append(thread_id)

        own = {(getattr(self.user, 'email', '') or '').strip().lower()} - {''}
        conversations = []
        for thread_id in thread_ids[:max_threads]:
            summary = self.describe_conversation(thread_id, own=own)
            if not summary:
                continue
            if customer_email:
                summary['customer_email'] = customer_email.lower()
            conversations.append(summary)
        return conversations

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Retrieve attachment binary data from Gmail."""
        local_content = self._get_local_attachment_content(message_id, attachment_id)
        if local_content is not None:
            return local_content

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        try:
            attachment = self.service.users().messages().attachments().get(
                userId='me',
                messageId=message_id,
                id=attachment_id,
            ).execute()
        except HttpError as error:
            logger.error(
                "Failed to download attachment %s for message %s (user %s): %s",
                attachment_id,
                message_id,
                self.user.username,
                error,
            )
            raise GmailAPIError("Unable to download attachment")

        data = attachment.get('data')
        if not data:
            return b''

        padding = '=' * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)

    def send_reply(
        self,
        original_message: Dict,
        body: str,
        attachments: Optional[List[Dict]] = None,
        subject: Optional[str] = None,
        to: Optional[str] = None,
        cc: Optional[List[str]] = None,
        html_body: Optional[str] = None,
    ) -> Dict:
        """Send a reply email referencing an existing message.

        ``to`` overrides the recipient; otherwise the reply goes to the original sender.
        ``cc`` is an optional list of addresses copied on the email.
        ``html_body`` adds an HTML version (the plain-text ``body`` stays as the alternative).
        """
        original_id = str(original_message.get('id') or '')
        if original_id.startswith('demo-') and self._local_demo_enabled():
            # Local development: never email anyone from the built-in sample data.
            # Add our reply to the local demo thread, as Gmail would, so the conversation stays realistic.
            now = timezone.now()
            fake_id = f"demo-sent-{now:%Y%m%d%H%M%S%f}"
            thread_id = original_message.get('thread_id') or f"{original_id}-thread"
            logger.info("Local demo: not sending reply to %s (cc %s) for %s", to or original_message.get('sender'),
                        ", ".join(cc or []) or "none", original_id)
            EmailResult.objects.create(
                user=self.user,
                email_id=fake_id,
                sender=(getattr(self.user, 'email', '') or 'demo@example.com'),
                subject=subject or original_message.get('subject') or '',
                snippet=(body or '')[:200],
                date=now.strftime("%a, %d %b %Y %H:%M:%S %z"),
                parsed_date=now,
                thread_id=thread_id,
                message_id_header=f"<{fake_id}@magicwand.local>",
                labels=["SENT"],
                is_done=True,
            )
            return {'id': fake_id, 'threadId': thread_id, 'local_demo': True}

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        reply_to = (
            (to or '').strip()
            or original_message.get('reply_to')
            or original_message.get('sender')
            or original_message.get('from')
        )
        if not reply_to:
            raise GmailAPIError("Original message does not contain a Reply-To address")

        subject_line = subject or (original_message.get('subject') or '')
        if subject_line and not subject_line.lower().startswith('re:'):
            subject_line = f"Re: {subject_line}"

        email_message = EmailMessage()
        email_message['To'] = reply_to
        if cc:
            email_message['Cc'] = ", ".join(cc)
        email_message['Subject'] = subject_line

        message_id_header = original_message.get('message_id_header')
        if message_id_header:
            email_message['In-Reply-To'] = message_id_header

        references = original_message.get('references') or ''
        reference_headers = ' '.join(
            reference for reference in [references.strip(), message_id_header] if reference
        )
        if reference_headers:
            email_message['References'] = reference_headers

        email_message.set_content(body)
        if html_body:
            email_message.add_alternative(html_body, subtype='html')

        for attachment in attachments or []:
            filename = attachment.get('filename') or 'attachment'
            mime_type = attachment.get('mime_type') or 'application/octet-stream'
            maintype, subtype = mime_type.split('/', 1) if '/' in mime_type else ('application', 'octet-stream')
            email_message.add_attachment(
                attachment.get('content', b''),
                maintype=maintype,
                subtype=subtype,
                filename=filename,
            )

        email_message['Message-ID'] = make_msgid(domain=None)

        raw_message = base64.urlsafe_b64encode(email_message.as_bytes()).decode('utf-8')

        send_body = {'raw': raw_message}
        thread_id = original_message.get('thread_id')
        if thread_id:
            send_body['threadId'] = thread_id

        try:
            sent = self.service.users().messages().send(userId='me', body=send_body).execute()
            return sent
        except HttpError as error:
            logger.error("Failed to send reply for user %s: %s", self.user.username, error)
            raise GmailAPIError("Unable to send reply")

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        cc: Optional[List[str]] = None,
        attachments: Optional[List[Dict]] = None,
        html_body: Optional[str] = None,
    ) -> Dict:
        """Send a new email (not a reply) with optional CC and attachments.

        Attachments are dicts with ``filename``, ``mime_type`` and ``content`` (bytes).
        ``html_body`` adds an HTML version (the plain-text ``body`` stays as the alternative).
        In local development nothing is sent; the email is only logged.
        """
        if self._local_demo_enabled():
            logger.info("Local demo: not sending email to %s (cc %s), subject %r, %d attachment(s)",
                        to, ", ".join(cc or []) or "none", subject, len(attachments or []))
            return {'id': f"demo-sent-{timezone.now():%Y%m%d%H%M%S%f}", 'local_demo': True}

        if not self.service:
            if not self.authenticate():
                raise GmailAPIError("Authentication failed")

        email_message = EmailMessage()
        email_message['To'] = to
        if cc:
            email_message['Cc'] = ", ".join(cc)
        email_message['Subject'] = subject
        email_message.set_content(body)
        if html_body:
            email_message.add_alternative(html_body, subtype='html')
        for attachment in attachments or []:
            mime_type = attachment.get('mime_type') or 'application/octet-stream'
            maintype, subtype = mime_type.split('/', 1) if '/' in mime_type else ('application', 'octet-stream')
            email_message.add_attachment(
                attachment.get('content', b''),
                maintype=maintype,
                subtype=subtype,
                filename=attachment.get('filename') or 'attachment',
            )
        email_message['Message-ID'] = make_msgid(domain=None)
        raw_message = base64.urlsafe_b64encode(email_message.as_bytes()).decode('utf-8')
        try:
            return self.service.users().messages().send(userId='me', body={'raw': raw_message}).execute()
        except HttpError as error:
            logger.error("Failed to send email for user %s: %s", self.user.username, error)
            raise GmailAPIError("Unable to send the email")

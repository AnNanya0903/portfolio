"""Security helpers for KYC by shareable link: tokens, OTPs, field encryption, rate limits, uploads, client IP.

Nothing here touches the database except through the arguments it is given.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import time
import uuid
from functools import wraps
from typing import Callable, Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse

# ----- Tokens and OTPs -------------------------------------------------------------------------------------------


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def same_hash(a: str, b: str) -> bool:
    """Constant-time comparison of two hex hashes."""
    return hmac.compare_digest(str(a or ""), str(b or ""))


def new_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(request_id: int, code: str) -> str:
    """The OTP hash is keyed with SECRET_KEY and the request, so a leaked table cannot be brute-forced offline
    across requests (there are only a million codes)."""
    message = f"{request_id}:{str(code or '').strip()}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


# ----- Field encryption (PAN, bank account number) ---------------------------------------------------------------


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    raw = [k.strip() for k in str(getattr(settings, "KYC_FIELD_ENCRYPTION_KEYS", "") or "").split(",") if k.strip()]
    if not raw:
        if not getattr(settings, "DEBUG", False):
            raise ImproperlyConfigured("KYC_FIELD_ENCRYPTION_KEYS must be set to store PAN and bank account numbers.")
        # Local development only: a key derived from SECRET_KEY, so a fresh checkout works without setup.
        raw = [base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()).decode()]
    return MultiFernet([Fernet(k.encode("utf-8")) for k in raw])


def encrypt(value: str) -> str:
    value = str(value or "")
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8") if token else ""


def mask(last4: str, length: int = 10) -> str:
    """XXXXXX1234: only the last four characters are shown."""
    last4 = str(last4 or "")
    if not last4:
        return ""
    return "X" * max(length - len(last4), 2) + last4


# ----- Client IP and rate limits ----------------------------------------------------------------------------------


def client_ip(request) -> Optional[str]:
    """App Engine sets X-Appengine-User-Ip itself and strips it from incoming requests, so it can be trusted;
    X-Forwarded-For can be typed by anyone and is not used."""
    ip = (request.META.get("HTTP_X_APPENGINE_USER_IP") or request.META.get("REMOTE_ADDR") or "").strip()
    return ip or None


def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit for `key`. True while the count in the current window is within `limit`.

    Uses Django's cache. With the default per-process cache each App Engine instance counts separately; set a
    shared CACHES backend to make the limits exact. The OTP limits that matter are also enforced in the database.
    """
    bucket = int(time.time() // window_seconds)
    cache_key = f"kyc-rl:{key}:{bucket}"
    if cache.add(cache_key, 1, window_seconds + 5):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:  # expired between add and incr
        cache.add(cache_key, 1, window_seconds + 5)
        return True
    return count <= limit


def rate_limited(scope: str, limit: int, window_seconds: int, per_token: bool = True) -> Callable:
    """Decorator for the public views: `limit` requests per `window_seconds` per IP (and per link)."""

    def decorator(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            parts = [scope, client_ip(request) or "unknown"]
            if per_token and kwargs.get("token"):
                parts.append(hash_token(kwargs["token"])[:16])
            if not hit(":".join(parts), limit, window_seconds):
                return JsonResponse({"error": "rate_limited",
                                     "message": "Too many attempts. Please wait a few minutes and try again."},
                                    status=429)
            return view(request, *args, **kwargs)

        return _wrapped

    return decorator


# ----- Uploads ------------------------------------------------------------------------------------------------------

ALLOWED_TYPES = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}


class UploadError(ValueError):
    pass


def sniff_mime(head: bytes) -> str:
    """The real type from the first bytes of the file, whatever its name or the browser says."""
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return ""


def max_upload_bytes() -> int:
    return int(getattr(settings, "KYC_UPLOAD_MAX_BYTES", 5 * 1024 * 1024) or 5 * 1024 * 1024)


def check_upload(uploaded) -> Tuple[bytes, str]:
    """(content, mime type) for an allowed file, else UploadError with a message for the customer."""
    if uploaded is None:
        raise UploadError("Choose a file to upload.")
    limit = max_upload_bytes()
    if uploaded.size is not None and uploaded.size > limit:
        raise UploadError(f"The file is larger than {limit // (1024 * 1024)} MB.")
    content = uploaded.read(limit + 1)
    if len(content) > limit:
        raise UploadError(f"The file is larger than {limit // (1024 * 1024)} MB.")
    if not content:
        raise UploadError("The file is empty.")
    mime = sniff_mime(content[:16])
    if mime not in ALLOWED_TYPES:
        raise UploadError("Only PDF, JPG and PNG files can be uploaded.")
    if mime.startswith("image/"):
        try:
            from PIL import Image

            Image.open(io.BytesIO(content)).verify()
        except ImportError:
            pass
        except Exception as exc:
            raise UploadError("The image could not be read. Please upload another copy.") from exc
    return content, mime


def storage_key_for(mime: str) -> str:
    """A random name: the customer's file name never reaches the storage path."""
    return f"kyc-requests/{uuid.uuid4().hex}.{ALLOWED_TYPES[mime]}"


# ----- Private storage ---------------------------------------------------------------------------------------------


class PrivateStorage:
    """A private GCS bucket (KYC_PRIVATE_BUCKET) or a local folder outside MEDIA_ROOT (KYC_PRIVATE_DIR).
    Neither has a public address: files are read only by kyc.link_views.document_download."""

    def _bucket(self) -> str:
        return str(getattr(settings, "KYC_PRIVATE_BUCKET", "") or "").strip()

    def _local_path(self, key: str):
        from pathlib import Path

        root = Path(getattr(settings, "KYC_PRIVATE_DIR", "") or (Path(settings.BASE_DIR) / "private" / "kyc"))
        path = (root / key).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("bad storage key")
        return path

    def save(self, key: str, content: bytes, mime: str) -> None:
        bucket = self._bucket()
        if bucket:
            from google.cloud import storage  # type: ignore

            storage.Client().bucket(bucket).blob(key).upload_from_string(content, content_type=mime)
            return
        path = self._local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read(self, key: str) -> bytes:
        bucket = self._bucket()
        if bucket:
            from google.cloud import storage  # type: ignore

            return storage.Client().bucket(bucket).blob(key).download_as_bytes()
        return self._local_path(key).read_bytes()

    def delete(self, key: str) -> None:
        try:
            bucket = self._bucket()
            if bucket:
                from google.cloud import storage  # type: ignore

                storage.Client().bucket(bucket).blob(key).delete()
            else:
                self._local_path(key).unlink(missing_ok=True)
        except Exception:
            pass


private_storage = PrivateStorage()

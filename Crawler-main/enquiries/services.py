"""HTTP client helpers for the enquiry microservice."""

from __future__ import annotations

import json
import os
from datetime import datetime
from collections.abc import Mapping
import re
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Sequence

import requests
from django.conf import settings
from requests import RequestException

from .constants import DETAIL_FIELD_LABELS


GOOGLE_SHEET_URL_RE = re.compile(r"https?://docs\.google\.com/spreadsheets/d/[A-Za-z0-9-_]+[^\s\)]*")


class EnquiryServiceError(RuntimeError):
    """Raised when the enquiry microservice returns an error."""


_AUTH_COOKIE_NAME = getattr(settings, "AUTH_ACCESS_COOKIE_NAME", os.getenv("AUTH_ACCESS_COOKIE_NAME", "session"))
_SERVICE_TOKEN = getattr(settings, "ENQUIRY_SERVICE_TOKEN", os.getenv("ENQUIRY_SERVICE_TOKEN"))
_SERVICE_AUTH_HEADER = getattr(
    settings,
    "ENQUIRY_SERVICE_AUTH_HEADER",
    os.getenv("ENQUIRY_SERVICE_AUTH_HEADER", "X-Service-Auth"),
)


def _timeout() -> int:
    raw_value: Any = getattr(
        settings,
        "ENQUIRY_API_TIMEOUT",
        os.getenv("ENQUIRY_API_TIMEOUT", "20"),
    )
    try:
        timeout = int(raw_value)
    except (TypeError, ValueError):
        timeout = 20
    return max(timeout, 1)


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _prepare_enquiry(raw: Mapping[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = dict(raw)
    attachments = list(data.get("attachments") or [])
    profile_attachments: List[Any] = []
    general_attachments: List[Any] = []
    for attachment in attachments:
        category = attachment.get("category") if isinstance(attachment, Mapping) else getattr(attachment, "category", None)
        if category == "profile":
            profile_attachments.append(attachment)
        else:
            general_attachments.append(attachment)
    data["attachments"] = attachments
    data["profile_attachments"] = profile_attachments
    data["general_attachments"] = general_attachments
    data["attachment_count"] = len(attachments)
    data["profile_attachment_count"] = len(profile_attachments)
    data["general_attachment_count"] = len(general_attachments)
    data["received_at_dt"] = _parse_iso8601(data.get("received_at"))
    # Extract structured rejection reason from remarks if present
    remarks_raw = str(data.get("remarks") or "").strip()
    if remarks_raw:
        marker = "Rejection reason (via share link):"
        rejection_reason: Optional[str] = None
        for line in str(remarks_raw).splitlines():
            if marker in line:
                reason_text = line.split(marker, 1)[1].strip()
                if reason_text:
                    rejection_reason = reason_text
        if rejection_reason:
            data["rejection_reason"] = rejection_reason
    detail_items: List[Dict[str, Any]] = []
    for key, label in DETAIL_FIELD_LABELS.items():
        value = data.get(key)
        if value in (None, "", [], {}):
            continue
        detail_items.append({"key": key, "label": label, "value": value})
    data["detail_items"] = detail_items
    subdivisions_raw = data.get("subdivisions") or []
    subdivisions: List[Dict[str, Any]] = []
    for item in subdivisions_raw:
        if isinstance(item, Mapping):
            subdivisions.append(_prepare_subdivision(item))
        else:
            try:
                subdivisions.append(_prepare_subdivision(dict(item)))
            except Exception:
                continue
    data["subdivisions"] = subdivisions
    sheet_url = _extract_google_sheet_url(data)
    if sheet_url:
        data["sheet_url"] = sheet_url
    return data


def _prepare_subdivision(raw: Mapping[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = dict(raw)
    attachments = list(data.get("attachments") or [])
    profile_attachments: List[Any] = []
    general_attachments: List[Any] = []
    for attachment in attachments:
        category = attachment.get("category") if isinstance(attachment, Mapping) else getattr(attachment, "category", None)
        if category == "profile":
            profile_attachments.append(attachment)
        else:
            general_attachments.append(attachment)
    data["attachments"] = attachments
    data["profile_attachments"] = profile_attachments
    data["general_attachments"] = general_attachments
    data["attachment_count"] = len(attachments)
    data["profile_attachment_count"] = len(profile_attachments)
    data["general_attachment_count"] = len(general_attachments)
    data["received_at_dt"] = _parse_iso8601(data.get("received_at"))
    detail_items: List[Dict[str, Any]] = []
    for key, label in DETAIL_FIELD_LABELS.items():
        value = data.get(key)
        if value in (None, "", [], {}):
            continue
        detail_items.append({"key": key, "label": label, "value": value})
    data["detail_items"] = detail_items
    sheet_url = _extract_google_sheet_url(data)
    if sheet_url:
        data["sheet_url"] = sheet_url
    return data


def _extract_google_sheet_url(values: Mapping[str, Any]) -> Optional[str]:
    def _normalise(candidate: Any) -> Optional[str]:
        if candidate in (None, ""):
            return None
        if not isinstance(candidate, str):
            candidate = str(candidate)
        match = GOOGLE_SHEET_URL_RE.search(candidate)
        if not match:
            return None
        url = match.group(0).rstrip(")., ")
        return url or None

    for key in ("sheet_url", "google_sheet_url"):
        url = _normalise(values.get(key))
        if url:
            return url

    for field in ("remarks", "description"):
        url = _normalise(values.get(field))
        if url:
            return url

    return None


def _auth_headers(request: Optional[Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {}

    token: Optional[str] = None
    if request is not None:
        try:
            token = request.COOKIES.get(_AUTH_COOKIE_NAME)
        except Exception:
            token = None

        if not token:
            session = getattr(request, "session", None)
            if session:
                token = session.get("auth_access_token") or session.get("access_token")

        if not token:
            auth_header = getattr(request, "META", {}).get("HTTP_AUTHORIZATION")
            if auth_header and auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1].strip()

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if _SERVICE_TOKEN and _SERVICE_AUTH_HEADER:
        headers[_SERVICE_AUTH_HEADER] = _SERVICE_TOKEN

    return headers


def _request(
    method: str,
    url: str,
    *,
    request: Optional[Any] = None,
    timeout: Optional[int] = None,
    **kwargs: Any,
) -> requests.Response:
    headers = kwargs.pop("headers", {})
    auth_headers = _auth_headers(request)
    if auth_headers:
        merged = dict(headers)
        merged.update(auth_headers)
        headers = merged
    if headers:
        kwargs["headers"] = headers

    request_timeout = timeout if timeout is not None else _timeout()
    try:
        response = requests.request(method, url, timeout=request_timeout, **kwargs)
    except RequestException as exc:
        raise EnquiryServiceError(str(exc)) from exc
    if response.status_code >= 400:
        snippet = response.text.strip()
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        message = f"Service returned {response.status_code}"
        if snippet:
            message = f"{message}: {snippet}"
        raise EnquiryServiceError(message)
    return response


def _build_payload(values: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        payload[key] = value
    return payload


def _build_json_payload(values: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            payload[key] = None
            continue
        payload[key] = value
    return payload


def _json_data(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise EnquiryServiceError("Received invalid JSON from enquiry service") from exc


def _json(response: requests.Response) -> Dict[str, Any]:
    data = _json_data(response)
    if not isinstance(data, Mapping):
        raise EnquiryServiceError("Expected JSON object from enquiry service")
    return dict(data)


def fetch_board(api_root: str, request: Optional[Any] = None) -> Dict[str, List[Dict[str, Any]]]:
    url = f"{api_root}/enquiries/board"
    response = _request("GET", url, request=request)
    data = _json(response)
    board = {
        "open": [_prepare_enquiry(item) for item in data.get("open", [])],
        "closed": [_prepare_enquiry(item) for item in data.get("closed", [])],
    }
    return board


def list_enquiries(
    api_root: str,
    *,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    request: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    url = f"{api_root}/enquiries"
    params: Dict[str, Any] = {}
    if status:
        params["status"] = status
    if priority:
        params["priority"] = priority
    response = _request("GET", url, params=params or None, request=request)
    data = _json_data(response)
    if not isinstance(data, list):
        raise EnquiryServiceError("Expected list of enquiries from service")
    return [_prepare_enquiry(item) for item in data]


def fetch_enquiry(api_root: str, enquiry_id: str, request: Optional[Any] = None) -> Dict[str, Any]:
    url = f"{api_root}/enquiries/{enquiry_id}"
    response = _request("GET", url, request=request)
    return _prepare_enquiry(_json(response))


def fetch_summary(
    api_root: str,
    *,
    period: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    request: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    url = f"{api_root}/enquiries/summary"
    params: Dict[str, Any] = {}
    if period:
        params["period"] = period
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    summary_timeout = max(
        _timeout(),
        getattr(settings, "ENQUIRY_SUMMARY_TIMEOUT", int(os.getenv("ENQUIRY_SUMMARY_TIMEOUT", "60"))),
    )
    response = _request("GET", url, params=params or None, request=request, timeout=summary_timeout)
    data = _json(response)
    data["generated_at_dt"] = _parse_iso8601(data.get("generated_at"))
    return data


def create_enquiry(
    api_root: str,
    values: Mapping[str, Any],
    attachments: Optional[Iterable[Any]] = None,
    profile_files: Optional[Iterable[Any]] = None,
    *,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    payload = _build_payload(values)
    multipart_files: List[Any] = []
    for upload in attachments or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "files",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    for upload in profile_files or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "profile",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    url = f"{api_root}/enquiries"
    if multipart_files:
        data = {"payload": json.dumps(payload)}
        response = _request("POST", url, data=data, files=multipart_files, request=request)
    else:
        response = _request("POST", url, json=payload, request=request)
    return _prepare_enquiry(_json(response))


def export_enquiry_pdf(
    api_root: str,
    enquiry_id: str,
    *,
    request: Optional[Any] = None,
) -> tuple[bytes, str]:
    url = f"{api_root}/enquiries/{enquiry_id}/export/pdf"
    response = _request("GET", url, request=request)
    filename = "enquiry.pdf"
    disposition = response.headers.get("Content-Disposition")
    if disposition and "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].strip('";')
    return response.content, filename


def download_attachment(
    api_root: str,
    download_url: str,
    *,
    request: Optional[Any] = None,
) -> tuple[bytes, str, str]:
    if not download_url.startswith("/"):
        download_url = f"/{download_url}"
    url = f"{api_root}{download_url}"
    response = _request("GET", url, request=request)
    filename = "attachment"
    disposition = response.headers.get("Content-Disposition")
    if disposition and "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].strip('";')
    content_type = response.headers.get("Content-Type", "application/octet-stream")
    return response.content, filename, content_type


def update_enquiry(
    api_root: str,
    enquiry_id: str,
    values: Mapping[str, Any],
    attachments: Optional[Iterable[Any]] = None,
    profile_files: Optional[Iterable[Any]] = None,
    *,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    payload = _build_json_payload(values)
    multipart_files: List[Any] = []
    for upload in attachments or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "files",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    for upload in profile_files or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "profile",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    url = f"{api_root}/enquiries/{enquiry_id}"
    if multipart_files:
        data = {"payload": json.dumps(payload)}
        response = _request("PATCH", url, data=data, files=multipart_files, request=request)
    else:
        response = _request("PATCH", url, json=payload, request=request)
    return _prepare_enquiry(_json(response))


def delete_enquiry(
    api_root: str,
    enquiry_id: str,
    *,
    request: Optional[Any] = None,
) -> None:
    """Archive an enquiry from the catalogue.

    The underlying service performs a soft-delete; callers should treat this as irreversible.
    """
    url = f"{api_root}/enquiries/{enquiry_id}"
    _request("DELETE", url, request=request)


def delete_enquiry(api_root: str, enquiry_id: str, request: Optional[Any] = None) -> None:
    url = f"{api_root}/enquiries/{enquiry_id}"
    _request("DELETE", url, request=request)


def fetch_subdivisions(api_root: str, enquiry_id: str, request: Optional[Any] = None) -> List[Dict[str, Any]]:
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions"
    response = _request("GET", url, request=request)
    data = _json_data(response)
    if not isinstance(data, list):
        raise EnquiryServiceError("Expected list of subdivisions from service")
    return [_prepare_subdivision(item) for item in data]


def fetch_subdivision(
    api_root: str,
    enquiry_id: str,
    subdivision_id: str,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions/{subdivision_id}"
    response = _request("GET", url, request=request)
    return _prepare_subdivision(_json(response))


def create_subdivision(
    api_root: str,
    enquiry_id: str,
    values: Mapping[str, Any],
    attachments: Optional[Iterable[Any]] = None,
    profile_files: Optional[Iterable[Any]] = None,
    *,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    payload = _build_payload(values)
    multipart_files: List[Any] = []
    for upload in attachments or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "files",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    for upload in profile_files or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "profile",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions"
    if multipart_files:
        data = {"payload": json.dumps(payload)}
        response = _request("POST", url, data=data, files=multipart_files, request=request)
    else:
        response = _request("POST", url, json=payload, request=request)
    return _prepare_subdivision(_json(response))


def update_subdivision(
    api_root: str,
    enquiry_id: str,
    subdivision_id: str,
    values: Mapping[str, Any],
    attachments: Optional[Iterable[Any]] = None,
    profile_files: Optional[Iterable[Any]] = None,
    *,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    payload = _build_json_payload(values)
    multipart_files: List[Any] = []
    for upload in attachments or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "files",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    for upload in profile_files or []:
        if not upload:
            continue
        try:
            upload.seek(0)
        except Exception:
            pass
        multipart_files.append(
            (
                "profile",
                (upload.name, upload.read(), upload.content_type or "application/octet-stream"),
            )
        )
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions/{subdivision_id}"
    if multipart_files:
        data = {"payload": json.dumps(payload)}
        response = _request("PATCH", url, data=data, files=multipart_files, request=request)
    else:
        response = _request("PATCH", url, json=payload, request=request)
    return _prepare_subdivision(_json(response))


def delete_subdivision(
    api_root: str,
    enquiry_id: str,
    subdivision_id: str,
    request: Optional[Any] = None,
) -> None:
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions/{subdivision_id}"
    _request("DELETE", url, request=request)


def export_subdivision_pdf(
    api_root: str,
    enquiry_id: str,
    subdivision_id: str,
    request: Optional[Any] = None,
) -> tuple[bytes, str]:
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions/{subdivision_id}/export/pdf"
    response = _request("GET", url, request=request)
    filename = "enquiry.pdf"
    disposition = response.headers.get("Content-Disposition")
    if disposition and "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].strip('";')
    return response.content, filename


def download_subdivision_attachment(
    api_root: str,
    enquiry_id: str,
    subdivision_id: str,
    attachment_id: str,
    request: Optional[Any] = None,
) -> tuple[bytes, str, str]:
    url = f"{api_root}/enquiries/{enquiry_id}/subdivisions/{subdivision_id}/attachments/{attachment_id}"
    response = _request("GET", url, request=request)
    filename = "attachment"
    disposition = response.headers.get("Content-Disposition")
    if disposition and "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].strip('";')
    content_type = response.headers.get("Content-Type", "application/octet-stream")
    return response.content, filename, content_type


def list_email_events(
    api_root: str,
    *,
    status: Optional[str] = None,
    limit: int = 50,
    request: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    url = f"{api_root}/integrations/email/events"
    params: Dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    response = _request("GET", url, params=params, request=request)
    data = _json(response)
    events = data.get("events", [])
    prepared: List[Dict[str, Any]] = []
    for item in events:
        if isinstance(item, Mapping):
            record = dict(item)
        else:
            try:
                record = dict(item)
            except Exception:
                continue
        record["received_at_dt"] = _parse_iso8601(record.get("received_at"))
        record["created_at_dt"] = _parse_iso8601(record.get("created_at"))
        prepared.append(record)
    return prepared


def promote_email_event(
    api_root: str,
    message_id: str,
    *,
    priority: Optional[str] = None,
    assigned_to: Optional[str] = None,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    url = f"{api_root}/integrations/email/events/{message_id}/promote"
    payload: Dict[str, Any] = {}
    if priority:
        payload["priority"] = priority
    if assigned_to:
        payload["assigned_to"] = assigned_to
    response = _request("POST", url, json=payload or {}, request=request)
    return _json(response)


def excel_pull(
    api_root: str,
    *,
    apply_changes: bool = False,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    url = f"{api_root}/integrations/excel/pull"
    payload = {"apply_changes": bool(apply_changes)}
    response = _request("POST", url, json=payload, request=request)
    return _json(response)


def excel_push(
    api_root: str,
    *,
    enquiry_ids: Optional[Sequence[str]] = None,
    include_closed: bool = True,
    request: Optional[Any] = None,
) -> Dict[str, Any]:
    url = f"{api_root}/integrations/excel/push"
    payload: Dict[str, Any] = {"include_closed": bool(include_closed)}
    if enquiry_ids:
        payload["enquiry_ids"] = list(enquiry_ids)
    response = _request("POST", url, json=payload, request=request)
    return _json(response)

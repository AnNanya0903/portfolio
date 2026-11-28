"""Stored IoT samples: saving bridge payloads, pulling new ones from the bridge, and reading compact history.

The IoT bridge (microservices/iot-service) receives the factory sender's posts. Django keeps its own copy of
every sample in IoTSample so that any past day can be charted and every batch can be replayed, independent
of how much history the bridge returns per request.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Iterable, List, Optional

import requests
from django.conf import settings
from django.core.cache import cache

from . import iot_config
from .models import IoTSample

logger = logging.getLogger(__name__)

# What the dashboard chart and the batch maths read; the same keys the history API returns.
COMPACT_FIELDS = ("avg_current", "t_watt", "t_kwh_import", "ch1_temperature", "ch2_temperature")

_LATEST_CACHE_KEY = "compounding:iot:latest_payload:{machine}"
_SYNC_LOCK_KEY = "compounding:iot:sync_lock:{machine}"


class IoTSyncError(RuntimeError):
    pass


def _signals():
    # The signal table (register map, aliases, scaling) lives with the IoT views; import lazily because
    # magicwand.views itself imports compounding.
    from magicwand import views as iot_views

    return iot_views


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sample_values(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{ts_ms, <value fields>} for one bridge/ingest payload, or None when it has no usable timestamp."""
    signals = _signals()
    ts = signals._coerce_iot_timestamp(payload.get("ts") if payload.get("ts") is not None else payload.get("timestamp"))
    if ts is None:
        return None
    registers = signals._coerce_iot_registers(payload.get("registers")) or []
    raw_map = signals._coerce_iot_raw_map(payload.get("raw"))
    values: Dict[str, Any] = {"ts_ms": ts}
    for field in IoTSample.VALUE_FIELDS:
        spec = signals._iot_signal_spec(field)
        values[field] = _finite(signals._iot_signal_value(spec, registers, raw_map)) if spec else None
    return values


def store_payloads(machine_id: str, payloads: Iterable[Dict[str, Any]]) -> int:
    """Save payloads as samples for `machine_id` (the public id). Samples already stored are skipped."""
    rows: List[IoTSample] = []
    seen: set[int] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        values = sample_values(payload)
        if values is None or values["ts_ms"] in seen:
            continue
        seen.add(values["ts_ms"])
        rows.append(IoTSample(machine_id=machine_id, **values))
    if not rows:
        return 0
    existing = set(
        IoTSample.objects.filter(
            machine_id=machine_id, ts_ms__gte=min(seen), ts_ms__lte=max(seen)
        ).values_list("ts_ms", flat=True)
    )
    new_rows = [row for row in rows if row.ts_ms not in existing]
    # ignore_conflicts still covers a concurrent sync writing the same samples.
    IoTSample.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)
    return len(new_rows)


def last_stored_ts(machine_id: str) -> Optional[int]:
    return (
        IoTSample.objects.filter(machine_id=machine_id).order_by("-ts_ms").values_list("ts_ms", flat=True).first()
    )


def has_samples(machine_id: str, start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> bool:
    query = IoTSample.objects.filter(machine_id=machine_id)
    if start_ts is not None:
        query = query.filter(ts_ms__gte=start_ts)
    if end_ts is not None:
        query = query.filter(ts_ms__lte=end_ts)
    return query.exists()


def remember_latest_payload(machine_id: str, payload: Dict[str, Any]) -> None:
    """Keep the newest full payload (registers and all) so `latest` can be served without another bridge call."""
    cache.set(_LATEST_CACHE_KEY.format(machine=machine_id), payload, timeout=120)


def cached_latest_payload(machine_id: str) -> Optional[Dict[str, Any]]:
    payload = cache.get(_LATEST_CACHE_KEY.format(machine=machine_id))
    return payload if isinstance(payload, dict) else None


def latest_sample(machine_id: str) -> Optional[IoTSample]:
    return IoTSample.objects.filter(machine_id=machine_id).order_by("-ts_ms").first()


# Bridge sync ----------------------------------------------------------------------------------------------
def bridge_enabled() -> bool:
    return bool((getattr(settings, "IOT_PORTAL_URL", "") or "").strip()) and not getattr(settings, "DEBUG", False)


def _fetch_bridge_history(machine_id: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    signals = _signals()
    token = (getattr(settings, "IOT_SERVICE_TOKEN", "") or "").strip()
    if not token:
        raise IoTSyncError("IOT_SERVICE_TOKEN is not configured")
    try:
        url = signals._build_iot_bridge_url("/history")
    except Exception as exc:  # IoTDashboardFetchError for a bad IOT_PORTAL_URL
        raise IoTSyncError(str(exc)) from exc
    source_id = signals._iot_source_machine_id(machine_id) or machine_id
    try:
        response = requests.get(
            url,
            # limit=0 with a time range asks the bridge for every stored sample in it, not the newest N.
            params={"machineId": source_id, "startTs": start_ts, "endTs": end_ts, "limit": 0},
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=getattr(settings, "IOT_PROXY_TIMEOUT", 20),
        )
    except requests.RequestException as exc:
        raise IoTSyncError(f"IoT bridge request failed: {exc}") from exc
    if response.status_code >= 400:
        raise IoTSyncError(f"IoT bridge returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise IoTSyncError("IoT bridge returned invalid JSON") from exc
    history = body.get("history") if isinstance(body, dict) else None
    return [item for item in history or [] if isinstance(item, dict)]


def sync_from_bridge(
    machine_id: str,
    *,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Copy samples from the bridge into IoTSample, in chunks. With no start, continue after the newest stored one."""
    now_ms = int(time.time() * 1000)
    end_ts = now_ms if end_ts is None else end_ts
    if start_ts is None:
        newest = last_stored_ts(machine_id)
        start_ts = newest + 1 if newest is not None else now_ms - iot_config.sync_initial_lookback_s() * 1000
    chunk_ms = max(iot_config.sync_chunk_s(), 10) * 1000
    fetched = stored = 0
    newest_payload: Optional[Dict[str, Any]] = None
    cursor = start_ts
    while cursor <= end_ts:
        chunk_end = min(cursor + chunk_ms - 1, end_ts)
        payloads = _fetch_bridge_history(machine_id, cursor, chunk_end)
        fetched += len(payloads)
        stored += store_payloads(machine_id, payloads)
        if payloads:
            newest_payload = max(payloads, key=lambda item: _signals()._coerce_iot_timestamp(item.get("ts")) or 0)
        cursor = chunk_end + 1
    if newest_payload is not None:
        remember_latest_payload(machine_id, newest_payload)
    return {"machineId": machine_id, "startTs": start_ts, "endTs": end_ts, "fetched": fetched, "stored": stored}


def sync_recent(machine_id: str) -> Optional[Dict[str, Any]]:
    """Pull the newest samples, at most once every IOT_SYNC_THROTTLE_S per machine. Never raises."""
    if not bridge_enabled() or not machine_id:
        return None
    if not cache.add(_SYNC_LOCK_KEY.format(machine=machine_id), 1, timeout=max(iot_config.sync_throttle_s(), 1)):
        return None
    try:
        return sync_from_bridge(machine_id)
    except Exception as exc:  # the dashboard still works from what is stored
        logger.warning("IoT sample sync failed for %s: %s", machine_id, exc)
        return None


# Reading ---------------------------------------------------------------------------------------------------
def _point(row: tuple) -> Dict[str, Any]:
    ts, *values = row
    point: Dict[str, Any] = {"ts": ts}
    for field, value in zip(COMPACT_FIELDS, values):
        point[field] = round(value, 3) if value is not None else None
    return point


def downsample_points(points: List[Dict[str, Any]], max_points: Optional[int]) -> List[Dict[str, Any]]:
    """Keep the shape of the current trace: each bucket contributes its lowest and highest avg_current point.

    A plain stride would drop the short loading spikes the batch analysis cares about.
    """
    if not max_points or len(points) <= max_points:
        return points
    if max_points < 4:
        return points[-max_points:]
    buckets = max_points // 2
    size = len(points) / buckets
    result: List[Dict[str, Any]] = []
    for index in range(buckets):
        bucket = points[int(index * size): int((index + 1) * size)] or []
        if not bucket:
            continue
        with_current = [point for point in bucket if point.get("avg_current") is not None]
        if not with_current:
            result.append(bucket[len(bucket) // 2])
            continue
        low = min(with_current, key=lambda point: point["avg_current"])
        high = max(with_current, key=lambda point: point["avg_current"])
        result.extend(sorted({id(low): low, id(high): high}.values(), key=lambda point: point["ts"]))
    if result[-1]["ts"] != points[-1]["ts"]:
        result.append(points[-1])
    return result


def compact_history(
    machine_id: str,
    *,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 0,
    since_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Compact points in time order. `limit` (when no range is given) keeps the newest N samples."""
    query = IoTSample.objects.filter(machine_id=machine_id)
    if start_ts is not None:
        query = query.filter(ts_ms__gte=start_ts)
    if end_ts is not None:
        query = query.filter(ts_ms__lte=end_ts)
    if since_ts is not None:
        query = query.filter(ts_ms__gt=since_ts)
    fields = ("ts_ms",) + COMPACT_FIELDS
    if limit and start_ts is None and end_ts is None:
        rows = list(query.order_by("-ts_ms").values_list(*fields)[:limit])
        rows.reverse()
    else:
        rows = list(query.order_by("ts_ms").values_list(*fields))
    return [_point(row) for row in rows]


def samples_between(machine_id: str, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """Every stored sample in [start_ts, end_ts] as dicts with ts_ms and all value fields, oldest first."""
    return list(
        IoTSample.objects.filter(machine_id=machine_id, ts_ms__gte=start_ts, ts_ms__lte=end_ts)
        .order_by("ts_ms")
        .values("ts_ms", *IoTSample.VALUE_FIELDS)
    )

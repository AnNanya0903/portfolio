"""Tunable numbers for IoT sample storage, batch detection and standard curves.

Each one can be overridden in Django settings (or the env file that feeds them); the defaults match the
two-roll mixing mill (mixing-mill-2), which samples every ~2 s and runs ~14-17 min batches.
"""
from __future__ import annotations

from django.conf import settings


def _number(name: str, default: float) -> float:
    value = getattr(settings, name, None)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    return int(_number(name, default))


# Batch detection -------------------------------------------------------------------------------------------
def start_threshold_a() -> float:
    """A batch starts once avg_current stays above this..."""
    return _number("IOT_BATCH_START_THRESHOLD_A", 40.0)


def start_sustain_s() -> int:
    """...for this many seconds."""
    return _int("IOT_BATCH_START_SUSTAIN_S", 60)


def idle_threshold_a() -> float:
    """A batch ends once avg_current stays below this..."""
    return _number("IOT_BATCH_IDLE_THRESHOLD_A", 15.0)


def idle_sustain_s() -> int:
    """...for this many seconds."""
    return _int("IOT_BATCH_IDLE_SUSTAIN_S", 90)


def load_threshold_a() -> float:
    """The first sample above this is the loading spike: curves are aligned on it, not on the button press."""
    return _number("IOT_BATCH_LOAD_THRESHOLD_A", 90.0)


def spike_settle_s() -> int:
    """The loading spike is over once the current stays at or below the load threshold for this long."""
    return _int("IOT_BATCH_SPIKE_SETTLE_S", 20)


def max_gap_s() -> int:
    """A gap in telemetry longer than this ends a running batch at the last sample before the gap."""
    return _int("IOT_BATCH_MAX_GAP_S", 120)


def min_batch_seconds() -> int:
    """Batches shorter than this are marked invalid and left out of every statistic."""
    return _int("MIN_BATCH_SECONDS", 480)


def manual_attach_window_s() -> int:
    """A detected run is attached to an operator's "Start batch" pressed up to this long after the run began."""
    return _int("IOT_BATCH_MANUAL_ATTACH_WINDOW_S", 600)


# Standard curves -------------------------------------------------------------------------------------------
def curve_step_s() -> int:
    return _int("IOT_CURVE_STEP_S", 10)


def curve_min_batches() -> int:
    return _int("IOT_CURVE_MIN_BATCHES", 20)


def curve_sigma() -> float:
    return _number("IOT_CURVE_SIGMA", 2.0)


def curve_out_of_band_tolerance_s() -> int:
    """A batch is out of band when more than this many seconds of its curve fall outside the band."""
    return _int("IOT_CURVE_OUT_OF_BAND_TOLERANCE_S", 60)


# Sample storage --------------------------------------------------------------------------------------------
def sync_throttle_s() -> int:
    """Dashboard requests pull new samples from the bridge at most this often."""
    return _int("IOT_SYNC_THROTTLE_S", 5)


def sync_chunk_s() -> int:
    """Backfills ask the bridge for this much history per request."""
    return _int("IOT_SYNC_CHUNK_S", 600)


def sync_initial_lookback_s() -> int:
    """With nothing stored yet, the first sync reaches this far back."""
    return _int("IOT_SYNC_INITIAL_LOOKBACK_S", 7200)

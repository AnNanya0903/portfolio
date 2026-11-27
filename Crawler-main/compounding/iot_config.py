"""Tunable numbers for IoT sample storage, machine-state segmentation, standards, pattern matching and the
machine-vs-log reconciliation.

Each one can be overridden in Django settings (or the env file that feeds them). The defaults match the
two-roll mixing mill (mixing-mill-2): samples every ~2 s, OFF at 0 A, IDLE at ~35-40 A, batches of ~15-20 min.
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


# Segmentation (state machine on a rolling mean of avg_current) ---------------------------------------------
def rolling_window_s() -> int:
    return _int("IOT_SEG_ROLLING_S", 30)


def off_below_a() -> float:
    """Rolling mean below this is OFF."""
    return _number("IOT_SEG_OFF_BELOW_A", 5.0)


def batch_above_a() -> float:
    """Rolling mean at or above this (for long enough) is a BATCH; between OFF and this is IDLE."""
    return _number("IOT_SEG_BATCH_ABOVE_A", 45.0)


def batch_start_sustain_s() -> int:
    return _int("IOT_SEG_BATCH_START_SUSTAIN_S", 60)


def batch_end_sustain_s() -> int:
    """A batch ends once the rolling mean stays below batch_above_a this long; shorter dips stay in the batch."""
    return _int("IOT_SEG_BATCH_END_SUSTAIN_S", 90)


def reload_dip_s() -> int:
    """A dip at least this long, followed by a new loading spike, starts a new batch even before end_sustain."""
    return _int("IOT_SEG_RELOAD_DIP_S", 40)


def reload_within_s() -> int:
    """...when the spike (a sample above the load threshold) comes within this long of the current rising again."""
    return _int("IOT_SEG_RELOAD_WITHIN_S", 180)


def min_state_s() -> int:
    """IDLE/OFF flickers shorter than this are absorbed into the state around them."""
    return _int("IOT_SEG_MIN_STATE_S", 60)


def max_gap_s() -> int:
    """A telemetry gap longer than this becomes a "No data" segment."""
    return _int("IOT_SEG_MAX_GAP_S", 120)


def min_batch_seconds() -> int:
    """Batch segments shorter than this are not batches (they count as IDLE)."""
    return _int("MIN_BATCH_SECONDS", 480)


def load_threshold_a(machine_id: str = "") -> float:
    """First sample above this is the loading point (load_ts). Per machine via IOT_LOAD_THRESHOLDS = {id: A}."""
    per_machine = getattr(settings, "IOT_LOAD_THRESHOLDS", None) or {}
    if isinstance(per_machine, dict) and machine_id in per_machine:
        try:
            return float(per_machine[machine_id])
        except (TypeError, ValueError):
            pass
    return _number("IOT_LOAD_THRESHOLD_A", 90.0)


def load_fallback_a() -> float:
    """Batches that never pass the load threshold (e.g. AG-FRC 70, peak ~77 A) use this instead."""
    return _number("IOT_LOAD_FALLBACK_A", 70.0)


def detect_throttle_s() -> int:
    return _int("IOT_DETECT_THROTTLE_S", 30)


# Curves, features and standards ---------------------------------------------------------------------------
def curve_step_s() -> int:
    return _int("IOT_CURVE_STEP_S", 10)


def steady_band_a() -> float:
    """A "steady" mixing stretch keeps the 10 s curve within this many amps (max - min)."""
    return _number("IOT_STEADY_BAND_A", 8.0)


def dip_drop_a() -> float:
    """A dip is the curve falling this far below the steady mean."""
    return _number("IOT_DIP_DROP_A", 15.0)


def standard_min_batches() -> int:
    return _int("IOT_STANDARD_MIN_BATCHES", 15)


def standard_band_method() -> str:
    """'percentile' (5th-95th) or 'sigma' (mean +/- 2 sigma)."""
    value = str(getattr(settings, "IOT_STANDARD_BAND", "percentile") or "percentile").lower()
    return value if value in {"percentile", "sigma"} else "percentile"


def standard_percentiles() -> tuple[float, float]:
    return (_number("IOT_STANDARD_LOW_PCT", 5.0), _number("IOT_STANDARD_HIGH_PCT", 95.0))


def standard_sigma() -> float:
    return _number("IOT_STANDARD_SIGMA", 2.0)


# Pattern identification and judging ------------------------------------------------------------------------
def label_min_overlap() -> float:
    """A detected batch takes an operator log's compound as its label when they overlap this much (IoU)."""
    return _number("IOT_LABEL_MIN_OVERLAP", 0.7)


def knn_min_per_compound() -> int:
    return _int("IOT_KNN_MIN_PER_COMPOUND", 15)


def knn_k() -> int:
    return _int("IOT_KNN_K", 5)


def match_threshold_pct() -> float:
    return _number("MATCH_THRESHOLD", _number("IOT_MATCH_THRESHOLD_PCT", 85.0))


def similar_standards_distance() -> float:
    """Standards whose median curves are closer than this (DTW, A per step) are suggested as one process group."""
    return _number("IOT_SIMILAR_STANDARDS_DTW", 4.0)


# Reconciliation --------------------------------------------------------------------------------------------
def recon_time_tolerance_s() -> int:
    return _int("IOT_RECON_TIME_TOLERANCE_S", 180)


def recon_mislog_ratio() -> float:
    """A log shorter than this fraction of the detected batch is Mis-logged."""
    return _number("IOT_RECON_MISLOG_RATIO", 0.5)


def recon_compound_confidence() -> float:
    """Operator and AI disagreeing counts as Compound mismatch only at this AI confidence (%) or above."""
    return _number("IOT_RECON_COMPOUND_CONFIDENCE", 80.0)


def recon_min_idle_s() -> int:
    """IDLE / OFF / No-data stretches shorter than this are not listed as their own rows."""
    return _int("IOT_RECON_MIN_IDLE_S", 300)


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

"""Pattern features of a detected batch, computed from its 10 s curve (aligned on the load point) and numbers.

    peak_current, time_to_peak_s   loading spike height and when it came (from the batch start)
    steady_mean, steady_len_s      the longest stretch where the curve stays within IOT_STEADY_BAND_A
    dips                           times the curve falls IOT_DIP_DROP_A below the steady mean after it
    duration_s, energy_kwh         from the batch numbers
"""
from __future__ import annotations

import statistics
from collections import deque
from typing import Any, Dict, List, Sequence, Tuple

from . import iot_config

FEATURE_KEYS = (
    "peak_current",
    "time_to_peak_s",
    "steady_mean",
    "steady_len_s",
    "duration_s",
    "energy_kwh",
    "dips",
)


def steady_stretch(curve: Sequence[float], band: float) -> Tuple[int, int]:
    """(start, end_exclusive) of the longest run whose max - min stays within band."""
    best = (0, 0)
    start = 0
    lows: deque = deque()
    highs: deque = deque()
    for end, value in enumerate(curve):
        while lows and curve[lows[-1]] >= value:
            lows.pop()
        lows.append(end)
        while highs and curve[highs[-1]] <= value:
            highs.pop()
        highs.append(end)
        while curve[highs[0]] - curve[lows[0]] > band:
            start += 1
            while lows[0] < start:
                lows.popleft()
            while highs[0] < start:
                highs.popleft()
        if end + 1 - start > best[1] - best[0]:
            best = (start, end + 1)
    return best


def count_dips(curve: Sequence[float], after: int, level: float) -> int:
    dips = 0
    below = False
    for value in curve[after:]:
        if value < level and not below:
            dips += 1
            below = True
        elif value >= level:
            below = False
    return dips


def compute_features(curve: Sequence[float], step_s: int, numbers: Dict[str, Any]) -> Dict[str, Any]:
    features: Dict[str, Any] = {
        "peak_current": numbers.get("peak_current"),
        "time_to_peak_s": numbers.get("time_to_peak_s"),
        "duration_s": numbers.get("duration_s"),
        "energy_kwh": numbers.get("energy_kwh"),
        "steady_mean": None,
        "steady_len_s": 0,
        "dips": 0,
    }
    if len(curve) < 2:
        return features
    start, end = steady_stretch(curve, iot_config.steady_band_a())
    if end > start:
        steady_mean = statistics.fmean(curve[start:end])
        features["steady_mean"] = round(steady_mean, 1)
        features["steady_len_s"] = (end - start) * step_s
        features["steady_start_s"] = start * step_s
        features["dips"] = count_dips(curve, end, steady_mean - iot_config.dip_drop_a())
    return features


def feature_vector(features: Dict[str, Any]) -> List[float]:
    return [float(features.get(key) or 0.0) for key in FEATURE_KEYS]

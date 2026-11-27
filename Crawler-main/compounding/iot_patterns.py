"""Analysis of a detected batch after detection: its label from the operator log, its pattern features,
the predicted compound (or process group) and the judgement against that standard.

Prediction: the nearest active standard by DTW distance between the batch's 10 s curve and each standard's
median curve. Once every candidate has IOT_KNN_MIN_PER_COMPOUND labelled batches (15), a kNN vote on the
standardised features is used instead. The output is predicted label + confidence % + the top-2
alternatives. Compounds in a process group are predicted as the group.

Judgement (pattern only): match_pct = share of the curve inside the standard's band. "Matches standard" at
MATCH_THRESHOLD (85 %) or more, else "Doesn't match"; "No standard yet" without an active standard. Up to two
short reasons explain the biggest deviations (spike timing, energy, mixing stretch, duration, spike height).
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.conf import settings

from . import iot_config, iot_features, iot_standards
from .models import DetectedBatch, StandardCurve


# DTW ------------------------------------------------------------------------------------------------------
def dtw_distance(a: Sequence[float], b: Sequence[float], window: Optional[int] = None) -> float:
    """Mean absolute difference (A) along the best DTW alignment, with a Sakoe-Chiba band."""
    n, m = len(a), len(b)
    if not n or not m:
        return math.inf
    window = max(window if window is not None else max(n, m) // 4, abs(n - m), 1)
    inf = math.inf
    previous = [inf] * (m + 1)
    previous_len = [0] * (m + 1)
    previous[0] = 0.0
    for i in range(1, n + 1):
        current = [inf] * (m + 1)
        current_len = [0] * (m + 1)
        for j in range(max(1, i - window), min(m, i + window) + 1):
            cost = abs(a[i - 1] - b[j - 1])
            options = (
                (previous[j - 1], previous_len[j - 1]),
                (previous[j], previous_len[j]),
                (current[j - 1], current_len[j - 1]),
            )
            best, length = min(options, key=lambda item: item[0])
            if best < inf:
                current[j] = best + cost
                current_len[j] = length + 1
        previous, previous_len = current, current_len
    if previous[m] == inf:
        return inf
    return previous[m] / max(previous_len[m], 1)


def _max_dtw() -> float:
    """Beyond this distance (A per step) a batch is an unknown pattern rather than any standard."""
    return float(getattr(settings, "IOT_PATTERN_MAX_DTW", 12.0) or 12.0)


def _median_curve(standard: StandardCurve) -> List[float]:
    return [point["median"] for point in standard.points]


# Prediction -----------------------------------------------------------------------------------------------
def _candidates(curve: Sequence[float], prefix: bool = False) -> List[Dict[str, Any]]:
    candidates = []
    for standard in iot_standards.active_standards():
        median = _median_curve(standard)
        if prefix:
            median = median[: len(curve)]
        distance = dtw_distance(curve, median)
        if math.isfinite(distance):
            candidates.append({"key": standard.key, "label": standard.label, "distance": round(distance, 2),
                               "standard": standard})
    return sorted(candidates, key=lambda item: item["distance"])


def _shares(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Confidence from inverse-square distances, with an "unknown" pseudo-candidate at the max distance,
    so one distant standard does not get 100 %."""
    weights = [1.0 / (item["distance"] + 0.5) ** 2 for item in candidates]
    unknown = 1.0 / (_max_dtw() + 0.5) ** 2
    total = sum(weights) + unknown
    return [{**item, "confidence": round(100.0 * weight / total, 1)} for item, weight in zip(candidates, weights)]


def predict_dtw(curve: Sequence[float], *, prefix: bool = False) -> Dict[str, Any]:
    candidates = _candidates(curve, prefix=prefix)
    if not candidates or candidates[0]["distance"] > _max_dtw():
        return {"key": "", "label": "", "confidence": None, "alternatives": [], "method": "dtw" if candidates else ""}
    ranked = _shares(candidates)
    top = ranked[0]
    return {
        "key": top["key"],
        "label": top["label"],
        "confidence": top["confidence"],
        "alternatives": [{"key": item["key"], "label": item["label"], "confidence": item["confidence"]}
                         for item in ranked[1:3]],
        "method": "dtw",
    }


def _knn_reference(exclude_id: Optional[int]) -> Optional[Dict[str, List[DetectedBatch]]]:
    """Labelled batches per standard key, when every key with a standard has enough of them (>= 2 keys)."""
    by_key: Dict[str, List[DetectedBatch]] = {}
    for standard in iot_standards.active_standards():
        batches = [batch for batch in iot_standards.reference_batches(standard.key) if batch.pk != exclude_id]
        by_key[(standard.key, standard.label)] = batches
    if len(by_key) < 2 or any(len(batches) < iot_config.knn_min_per_compound() for batches in by_key.values()):
        return None
    return by_key


def predict_knn(batch: DetectedBatch) -> Optional[Dict[str, Any]]:
    reference = _knn_reference(batch.pk)
    if reference is None:
        return None
    rows: List[Tuple[Tuple[str, str], List[float]]] = []
    for key_label, batches in reference.items():
        for item in batches:
            rows.append((key_label, iot_features.feature_vector(item.features or {})))
    columns = list(zip(*(vector for _key, vector in rows)))
    means = [statistics.fmean(column) for column in columns]
    spreads = [statistics.pstdev(column) or 1.0 for column in columns]

    def scaled(vector: Sequence[float]) -> List[float]:
        return [(value - mean) / spread for value, mean, spread in zip(vector, means, spreads)]

    target = scaled(iot_features.feature_vector(batch.features or {}))
    distances = sorted(
        (math.dist(target, scaled(vector)), key_label) for key_label, vector in rows
    )[: iot_config.knn_k()]
    votes: Dict[Tuple[str, str], float] = {}
    for distance, key_label in distances:
        votes[key_label] = votes.get(key_label, 0.0) + 1.0 / (distance + 1e-6)
    total = sum(votes.values())
    ranked = sorted(votes.items(), key=lambda item: item[1], reverse=True)
    (top_key, top_label), top_vote = ranked[0]
    return {
        "key": top_key,
        "label": top_label,
        "confidence": round(100.0 * top_vote / total, 1),
        "alternatives": [{"key": key, "label": label, "confidence": round(100.0 * vote / total, 1)}
                         for (key, label), vote in ranked[1:3]],
        "method": "knn",
    }


def predict(batch: DetectedBatch) -> Dict[str, Any]:
    return predict_knn(batch) or predict_dtw(batch.curve or [])


# Judgement ------------------------------------------------------------------------------------------------
def band_inside(curve: Sequence[float], curve_step_s: int, standard: StandardCurve) -> Tuple[int, int]:
    """(points inside the band, points compared) over the stretch both curves cover."""
    inside = compared = 0
    for point in standard.points:
        index = point["t"] // max(curve_step_s, 1)
        if index >= len(curve):
            break
        compared += 1
        if point["lower"] <= curve[index] <= point["upper"]:
            inside += 1
    return inside, compared


def _minutes(seconds: float) -> str:
    value = round(abs(seconds) / 60 * 2) / 2
    return f"{value:g} min"


def build_reasons(batch: DetectedBatch, standard: StandardCurve, match_pct: Optional[float],
                  outside_s: int) -> List[str]:
    stats = standard.stats or {}
    features = batch.features or {}
    found: List[Tuple[float, str]] = []

    def median(name: str) -> Optional[float]:
        return (stats.get(name) or {}).get("median")

    if batch.time_to_peak_s is not None and median("time_to_peak_s") is not None:
        delta = batch.time_to_peak_s - median("time_to_peak_s")
        if abs(delta) >= 60:
            found.append((abs(delta) / 60, f"loading spike {_minutes(delta)} {'late' if delta > 0 else 'early'}"))
    if batch.energy_kwh is not None and median("energy_kwh"):
        pct = (batch.energy_kwh - median("energy_kwh")) / median("energy_kwh") * 100
        if abs(pct) >= 10:
            found.append((abs(pct) / 10, f"energy {pct:+.0f} %"))
    if features.get("steady_len_s") is not None and median("steady_len_s") is not None:
        delta = features["steady_len_s"] - median("steady_len_s")
        if abs(delta) >= 120:
            found.append((abs(delta) / 120, f"mixing stretch {_minutes(delta)} {'long' if delta > 0 else 'short'}"))
    if median("duration_s") is not None:
        delta = batch.duration_s - median("duration_s")
        if abs(delta) >= 120:
            found.append((abs(delta) / 120, f"batch {_minutes(delta)} {'longer' if delta > 0 else 'shorter'}"))
    if batch.peak_current is not None and median("peak_current") is not None:
        delta = batch.peak_current - median("peak_current")
        if abs(delta) >= 15:
            found.append((abs(delta) / 15, f"loading spike {abs(delta):.0f} A {'higher' if delta > 0 else 'lower'}"))
    if match_pct is not None and match_pct < iot_config.match_threshold_pct() and not found:
        found.append((1.0, f"current outside the band for {_minutes(outside_s)}"))
    found.sort(key=lambda item: item[0], reverse=True)
    return [text for _severity, text in found[:2]]


def _judge_key(batch: DetectedBatch) -> Tuple[str, str]:
    """The standard to judge against: the known compound (tag/log), else the prediction."""
    compound = batch.tagged_compound or (batch.log_run.compound_code if batch.log_run_id else "")
    if compound:
        key, label, _group = iot_standards.key_for(compound)
        return key, label
    return batch.predicted_key, batch.predicted_label


def judge(batch: DetectedBatch) -> None:
    key, label = _judge_key(batch)
    batch.judged_key, batch.judged_label = key, label
    standard = iot_standards.active_standard(key) if key else None
    batch.standard = standard
    batch.match_pct = batch.dtw_distance = batch.energy_dev_pct = batch.duration_dev_pct = None
    batch.peak_time_dev_s = None
    batch.reasons = []
    if standard is None or not batch.curve:
        batch.match_result = DetectedBatch.MATCH_NO_STANDARD
        return
    inside, compared = band_inside(batch.curve, batch.curve_step_s, standard)
    batch.match_pct = round(100.0 * inside / compared, 1) if compared else 0.0
    batch.dtw_distance = round(dtw_distance(batch.curve, _median_curve(standard)), 2)
    stats = standard.stats or {}
    energy = (stats.get("energy_kwh") or {}).get("median")
    duration = (stats.get("duration_s") or {}).get("median")
    peak_time = (stats.get("time_to_peak_s") or {}).get("median")
    if batch.energy_kwh is not None and energy:
        batch.energy_dev_pct = round((batch.energy_kwh - energy) / energy * 100, 1)
    if duration:
        batch.duration_dev_pct = round((batch.duration_s - duration) / duration * 100, 1)
    if batch.time_to_peak_s is not None and peak_time is not None:
        batch.peak_time_dev_s = int(round(batch.time_to_peak_s - peak_time))
    batch.match_result = (
        DetectedBatch.MATCH_OK if batch.match_pct >= iot_config.match_threshold_pct() else DetectedBatch.MATCH_NO
    )
    batch.reasons = build_reasons(batch, standard, batch.match_pct, (compared - inside) * standard.step_s)


ANALYSIS_FIELDS = [
    "features", "log_run", "log_overlap", "label_compound",
    "predicted_key", "predicted_label", "prediction_confidence", "prediction_alternatives", "prediction_method",
    "judged_key", "judged_label", "standard", "match_pct", "dtw_distance", "energy_dev_pct", "duration_dev_pct",
    "peak_time_dev_s", "match_result", "reasons", "updated_at",
]


def analyze_batch(batch: DetectedBatch, *, save: bool = True) -> DetectedBatch:
    numbers: Dict[str, Any] = {
        "peak_current": batch.peak_current,
        "time_to_peak_s": batch.time_to_peak_s,
        "duration_s": batch.duration_s,
        "energy_kwh": batch.energy_kwh,
    }
    batch.features = iot_features.compute_features(batch.curve or [], batch.curve_step_s, numbers)
    iot_standards.refresh_label(batch)
    prediction = predict(batch)
    batch.predicted_key = prediction["key"]
    batch.predicted_label = prediction["label"]
    batch.prediction_confidence = prediction["confidence"]
    batch.prediction_alternatives = prediction["alternatives"]
    batch.prediction_method = prediction["method"]
    judge(batch)
    if save:
        batch.save(update_fields=ANALYSIS_FIELDS)
    return batch

"""Analysis of a detected batch after detection: its label from the operator log, its pattern features,
the predicted compound and the judgement against the standard."""
from __future__ import annotations

from typing import Any, Dict

from . import iot_features, iot_standards
from .models import DetectedBatch


def analyze_batch(batch: DetectedBatch, *, save: bool = True) -> DetectedBatch:
    numbers: Dict[str, Any] = {
        "peak_current": batch.peak_current,
        "time_to_peak_s": batch.time_to_peak_s,
        "duration_s": batch.duration_s,
        "energy_kwh": batch.energy_kwh,
    }
    batch.features = iot_features.compute_features(batch.curve or [], batch.curve_step_s, numbers)
    iot_standards.refresh_label(batch)
    if save:
        batch.save(update_fields=["features", "log_run", "log_overlap", "label_compound", "updated_at"])
    return batch

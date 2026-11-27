from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import models
from django.db import transaction
from django.utils import timezone


try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

IST_TIMEZONE = ZoneInfo("Asia/Kolkata") if ZoneInfo else dt_timezone(timedelta(hours=5, minutes=30))


def compounding_business_date(now: datetime | None = None):
    """
    Compounding 'day' rolls over at 06:00 Asia/Kolkata (not midnight).

    If it's before 6am IST, we treat it as the previous day's plan.
    """
    current = now or timezone.now()
    local = current.astimezone(IST_TIMEZONE)
    cutoff = local.replace(hour=6, minute=0, second=0, microsecond=0)
    plan_date = local.date()
    if local < cutoff:
        plan_date = plan_date - timedelta(days=1)
    return plan_date


def default_compounding_plan_date():
    return compounding_business_date()


class CompoundingPlan(models.Model):
    """
    Daily compounding production plan.
    """

    plan_date = models.DateField(default=default_compounding_plan_date, db_index=True, unique=True)
    cjc_number = models.PositiveIntegerField()

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="compounding_plans"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-plan_date"]

    def __str__(self) -> str:
        return f"Compounding plan {self.plan_date} (CJC {self.cjc_number})"


class CompoundingLine(models.Model):
    """
    One compound line within a plan (shift-wise MB/FB/priority fields).
    """

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    compound_code = models.CharField(max_length=64, blank=True, default="")

    shift1_mb = models.PositiveIntegerField(null=True, blank=True)
    shift1_fb = models.PositiveIntegerField(null=True, blank=True)
    shift1_priority = models.CharField(max_length=32, blank=True, default="")

    shift2_mb = models.PositiveIntegerField(null=True, blank=True)
    shift2_fb = models.PositiveIntegerField(null=True, blank=True)
    shift2_priority = models.CharField(max_length=32, blank=True, default="")

    shift3_mb = models.PositiveIntegerField(null=True, blank=True)
    shift3_fb = models.PositiveIntegerField(null=True, blank=True)
    shift3_priority = models.CharField(max_length=32, blank=True, default="")

    actual_mb = models.PositiveIntegerField(default=0)
    actual_fb = models.PositiveIntegerField(default=0)
    actuals_saved = models.BooleanField(default=False)
    returned_to_planning_mb = models.PositiveIntegerField(default=0)
    returned_to_planning_fb = models.PositiveIntegerField(default=0)

    crin_request = models.ForeignKey(
        "compounding.CompoundingCRIN",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="planned_lines",
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["plan", "position"])]

    def __str__(self) -> str:
        return f"{self.plan.plan_date}: {self.compound_code or '(blank)'}"


class PendingMixingLine(models.Model):
    """
    Pending compounds for mixing list (right side of the board).
    """

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="pending")
    position = models.PositiveIntegerField(default=0)
    compound_code = models.CharField(max_length=64, blank=True, default="")
    total_batch = models.PositiveIntegerField(null=True, blank=True)
    crtn = models.CharField(max_length=64, blank=True, default="")
    weight_kg = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["plan", "position"])]

    def __str__(self) -> str:
        return f"{self.plan.plan_date}: pending {self.compound_code or '(blank)'}"


class CompoundingCRINRequest(models.Model):
    """
    One request raised by Production, covering one or more compounds.

    Each compound is a CompoundingCRIN, numbered {request_number}-1, -2, and so on.
    """

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="crin_requests")
    request_number = models.CharField(max_length=32, unique=True)
    # Stops a double click or browser resend from raising the same request twice.
    submission_id = models.UUIDField(unique=True, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_crin_requests",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return self.request_number


class CompoundingCRIN(models.Model):
    """
    One compound on a CRIN request.
    """

    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="crins")
    request = models.ForeignKey(
        CompoundingCRINRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
    )
    crin_number = models.CharField(max_length=32, unique=True)
    compound_folder = models.CharField(max_length=256, blank=True, default="")
    compound_code = models.CharField(max_length=64, blank=True, default="")
    quantity = models.PositiveIntegerField()
    # Original order. `quantity` shrinks when remaining batches return to planning.
    ordered_quantity = models.PositiveIntegerField(null=True, blank=True)
    fb_batch_size = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    mb_planning = models.PositiveIntegerField(null=True, blank=True)
    mb_msl = models.PositiveIntegerField(null=True, blank=True)
    fb_planning = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_crins",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["plan", "status", "created_at"])]

    def save(self, *args, **kwargs) -> None:
        if self._state.adding and self.ordered_quantity is None:
            self.ordered_quantity = self.quantity
        super().save(*args, **kwargs)

    @property
    def display_number(self) -> str:
        """The number people see: a multi-compound CRIN shares one number, without -1, -2, -3."""
        match = re.fullmatch(r"(CRIN-\d{4}-\d+)-\d+", self.crin_number or "")
        return match.group(1) if match else self.crin_number

    @property
    def order_quantity(self) -> int:
        return int(self.ordered_quantity if self.ordered_quantity is not None else self.quantity)

    def __str__(self) -> str:
        return f"{self.crin_number}: {self.compound_code} ({self.quantity})"


class CompoundingCJCSequence(models.Model):
    """
    Daily CJC number for the compounding board.

    Starts at 500 and increments by 1 each new compounding day (06:00 IST boundary).
    Stored per-year to keep the counter bounded and avoid clashes across years.
    """

    year = models.PositiveIntegerField(unique=True, db_index=True)
    last_number = models.PositiveIntegerField(default=499)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year"]
        indexes = [
            models.Index(fields=["year"]),
        ]

    def __str__(self) -> str:
        return f"compounding-cjc-{self.year}-{self.last_number}"


def next_compounding_cjc_number(*, year: int) -> int:
    """
    Allocate the next compounding-board CJC number for a given year.

    - Starts at 500
    - Increments sequentially (used once per business day when a new plan is created)
    """
    if not isinstance(year, int) or year <= 0:
        raise ValueError("year must be a positive integer")

    with transaction.atomic():
        seq, created = CompoundingCJCSequence.objects.select_for_update().get_or_create(
            year=year,
            defaults={"last_number": 499},
        )
        if created:
            existing_max = (
                CompoundingPlan.objects.filter(plan_date__year=year)
                .aggregate(max_value=models.Max("cjc_number"))
                .get("max_value")
            )
            try:
                existing_max_int = int(existing_max or 0)
            except (TypeError, ValueError):
                existing_max_int = 0
            if existing_max_int > int(seq.last_number or 0):
                seq.last_number = existing_max_int

        next_value = max(int(seq.last_number or 0) + 1, 500)
        seq.last_number = next_value
        seq.save(update_fields=["last_number", "updated_at"])
        return next_value


class CompoundingMBPlan(models.Model):
    """
    Compound-level MB planning for a plan date (aggregated across CRINs).

    Stores MSL + batch size so we can compute MB quantity/batches and send to board
    without forcing users to pick a CRIN.
    """

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="mb_plans")
    compound_code = models.CharField(max_length=64, db_index=True)
    msl = models.PositiveIntegerField(default=0)
    batch_size = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    # Snapshot of the latest computed values at save-time (derived from CRIN totals).
    total_quantity = models.PositiveIntegerField(default=0)
    mb_quantity = models.PositiveIntegerField(default=0)
    mb_batches = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_mb_plans",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["compound_code", "id"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "compound_code"], name="uniq_mb_plan_per_compound_per_day"),
        ]
        indexes = [
            models.Index(fields=["plan", "compound_code"]),
        ]

    def __str__(self) -> str:
        return f"{self.plan.plan_date}: MB {self.compound_code}"


class CompoundingIoTRun(models.Model):
    """
    Operator-entered production context for an IoT telemetry run.
    """

    QUALITY_GOOD = "good"
    QUALITY_OK = "ok"
    QUALITY_AVERAGE = "average"
    QUALITY_REJECTED = "rejected"

    QUALITY_CHOICES = [
        (QUALITY_GOOD, "Good"),
        (QUALITY_OK, "Ok"),
        (QUALITY_AVERAGE, "Average"),
        (QUALITY_REJECTED, "Rejected"),
    ]

    BATCH_MB = "mb"
    BATCH_FB = "fb"
    BATCH_TYPE_CHOICES = [
        (BATCH_MB, "MB"),
        (BATCH_FB, "FB"),
    ]

    REJECTION_REASON_CHOICES = [
        ("scorch", "Scorch"),
        ("poor_dispersion", "Poor dispersion"),
        ("hardness", "Hardness off"),
        ("weight", "Weight off"),
        ("other", "Other"),
    ]

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="iot_runs")
    machine_id = models.CharField(max_length=64, db_index=True, default="")
    compound_code = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField()
    # Empty while a batch started with "Start batch" is still on the machine.
    ended_at = models.DateTimeField(null=True, blank=True)
    # Empty until the batch is ended and graded.
    stock_quality = models.CharField(max_length=16, choices=QUALITY_CHOICES, blank=True, default=QUALITY_GOOD)
    batch_type = models.CharField(max_length=2, choices=BATCH_TYPE_CHOICES, blank=True, default="")
    batch_weight_kg = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    crin = models.ForeignKey(
        CompoundingCRIN,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="iot_runs",
    )
    rejection_reason = models.CharField(max_length=32, choices=REJECTION_REASON_CHOICES, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_iot_runs",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["plan", "compound_code"]),
            models.Index(fields=["machine_id", "started_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.plan.plan_date}: {self.compound_code} ({self.machine_id or 'machine'})"


class IoTSample(models.Model):
    """One telemetry sample from a machine (~every 2 s), kept so any past day or batch can be replayed.

    Only the named electrical and temperature values are stored; the raw register block stays in the IoT
    bridge's own store and in its raw export.
    """

    machine_id = models.CharField(max_length=64)
    ts_ms = models.BigIntegerField(help_text="Sample time, Unix epoch milliseconds")
    avg_current = models.FloatField(null=True, blank=True)
    t_watt = models.FloatField(null=True, blank=True)
    t_kwh_import = models.FloatField(null=True, blank=True)
    r_irms = models.FloatField(null=True, blank=True)
    y_irms = models.FloatField(null=True, blank=True)
    b_irms = models.FloatField(null=True, blank=True)
    avg_pf = models.FloatField(null=True, blank=True)
    a_vrms_ll = models.FloatField(null=True, blank=True)
    frequency = models.FloatField(null=True, blank=True)
    ch1_temperature = models.FloatField(null=True, blank=True)
    ch2_temperature = models.FloatField(null=True, blank=True)

    VALUE_FIELDS = (
        "avg_current",
        "t_watt",
        "t_kwh_import",
        "r_irms",
        "y_irms",
        "b_irms",
        "avg_pf",
        "a_vrms_ll",
        "frequency",
        "ch1_temperature",
        "ch2_temperature",
    )

    class Meta:
        ordering = ["machine_id", "ts_ms"]
        constraints = [
            # Also the (machine_id, ts) index every range read uses; it makes re-syncs idempotent.
            models.UniqueConstraint(fields=["machine_id", "ts_ms"], name="uniq_iot_sample_per_machine_ts"),
        ]

    def __str__(self) -> str:
        return f"{self.machine_id} @ {self.ts_ms}"



class ProcessGroup(models.Model):
    """Compounds whose current patterns cannot be told apart; they share one standard and one prediction."""

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ProcessGroupCompound(models.Model):
    """A compound's process group (compounds are master-sheet codes, so this is the compound's FK)."""

    process_group = models.ForeignKey(ProcessGroup, on_delete=models.CASCADE, related_name="compounds")
    compound_code = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["compound_code"]

    def __str__(self) -> str:
        return f"{self.compound_code} -> {self.process_group}"


class StandardCurve(models.Model):
    """The standard current graph for one compound, or for one process group, built from normal batches.

    `points`: [{"t": seconds after load, "median", "lower", "upper"}] every `step_s` seconds; the band is the
    5th-95th percentile (or mean +/- 2 sigma). `stats`: {"duration_s"|"energy_kwh"|"peak_current"|
    "time_to_peak_s"|"steady_len_s"|"steady_mean": {"median", "low", "high"}}.
    """

    # "compound:<code>" or "group:<id>"
    key = models.CharField(max_length=100, db_index=True)
    compound_code = models.CharField(max_length=64, blank=True, default="")
    process_group = models.ForeignKey(
        ProcessGroup, on_delete=models.CASCADE, null=True, blank=True, related_name="standards"
    )
    version = models.PositiveIntegerField()
    built_from_n = models.PositiveIntegerField()
    source_batch_ids = models.JSONField(default=list, blank=True)
    step_s = models.PositiveIntegerField(default=10)
    band_method = models.CharField(max_length=12, default="percentile")
    points = models.JSONField(default=list)
    stats = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["key", "-version"]
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="uniq_standard_version_per_key")]

    def __str__(self) -> str:
        return f"{self.label} v{self.version}"

    @property
    def label(self) -> str:
        return self.process_group.name if self.process_group_id else self.compound_code


class DetectedBatch(models.Model):
    """A batch the machine ran, found in the current trace (compounding.iot_segments).

    Kept apart from the operator's log (CompoundingIoTRun): the two are compared in the reconciliation
    table, and the log is only changed when a supervisor accepts the machine's times.
    """

    machine_id = models.CharField(max_length=64)
    start_at = models.DateTimeField()
    # The start of the next segment; for a batch still on the machine, the latest sample so far.
    end_at = models.DateTimeField()
    is_open = models.BooleanField(default=False, help_text="Still running when last detected")
    load_at = models.DateTimeField(null=True, blank=True, help_text="First sample above the load threshold")
    load_threshold_a = models.FloatField(null=True, blank=True)
    duration_s = models.PositiveIntegerField(default=0)
    energy_kwh = models.FloatField(null=True, blank=True)
    avg_current = models.FloatField(null=True, blank=True)
    peak_current = models.FloatField(null=True, blank=True)
    time_to_peak_s = models.PositiveIntegerField(null=True, blank=True)
    # avg_current every curve_step_s seconds from load_at (or start_at when no load point was found).
    curve = models.JSONField(default=list, blank=True)
    curve_step_s = models.PositiveIntegerField(default=10)
    # Pattern features: steady_mean, steady_len_s, dips, peak_current, time_to_peak_s, duration_s, energy_kwh.
    features = models.JSONField(default=dict, blank=True)

    # The operator log with the largest overlap, and how much they overlap (intersection over union).
    log_run = models.ForeignKey(
        "CompoundingIoTRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="detected_batches"
    )
    log_overlap = models.FloatField(null=True, blank=True)
    # A supervisor's tag (one-time seeding of the reference set); wins over the log.
    tagged_compound = models.CharField(max_length=64, blank=True, default="")
    tagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    tagged_at = models.DateTimeField(null=True, blank=True)
    # The compound this batch is known to be: the tag, else the log's compound when they overlap >= 70 %.
    label_compound = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Left out of standards and of the classifier's reference set (mis-tagged or abnormal).
    excluded = models.BooleanField(default=False)
    excluded_reason = models.CharField(max_length=200, blank=True, default="")

    # Pattern identification: the nearest standard (DTW), or kNN on the features once enough batches exist.
    predicted_key = models.CharField(max_length=100, blank=True, default="")
    predicted_label = models.CharField(max_length=80, blank=True, default="")
    prediction_confidence = models.FloatField(null=True, blank=True)
    prediction_alternatives = models.JSONField(default=list, blank=True)  # [{"key", "label", "confidence"}]
    prediction_method = models.CharField(max_length=8, blank=True, default="")

    # Judgement against a standard (pattern only).
    MATCH_OK = "matches"
    MATCH_NO = "doesnt_match"
    MATCH_NO_STANDARD = "no_standard"
    MATCH_CHOICES = [
        (MATCH_OK, "Matches standard"),
        (MATCH_NO, "Doesn't match"),
        (MATCH_NO_STANDARD, "No standard yet"),
    ]
    judged_key = models.CharField(max_length=100, blank=True, default="")
    judged_label = models.CharField(max_length=80, blank=True, default="")
    standard = models.ForeignKey(
        "StandardCurve", on_delete=models.SET_NULL, null=True, blank=True, related_name="judged_batches"
    )
    match_pct = models.FloatField(null=True, blank=True)
    dtw_distance = models.FloatField(null=True, blank=True)
    energy_dev_pct = models.FloatField(null=True, blank=True)
    duration_dev_pct = models.FloatField(null=True, blank=True)
    peak_time_dev_s = models.IntegerField(null=True, blank=True)
    match_result = models.CharField(max_length=12, choices=MATCH_CHOICES, blank=True, default="")
    reasons = models.JSONField(default=list, blank=True)  # at most two short strings

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["machine_id", "start_at"]
        indexes = [models.Index(fields=["machine_id", "start_at"])]

    def __str__(self) -> str:
        return f"{self.machine_id} batch @ {self.start_at:%Y-%m-%d %H:%M}"

    @property
    def batch_id(self) -> str:
        return f"D{self.pk:06d}" if self.pk else ""


class MachineSegment(models.Model):
    """One stretch of a machine's day in a single state; consecutive segments cover the day without gaps."""

    STATE_OFF = "off"
    STATE_IDLE = "idle"
    STATE_BATCH = "batch"
    STATE_NO_DATA = "no_data"
    STATE_CHOICES = [
        (STATE_OFF, "Off"),
        (STATE_IDLE, "Idle"),
        (STATE_BATCH, "Batch"),
        (STATE_NO_DATA, "No data"),
    ]

    machine_id = models.CharField(max_length=64)
    state = models.CharField(max_length=8, choices=STATE_CHOICES)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    is_open = models.BooleanField(default=False)
    detected_batch = models.ForeignKey(
        DetectedBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="segments"
    )

    class Meta:
        ordering = ["machine_id", "start_at"]
        indexes = [models.Index(fields=["machine_id", "start_at"])]

    def __str__(self) -> str:
        return f"{self.machine_id} {self.state} {self.start_at:%H:%M}-{self.end_at:%H:%M}"

    @property
    def duration_s(self) -> int:
        return max(int((self.end_at - self.start_at).total_seconds()), 0)



class IoTRunCorrection(models.Model):
    """Audit trail: a supervisor replaced an operator log's times with the machine's detected times."""

    run = models.ForeignKey("CompoundingIoTRun", on_delete=models.CASCADE, related_name="corrections")
    detected_batch = models.ForeignKey(
        DetectedBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="corrections"
    )
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    corrected_at = models.DateTimeField(default=timezone.now)
    verdict = models.CharField(max_length=32, blank=True, default="")
    old_started_at = models.DateTimeField()
    old_ended_at = models.DateTimeField(null=True, blank=True)
    new_started_at = models.DateTimeField()
    new_ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-corrected_at"]

    def __str__(self) -> str:
        return f"Correction of run {self.run_id} at {self.corrected_at:%Y-%m-%d %H:%M}"


class CompoundingStockEntry(models.Model):
    """
    One stock movement. A compound's balance is the sum of its entries.

    Entries are never edited: a correction is recorded as a new stock count.
    """

    STOCK_MB = "mb"
    STOCK_FB = "fb"
    STOCK_CHOICES = [
        (STOCK_MB, "MB"),
        (STOCK_FB, "FB"),
    ]

    ENTRY_OPENING = "opening"
    ENTRY_STOCK_COUNT = "stock_count"
    ENTRY_MB_MADE = "mb_made"
    ENTRY_MB_USED = "mb_used"
    ENTRY_FB_MADE = "fb_made"
    ENTRY_FB_ISSUED = "fb_issued"
    ENTRY_CHOICES = [
        (ENTRY_OPENING, "Opening stock"),
        (ENTRY_STOCK_COUNT, "Stock count from Excel"),
        (ENTRY_MB_MADE, "MB made"),
        (ENTRY_MB_USED, "MB used in FB"),
        (ENTRY_FB_MADE, "FB made"),
        (ENTRY_FB_ISSUED, "FB issued to Production"),
    ]

    compound_key = models.CharField(max_length=64, db_index=True)
    compound_code = models.CharField(max_length=64)
    stock_type = models.CharField(max_length=2, choices=STOCK_CHOICES)
    entry_type = models.CharField(max_length=16, choices=ENTRY_CHOICES)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    batches = models.PositiveIntegerField(null=True, blank=True)
    batch_weight = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    plan = models.ForeignKey(
        CompoundingPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_entries",
    )
    line = models.ForeignKey(
        CompoundingLine,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_entries",
    )
    crin = models.ForeignKey(
        CompoundingCRIN,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_entries",
    )
    note = models.CharField(max_length=255, blank=True, default="")
    # Set for movements typed in the production record, which have no board line or plan.
    movement_date = models.DateField(null=True, blank=True)
    # Nullable so the previous app version can still save entries during a deploy or rollback.
    record_source = models.CharField(max_length=20, null=True, blank=True, default="", db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_stock_entries",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["compound_key", "stock_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["line", "entry_type"],
                condition=models.Q(line__isnull=False),
                name="uniq_stock_entry_per_line_and_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.compound_code} {self.stock_type}: {self.entry_type} {self.quantity}"


class CompoundingMasterSheet(models.Model):
    """
    Editable spreadsheet snapshot uploaded from a master Excel file for a plan.
    """

    plan = models.OneToOneField(CompoundingPlan, on_delete=models.CASCADE, related_name="master_sheet")
    original_filename = models.CharField(max_length=255, blank=True, default="")
    worksheet_name = models.CharField(max_length=128, blank=True, default="")
    headers = models.JSONField(default=list, blank=True)
    rows = models.JSONField(default=list, blank=True)
    base_headers = models.JSONField(default=list, blank=True)
    base_rows = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_master_sheets",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return f"{self.plan.plan_date}: {self.original_filename or 'master-sheet'}"


class CompoundingRecordMonth(models.Model):
    """
    A month imported from the Excel production record.

    Imported months are history for reports only and never change the stock ledger.
    """

    month = models.DateField(unique=True)
    source_filename = models.CharField(max_length=255, blank=True, default="")
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_record_imports",
    )
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-month"]

    def __str__(self) -> str:
        return f"Production record {self.month:%B %Y}"


class CompoundingRecordRow(models.Model):
    """One compound's batches and weights in an imported month."""

    record = models.ForeignKey(CompoundingRecordMonth, on_delete=models.CASCADE, related_name="rows")
    position = models.PositiveIntegerField(default=0)
    compound_code = models.CharField(max_length=64)
    compound_key = models.CharField(max_length=64)
    mb_weight = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    chemical_weight = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    previous_balance = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    opening_batches = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    production_batches = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    daily_batches = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["record", "compound_key"], name="uniq_record_row_per_compound"),
        ]

    def __str__(self) -> str:
        return f"{self.record.month:%b %Y}: {self.compound_code}"


class CompoundingRecordValue(models.Model):
    """Values typed in the production record for a live month, on top of saved board actuals."""

    month = models.DateField()
    compound_key = models.CharField(max_length=64)
    compound_code = models.CharField(max_length=64)
    mb_weight = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    chemical_weight = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    previous_balance = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    opening_batches = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    production_batches = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    daily_batches = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["month", "compound_code"]
        constraints = [
            models.UniqueConstraint(fields=["month", "compound_key"], name="uniq_record_value_per_month_compound"),
        ]

    def __str__(self) -> str:
        return f"{self.month:%b %Y}: {self.compound_code}"


class CompoundingRecordChange(models.Model):
    """Who changed which production record value."""

    month = models.DateField(db_index=True)
    compound_code = models.CharField(max_length=64, blank=True, default="")
    field = models.CharField(max_length=32)
    day = models.PositiveSmallIntegerField(null=True, blank=True)
    old_value = models.CharField(max_length=32, blank=True, default="")
    new_value = models.CharField(max_length=32, blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_record_changes",
    )
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self) -> str:
        return f"{self.month:%b %Y} {self.compound_code} {self.field}"


class CompoundingDailyScrap(models.Model):
    """Total compounding scrap in kg for one production day."""

    date = models.DateField(unique=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="compounding_scrap_entries",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["date"]

    def __str__(self) -> str:
        return f"{self.date}: {self.quantity} kg scrap"

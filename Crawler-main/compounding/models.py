from __future__ import annotations

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

    plan = models.ForeignKey(CompoundingPlan, on_delete=models.CASCADE, related_name="iot_runs")
    machine_id = models.CharField(max_length=64, db_index=True, default="")
    compound_code = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    stock_quality = models.CharField(max_length=16, choices=QUALITY_CHOICES, default=QUALITY_GOOD)
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
    record_source = models.CharField(max_length=20, blank=True, default="", db_index=True)
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

"""SchoolEnrollmentCensus split out of apps.billing.models (a single >700-line module) by
responsibility, per apps/core/tests/test_code_size.py's own rule - the same convention
apps.billing.cycle already follows. Imported back into models.py so Django's app registry
still discovers it there; apps.billing.census holds the behavior that reads and writes it.
"""

import uuid

from django.conf import settings
from django.db import models

from .models import BillingInvoice, CensusStatus, SchoolBillingMeterSnapshot


class SchoolEnrollmentCensus(models.Model):
    """One school's authoritative, pricing-grade enrollment count for one billing period
    (apps.billing.census, Phase D Stage 3 Task 1) - distinct from the raw, continuously
    updated SchoolBillingMeterSnapshot it is taken from.

    status progresses OPEN -> FROZEN (a real, non-zero census existed at the configured
    cutoff) or OPEN -> NEEDS_REVIEW (the cutoff passed, or pricing proved undefined, with no
    usable count - flagged for an operator, never silently invoiced). A correction never
    mutates a FROZEN/NEEDS_REVIEW row's own recorded count or source: it flips that row's
    status to CORRECTED and creates a new current row referencing it via corrected_from, so
    the full history survives and a BillingInvoice already issued from the superseded row is
    never silently rewritten (see correct_school_census()).

    With no row at all for a (school, period), nothing about this model has fired yet - the
    cycle still prices directly from the latest authoritative meter, unchanged from before
    this model existed. That remains true platform-wide until a plan's BillingCyclePolicy
    actually sets census_cutoff_days.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        "schools.School",
        on_delete=models.CASCADE,
        related_name="enrollment_censuses",
    )
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=CensusStatus.choices,
        default=CensusStatus.OPEN,
    )
    # Recorded explicitly at the moment this row is created, so a later change to the
    # plan's census_cutoff_days policy never retroactively alters what already happened for
    # this period.
    cutoff_at = models.DateTimeField(null=True, blank=True)
    billable_student_count = models.PositiveIntegerField(null=True, blank=True)
    source_snapshot = models.ForeignKey(
        SchoolBillingMeterSnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # An explicit, voluntary freeze requested before the ordinary cutoff (Task 2's early
    # subscription purchase) - never set by the ordinary cutoff-driven freeze path.
    is_early = models.BooleanField(default=False)
    frozen_at = models.DateTimeField(null=True, blank=True)
    exception_reason = models.CharField(max_length=250, blank=True)
    invoice = models.ForeignKey(
        BillingInvoice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="census_records",
    )
    corrected_from = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrections",
    )
    correction_reason = models.TextField(blank=True)
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    corrected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["school", "period_start", "period_end", "-created_at"],
                name="billing_census_period_idx",
            ),
            models.Index(
                fields=["status"],
                name="billing_census_status_idx",
            ),
        ]

    def __str__(self):
        return f"{self.school} · {self.period_start:%Y-%m-%d} · {self.status}"

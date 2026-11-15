"""Commercial pricing configuration: fixed brackets, enterprise overrides, operator
discounts, and the audit trail for all of them. Split out of models.py (this project's
per-file line limit) - imported back there so Django's app registry still discovers
these as part of apps.billing.models, exactly like every other model in that file."""

import uuid

from django.conf import settings
from django.db import models


class PricingBracket(models.Model):
    """One fixed-fee tier in SkulGen's per-school subscription pricing.

    Supersedes per-student billing for every invoice issued after it takes effect
    (Plan.student_unit_amount_minor and already-issued invoices keep their original
    meaning and are never reinterpreted). A school's fixed fee for a period is the
    single bracket whose [min_students, max_students] contains its authoritative
    billable enrollment and whose [effective_from, effective_until) contains the
    measurement date. Brackets are platform-managed only - no school- or
    organization-facing endpoint writes this table. A school whose enrollment
    exceeds every configured bracket needs an EnterpriseAgreement instead; none is
    assumed automatically.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    currency = models.CharField(max_length=3, default="NGN")
    min_students = models.PositiveIntegerField()
    max_students = models.PositiveIntegerField(null=True, blank=True)
    fixed_fee_minor = models.PositiveBigIntegerField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["effective_from", "min_students"]
        indexes = [
            models.Index(
                fields=["effective_from", "effective_until"],
                name="billing_bracket_effective_idx",
            )
        ]

    def __str__(self):
        upper = self.max_students if self.max_students is not None else "+"
        return f"{self.min_students}-{upper} students · {self.fixed_fee_minor} {self.currency}"


class EnterpriseAgreement(models.Model):
    """A custom, explicitly authorized fixed fee for one school above the standard
    bracket ceiling. Never generated automatically - a school above every configured
    bracket has no price until an operator records one here."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        "schools.School",
        on_delete=models.CASCADE,
        related_name="enterprise_agreements",
    )
    currency = models.CharField(max_length=3, default="NGN")
    fixed_fee_minor = models.PositiveBigIntegerField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    # The external commercial contract this price comes from - never invented, and blank
    # only for agreements recorded before this field existed (Phase D Stage 3, Task 6).
    contract_reference = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Distinct from created_by: the platform staff member who approved this specific
    # commercial exception, recorded explicitly rather than assumed from who merely entered
    # it (Task 6/7's "approval details" and "appropriate approval controls").
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from"]
        indexes = [
            models.Index(
                fields=["school", "effective_from", "effective_until"],
                name="billing_enterprise_school_idx",
            )
        ]

    def __str__(self):
        return f"{self.school} · enterprise · {self.fixed_fee_minor} {self.currency}"


class DiscountKind(models.TextChoices):
    PERCENT = "percent", "Percent off"
    FIXED = "fixed", "Fixed amount off"


class SchoolDiscount(models.Model):
    """An operator-negotiated discount off one school's future invoices - distinct from
    the automatic 10% annual-payment discount every annual payer already gets. Reachable
    only through apps.console, gated to platform staff, the same as EnterpriseAgreement:
    there is no path for a school's own staff to grant themselves one."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        "schools.School",
        on_delete=models.CASCADE,
        related_name="discounts",
    )
    kind = models.CharField(max_length=16, choices=DiscountKind.choices)
    percent_off = models.PositiveSmallIntegerField(null=True, blank=True)
    amount_off_minor = models.PositiveBigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="NGN")
    reason = models.TextField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from", "-id"]
        indexes = [
            models.Index(
                fields=["school", "effective_from", "effective_until"],
                name="billing_discount_school_idx",
            )
        ]

    def __str__(self):
        amount = f"{self.percent_off}%" if self.kind == DiscountKind.PERCENT else f"{self.amount_off_minor} {self.currency}"
        return f"{self.school} · {amount} off"


class PricingAuditEvent(models.Model):
    """Append-only record of who configured or changed commercial pricing, and when -
    platform-wide, not scoped to one organization (apps.organizations.OrganizationAuditEvent
    is the per-organization equivalent)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self):
        return f"{self.action} · {self.target_type}:{self.target_id}"

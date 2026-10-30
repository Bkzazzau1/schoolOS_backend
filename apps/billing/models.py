import uuid

from django.conf import settings
from django.db import models

from apps.organizations.models import Organization


class BillingInterval(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    ANNUAL = "annual", "Annual"
    TERM = "term", "Per term"
    CUSTOM = "custom", "Custom"


class SubscriptionStatus(models.TextChoices):
    TRIAL = "trial", "Trial"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past due"
    GRACE = "grace", "Grace period"
    RESTRICTED = "restricted", "Restricted"
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    PAID = "paid", "Paid"
    VOID = "void", "Void"
    UNCOLLECTIBLE = "uncollectible", "Uncollectible"


class PaymentAttemptStatus(models.TextChoices):
    INITIALIZED = "initialized", "Initialized"
    PENDING = "pending", "Pending"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class WebhookProcessingStatus(models.TextChoices):
    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    IGNORED = "ignored", "Ignored"
    FAILED = "failed", "Failed"


class Plan(models.Model):
    """Commercial plan definition, separate from any school tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default="NGN")
    billing_interval = models.CharField(
        max_length=16,
        choices=BillingInterval.choices,
        blank=True,
        default="",
    )
    base_amount_minor = models.PositiveBigIntegerField(default=0)
    student_unit_amount_minor = models.PositiveBigIntegerField(default=0)
    is_public = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class PlanEntitlement(models.Model):
    """One feature/limit included by a plan."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="entitlements",
    )
    code = models.SlugField(max_length=64)
    enabled = models.BooleanField(default=True)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "code"],
                name="unique_plan_entitlement_code",
            )
        ]

    def __str__(self):
        return f"{self.plan.code} · {self.code}"


class OrganizationSubscription(models.Model):
    """The organization's current SkulGen commercial state."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    plan = models.ForeignKey(
        Plan,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    status = models.CharField(
        max_length=16,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
    )
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    grace_ends_at = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    provider = models.CharField(max_length=32, blank=True)
    provider_customer_ref = models.CharField(max_length=128, blank=True)
    provider_subscription_ref = models.CharField(max_length=128, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="billing_sub_status_idx"),
            models.Index(
                fields=["provider", "provider_subscription_ref"],
                name="billing_provider_sub_idx",
            ),
        ]

    def __str__(self):
        plan = self.plan.code if self.plan_id else "no-plan"
        return f"{self.organization} · {plan} · {self.status}"


class SubscriptionEvent(models.Model):
    """Append-only subscription lifecycle history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        OrganizationSubscription,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event = models.CharField(max_length=64)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [
            models.Index(
                fields=["subscription", "-at"],
                name="billing_event_sub_at_idx",
            )
        ]


class UsageSnapshot(models.Model):
    """Immutable billing-meter observation for an organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="usage_snapshots",
    )
    subscription = models.ForeignKey(
        OrganizationSubscription,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="usage_snapshots",
    )
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    active_school_count = models.PositiveIntegerField(default=0)
    billable_student_count = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=64, default="manual")
    metadata = models.JSONField(default=dict, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at", "-id"]
        indexes = [
            models.Index(
                fields=["organization", "-captured_at"],
                name="billing_usage_org_at_idx",
            )
        ]


class BillingInvoice(models.Model):
    """An immutable-priced SkulGen SaaS invoice.

    Amount inputs are copied onto the invoice at issue time so later plan changes
    cannot rewrite historical charges.

    `school` is set for every invoice issued since the per-school independent billing
    change (Phase C): each active school in an organization gets its own invoice for a
    period, priced and paid independently of its siblings - paying school A's invoice
    never touches school B's. It is null only on invoices issued before that change,
    which combined every school's charge into one invoice with its own BillingInvoiceLine
    per school; those historical invoices are never rewritten or split retroactively.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="billing_invoices",
    )
    subscription = models.ForeignKey(
        OrganizationSubscription,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    school = models.ForeignKey(
        "schools.School",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="billing_invoices",
    )
    usage_snapshot = models.ForeignKey(
        UsageSnapshot,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    number = models.CharField(max_length=40, unique=True)
    currency = models.CharField(max_length=3)
    base_amount_minor = models.PositiveBigIntegerField(default=0)
    student_unit_amount_minor = models.PositiveBigIntegerField(default=0)
    billable_student_count = models.PositiveIntegerField(default=0)
    amount_due_minor = models.PositiveBigIntegerField()
    amount_paid_minor = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=16,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.OPEN,
    )
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(
                fields=["organization", "status", "-issued_at"],
                name="billing_inv_org_status_idx",
            ),
            models.Index(
                fields=["school", "period_start", "period_end"],
                name="billing_inv_school_period_idx",
            ),
        ]
        constraints = [
            # Database-backed, not just the application-level select_for_update() +
            # check-then-create that run_subscription_cycle already serializes through -
            # defense in depth against ever double-issuing one school's invoice for one period,
            # even from a future caller that bypasses that lock. Never restricts historical
            # invoices (school is null on those): SQL UNIQUE constraints already never treat two
            # NULLs as equal, so any number of null-school rows may share a period.
            models.UniqueConstraint(
                fields=["subscription", "school", "period_start", "period_end"],
                name="unique_school_period_invoice",
            ),
        ]

    def __str__(self):
        return f"{self.number} · {self.organization} · {self.status}"


class BillingInvoiceLine(models.Model):
    """One school's own charge within a multi-school organization's invoice.

    Pricing is determined separately for each school (never by combining every school's
    enrollment into one bracket), so every invoice - including a single-school organization's -
    carries one line per active school. Fields are copied at issue time, the same as
    BillingInvoice's own amount fields, so a later bracket-price change or re-enrollment never
    rewrites a line already issued.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(
        BillingInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    school = models.ForeignKey(
        "schools.School",
        on_delete=models.PROTECT,
        related_name="billing_invoice_lines",
    )
    billable_student_count = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    fixed_fee_minor = models.PositiveBigIntegerField()
    pricing_source = models.CharField(max_length=16)  # "bracket" or "enterprise"
    bracket_min_students = models.PositiveIntegerField(null=True, blank=True)
    bracket_max_students = models.PositiveIntegerField(null=True, blank=True)
    pricing_bracket_id = models.UUIDField(null=True, blank=True)
    enterprise_agreement_id = models.UUIDField(null=True, blank=True)
    #: Pre-discount amount and term count this line covers (annual payments only). For an
    #: ordinary per-term line, gross_fee_minor == fixed_fee_minor and discount_minor == 0.
    gross_fee_minor = models.PositiveBigIntegerField(default=0)
    discount_minor = models.PositiveBigIntegerField(default=0)
    term_count = models.PositiveSmallIntegerField(null=True, blank=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["school__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "school"],
                name="unique_billing_invoice_line_per_school",
            )
        ]

    def __str__(self):
        return f"{self.invoice.number} · {self.school} · {self.fixed_fee_minor} {self.currency}"


class PaymentAttempt(models.Model):
    """One server-created attempt to pay one invoice through a provider."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(
        BillingInvoice,
        on_delete=models.PROTECT,
        related_name="payment_attempts",
    )
    provider = models.CharField(max_length=32)
    reference = models.CharField(max_length=100, unique=True)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=16,
        choices=PaymentAttemptStatus.choices,
        default=PaymentAttemptStatus.INITIALIZED,
    )
    checkout_url = models.URLField(max_length=500, blank=True)
    access_code = models.CharField(max_length=160, blank=True)
    provider_transaction_id = models.CharField(max_length=128, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    failure_message = models.CharField(max_length=250, blank=True)
    provider_detail = models.JSONField(default=dict, blank=True)
    succeeded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["invoice", "status", "-created_at"],
                name="billing_pay_inv_status_idx",
            ),
            models.Index(
                fields=["provider", "provider_transaction_id"],
                name="billing_pay_provider_idx",
            ),
        ]

    def __str__(self):
        return f"{self.reference} · {self.provider} · {self.status}"


class ProviderWebhookEvent(models.Model):
    """Idempotency record for provider callbacks; raw payment payloads are not retained."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32)
    payload_hash = models.CharField(max_length=64)
    event_type = models.CharField(max_length=80, blank=True)
    provider_object_ref = models.CharField(max_length=128, blank=True)
    status = models.CharField(
        max_length=16,
        choices=WebhookProcessingStatus.choices,
        default=WebhookProcessingStatus.RECEIVED,
    )
    detail = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "payload_hash"],
                name="unique_provider_webhook_payload",
            )
        ]
        indexes = [
            models.Index(
                fields=["provider", "status", "-received_at"],
                name="billing_webhook_status_idx",
            )
        ]

    def __str__(self):
        return f"{self.provider} · {self.event_type} · {self.status}"


class BillingCyclePolicy(models.Model):
    """Explicit automatic-billing timings for one commercial plan.

    Timing fields are nullable on purpose. SkulGen will not silently invent a
    due date or escalation window; automatic invoicing is ready only when all
    required durations are configured by the platform operator.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.OneToOneField(
        Plan,
        on_delete=models.CASCADE,
        related_name="billing_cycle_policy",
    )
    automatic_invoicing_enabled = models.BooleanField(default=False)
    invoice_due_days = models.PositiveSmallIntegerField(null=True, blank=True)
    past_due_days = models.PositiveSmallIntegerField(null=True, blank=True)
    grace_days = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.plan.code} · automatic={self.automatic_invoicing_enabled}"


class SchoolBillingMeterSnapshot(models.Model):
    """Append-only school roster count supplied by an authoritative server source.

    This stores only the count needed for SaaS metering, not student records.
    The future canonical Student module should publish a new row whenever its
    active billable roster changes. Billing never derives this count from login
    memberships or client-entered invoice data.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    school = models.ForeignKey(
        "schools.School",
        on_delete=models.CASCADE,
        related_name="billing_meter_snapshots",
    )
    billable_student_count = models.PositiveIntegerField()
    source = models.CharField(max_length=64)
    source_version = models.CharField(max_length=128)
    authoritative = models.BooleanField(default=True)
    measured_at = models.DateTimeField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-measured_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "source", "source_version"],
                name="unique_school_billing_meter_version",
            )
        ]
        indexes = [
            models.Index(
                fields=["school", "authoritative", "-measured_at"],
                name="billing_meter_school_at_idx",
            )
        ]

    def __str__(self):
        return f"{self.school} · {self.billable_student_count} · {self.measured_at}"


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
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
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

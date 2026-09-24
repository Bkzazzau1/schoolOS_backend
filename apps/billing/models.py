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
    """The organization's current SchoolOS commercial state."""

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
    """An immutable-priced SchoolOS SaaS invoice for one usage observation.

    Amount inputs are copied onto the invoice at issue time so later plan changes
    cannot rewrite historical charges.
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
    usage_snapshot = models.OneToOneField(
        UsageSnapshot,
        on_delete=models.PROTECT,
        related_name="invoice",
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
            )
        ]

    def __str__(self):
        return f"{self.number} · {self.organization} · {self.status}"


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

    Timing fields are nullable on purpose. SchoolOS will not silently invent a
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

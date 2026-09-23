import uuid

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
    """One feature/limit included by a plan.

    New entitlements can be introduced without changing the subscription schema.
    ``limit_value`` is optional: for example, a school-provisioning entitlement
    may cap the total schools in an organization while an unlimited plan leaves
    it null.
    """

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
    """The organization's current SchoolOS commercial state.

    This record is intentionally account-level. It never grants a school role
    and never replaces ``schools.School`` as the operational tenant boundary.
    """

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
            models.Index(fields=["provider", "provider_subscription_ref"], name="billing_provider_sub_idx"),
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
    """Immutable billing-meter observation for an organization.

    ``billable_student_count`` is deliberately not inferred from login
    memberships. SchoolOS may have students who do not own accounts; the future
    meter must capture the authoritative student population from school data.
    """

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

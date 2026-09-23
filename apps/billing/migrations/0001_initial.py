import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Plan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.SlugField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("currency", models.CharField(default="NGN", max_length=3)),
                ("billing_interval", models.CharField(blank=True, choices=[("monthly", "Monthly"), ("annual", "Annual"), ("term", "Per term"), ("custom", "Custom")], default="", max_length=16)),
                ("base_amount_minor", models.PositiveBigIntegerField(default=0)),
                ("student_unit_amount_minor", models.PositiveBigIntegerField(default=0)),
                ("is_public", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="OrganizationSubscription",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("trial", "Trial"), ("active", "Active"), ("past_due", "Past due"), ("grace", "Grace period"), ("restricted", "Restricted"), ("suspended", "Suspended"), ("cancelled", "Cancelled")], default="active", max_length=16)),
                ("current_period_start", models.DateTimeField(blank=True, null=True)),
                ("current_period_end", models.DateTimeField(blank=True, null=True)),
                ("trial_ends_at", models.DateTimeField(blank=True, null=True)),
                ("grace_ends_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_at_period_end", models.BooleanField(default=False)),
                ("provider", models.CharField(blank=True, max_length=32)),
                ("provider_customer_ref", models.CharField(blank=True, max_length=128)),
                ("provider_subscription_ref", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="subscription", to="organizations.organization")),
                ("plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="subscriptions", to="billing.plan")),
            ],
        ),
        migrations.CreateModel(
            name="PlanEntitlement",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.SlugField(max_length=64)),
                ("enabled", models.BooleanField(default=True)),
                ("limit_value", models.PositiveIntegerField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entitlements", to="billing.plan")),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.CreateModel(
            name="SubscriptionEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event", models.CharField(max_length=64)),
                ("from_status", models.CharField(blank=True, max_length=16)),
                ("to_status", models.CharField(blank=True, max_length=16)),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("at", models.DateTimeField(auto_now_add=True)),
                ("subscription", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="billing.organizationsubscription")),
            ],
            options={"ordering": ["-at", "-id"]},
        ),
        migrations.CreateModel(
            name="UsageSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("period_start", models.DateTimeField(blank=True, null=True)),
                ("period_end", models.DateTimeField(blank=True, null=True)),
                ("active_school_count", models.PositiveIntegerField(default=0)),
                ("billable_student_count", models.PositiveIntegerField(blank=True, null=True)),
                ("source", models.CharField(default="manual", max_length=64)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("captured_at", models.DateTimeField(auto_now_add=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_snapshots", to="organizations.organization")),
                ("subscription", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="usage_snapshots", to="billing.organizationsubscription")),
            ],
            options={"ordering": ["-captured_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="planentitlement",
            constraint=models.UniqueConstraint(fields=("plan", "code"), name="unique_plan_entitlement_code"),
        ),
        migrations.AddIndex(
            model_name="organizationsubscription",
            index=models.Index(fields=["status"], name="billing_sub_status_idx"),
        ),
        migrations.AddIndex(
            model_name="organizationsubscription",
            index=models.Index(fields=["provider", "provider_subscription_ref"], name="billing_provider_sub_idx"),
        ),
        migrations.AddIndex(
            model_name="subscriptionevent",
            index=models.Index(fields=["subscription", "-at"], name="billing_event_sub_at_idx"),
        ),
        migrations.AddIndex(
            model_name="usagesnapshot",
            index=models.Index(fields=["organization", "-captured_at"], name="billing_usage_org_at_idx"),
        ),
    ]

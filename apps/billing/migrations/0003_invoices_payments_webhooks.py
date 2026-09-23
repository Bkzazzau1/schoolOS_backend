# Generated manually for the SchoolOS SaaS billing slice.

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("billing", "0002_seed_standard_plan"),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingInvoice",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("number", models.CharField(max_length=40, unique=True)),
                ("currency", models.CharField(max_length=3)),
                ("base_amount_minor", models.PositiveBigIntegerField(default=0)),
                ("student_unit_amount_minor", models.PositiveBigIntegerField(default=0)),
                ("billable_student_count", models.PositiveIntegerField(default=0)),
                ("amount_due_minor", models.PositiveBigIntegerField()),
                ("amount_paid_minor", models.PositiveBigIntegerField(default=0)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("open", "Open"), ("paid", "Paid"), ("void", "Void"), ("uncollectible", "Uncollectible")], default="open", max_length=16)),
                ("period_start", models.DateTimeField(blank=True, null=True)),
                ("period_end", models.DateTimeField(blank=True, null=True)),
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("due_at", models.DateTimeField(blank=True, null=True)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="billing_invoices", to="organizations.organization")),
                ("subscription", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="invoices", to="billing.organizationsubscription")),
                ("usage_snapshot", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="invoice", to="billing.usagesnapshot")),
            ],
            options={"ordering": ["-issued_at", "-id"]},
        ),
        migrations.CreateModel(
            name="PaymentAttempt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=32)),
                ("reference", models.CharField(max_length=100, unique=True)),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(max_length=3)),
                ("status", models.CharField(choices=[("initialized", "Initialized"), ("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("cancelled", "Cancelled")], default="initialized", max_length=16)),
                ("checkout_url", models.URLField(blank=True, max_length=500)),
                ("access_code", models.CharField(blank=True, max_length=160)),
                ("provider_transaction_id", models.CharField(blank=True, max_length=128)),
                ("failure_message", models.CharField(blank=True, max_length=250)),
                ("provider_detail", models.JSONField(blank=True, default=dict)),
                ("succeeded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("initiated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payment_attempts", to="billing.billinginvoice")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ProviderWebhookEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=32)),
                ("payload_hash", models.CharField(max_length=64)),
                ("event_type", models.CharField(blank=True, max_length=80)),
                ("provider_object_ref", models.CharField(blank=True, max_length=128)),
                ("status", models.CharField(choices=[("received", "Received"), ("processed", "Processed"), ("ignored", "Ignored"), ("failed", "Failed")], default="received", max_length=16)),
                ("detail", models.JSONField(blank=True, default=dict)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-received_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="billinginvoice",
            index=models.Index(fields=["organization", "status", "-issued_at"], name="billing_inv_org_status_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentattempt",
            index=models.Index(fields=["invoice", "status", "-created_at"], name="billing_pay_inv_status_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentattempt",
            index=models.Index(fields=["provider", "provider_transaction_id"], name="billing_pay_provider_idx"),
        ),
        migrations.AddConstraint(
            model_name="providerwebhookevent",
            constraint=models.UniqueConstraint(fields=("provider", "payload_hash"), name="unique_provider_webhook_payload"),
        ),
        migrations.AddIndex(
            model_name="providerwebhookevent",
            index=models.Index(fields=["provider", "status", "-received_at"], name="billing_webhook_status_idx"),
        ),
    ]

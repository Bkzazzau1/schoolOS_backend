# Generated manually for the SchoolOS automatic billing-cycle slice.

import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_invoices_payments_webhooks"),
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="BillingCyclePolicy",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("automatic_invoicing_enabled", models.BooleanField(default=False)),
                ("invoice_due_days", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("past_due_days", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("grace_days", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "plan",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_cycle_policy",
                        to="billing.plan",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SchoolBillingMeterSnapshot",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("billable_student_count", models.PositiveIntegerField()),
                ("source", models.CharField(max_length=64)),
                ("source_version", models.CharField(max_length=128)),
                ("authoritative", models.BooleanField(default=True)),
                ("measured_at", models.DateTimeField()),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "school",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="billing_meter_snapshots",
                        to="schools.school",
                    ),
                ),
            ],
            options={"ordering": ["-measured_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="schoolbillingmetersnapshot",
            constraint=models.UniqueConstraint(
                fields=("school", "source", "source_version"),
                name="unique_school_billing_meter_version",
            ),
        ),
        migrations.AddIndex(
            model_name="schoolbillingmetersnapshot",
            index=models.Index(
                fields=["school", "authoritative", "-measured_at"],
                name="billing_meter_school_at_idx",
            ),
        ),
    ]

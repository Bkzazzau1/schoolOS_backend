from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("alumni", "0002_alumni_review_fields"),
        ("schools", "0004_alter_membership_role_add_alumni"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlumniVerificationEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event",
                    models.CharField(
                        choices=[
                            ("transitioned", "Transitioned"),
                            ("submitted", "Submitted"),
                            ("resubmitted", "Resubmitted"),
                            ("verified", "Verified"),
                            ("rejected", "Rejected"),
                        ],
                        max_length=20,
                    ),
                ),
                ("note", models.CharField(blank=True, max_length=500)),
                ("at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="alumni_verification_events",
                        to="schools.membership",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="verification_events",
                        to="alumni.alumniprofile",
                    ),
                ),
            ],
            options={"ordering": ["at", "id"]},
        ),
    ]

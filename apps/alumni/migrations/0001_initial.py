from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("schools", "0004_alter_membership_role_add_alumni"),
    ]

    operations = [
        migrations.CreateModel(
            name="AlumniProfile",
            fields=[
                (
                    "membership",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="alumni_profile",
                        serialize=False,
                        to="schools.membership",
                    ),
                ),
                ("original_student_reference", models.CharField(blank=True, max_length=120)),
                ("admission_number", models.CharField(blank=True, max_length=80)),
                ("graduation_year", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("graduation_set", models.CharField(blank=True, max_length=120)),
                (
                    "verification_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("verified", "Verified"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("profession", models.CharField(blank=True, max_length=160)),
                ("organisation", models.CharField(blank=True, max_length=200)),
                ("location_text", models.CharField(blank=True, max_length=160)),
                ("bio", models.TextField(blank=True)),
                ("directory_visible", models.BooleanField(default=False)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "school",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="alumni_profiles",
                        to="schools.school",
                    ),
                ),
                (
                    "verified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="verified_alumni_profiles",
                        to="schools.membership",
                    ),
                ),
            ],
            options={"ordering": ["-graduation_year", "membership_id"]},
        ),
        migrations.AddConstraint(
            model_name="alumniprofile",
            constraint=models.UniqueConstraint(
                condition=~models.Q(admission_number=""),
                fields=("school", "admission_number"),
                name="unique_alumni_admission_number_per_school",
            ),
        ),
    ]

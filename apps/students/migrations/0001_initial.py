# Canonical SchoolOS student roster, admissions and enrollment domain.

import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdmissionApplication",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("reference", models.CharField(max_length=128)),
                ("applicant_name", models.CharField(max_length=240)),
                ("section", models.CharField(max_length=80)),
                ("proposed_class", models.CharField(max_length=120)),
                ("guardian_name", models.CharField(max_length=200)),
                ("guardian_phone", models.CharField(max_length=40)),
                ("stage", models.CharField(choices=[("newApplication", "New"), ("documents", "Documents"), ("screening", "Screening"), ("offer", "Offer"), ("accepted", "Accepted"), ("registered", "Registered")], default="newApplication", max_length=24)),
                ("source", models.CharField(default="School website", max_length=80)),
                ("submitted_label", models.CharField(blank=True, max_length=40)),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("birth_certificate", models.CharField(choices=[("received", "Received"), ("pending", "Pending")], default="pending", max_length=16)),
                ("previous_school_report", models.CharField(choices=[("received", "Received"), ("pending", "Pending")], default="pending", max_length=16)),
                ("guardian_identification", models.CharField(choices=[("received", "Received"), ("pending", "Pending")], default="pending", max_length=16)),
                ("document_request_queued", models.BooleanField(default=False)),
                ("closed_reason", models.CharField(blank=True, max_length=250)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="admission_applications", to="schools.school")),
            ],
            options={"ordering": ["-submitted_at", "-id"]},
        ),
        migrations.CreateModel(
            name="Student",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("admission_number", models.CharField(max_length=80)),
                ("student_code", models.CharField(max_length=80)),
                ("first_name", models.CharField(max_length=120)),
                ("surname", models.CharField(max_length=120)),
                ("other_name", models.CharField(blank=True, max_length=160)),
                ("date_of_birth", models.DateField(blank=True, null=True)),
                ("gender", models.CharField(blank=True, max_length=40)),
                ("previous_school", models.CharField(blank=True, max_length=200)),
                ("address", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("transfer_pending", "Transfer pending"), ("transferred_out", "Transferred out"), ("graduated", "Graduated"), ("withdrawn", "Withdrawn"), ("inactive", "Inactive")], default="active", max_length=24)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="students", to="schools.school")),
            ],
            options={"ordering": ["surname", "first_name", "student_code"]},
        ),
        migrations.CreateModel(
            name="StudentRegistration",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("registration_id", models.CharField(max_length=128)),
                ("first_name", models.CharField(max_length=120)),
                ("surname", models.CharField(max_length=120)),
                ("other_name", models.CharField(blank=True, max_length=160)),
                ("date_of_birth", models.DateField(blank=True, null=True)),
                ("gender", models.CharField(blank=True, max_length=40)),
                ("academic_section", models.CharField(max_length=80)),
                ("proposed_class", models.CharField(max_length=120)),
                ("previous_school", models.CharField(blank=True, max_length=200)),
                ("address", models.TextField(blank=True)),
                ("admission_number", models.CharField(max_length=80)),
                ("student_code", models.CharField(max_length=80)),
                ("status", models.CharField(choices=[("in_progress", "Admission in progress"), ("active", "Active")], default="in_progress", max_length=16)),
                ("primary_guardian", models.CharField(max_length=200)),
                ("relationship", models.CharField(blank=True, max_length=60)),
                ("guardian_phone", models.CharField(max_length=40)),
                ("guardian_email", models.EmailField(blank=True, max_length=254)),
                ("family_account_ref", models.CharField(blank=True, max_length=160)),
                ("sibling_link", models.CharField(blank=True, max_length=160)),
                ("birth_certificate_status", models.CharField(blank=True, max_length=120)),
                ("previous_school_record_status", models.CharField(blank=True, max_length=120)),
                ("guardian_identification_status", models.CharField(blank=True, max_length=120)),
                ("finance_setup_status", models.CharField(blank=True, max_length=120)),
                ("transport_meal_status", models.CharField(blank=True, max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_registrations", to="schools.school")),
                ("source_applicant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="registrations", to="students.admissionapplication")),
                ("student", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="registration", to="students.student")),
            ],
            options={"ordering": ["-updated_at", "-id"]},
        ),
        migrations.CreateModel(
            name="GuardianLink",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("relationship", models.CharField(blank=True, max_length=60)),
                ("phone", models.CharField(max_length=40)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("is_primary", models.BooleanField(default=False)),
                ("family_account_ref", models.CharField(blank=True, max_length=160)),
                ("sibling_link", models.CharField(blank=True, max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="guardians", to="students.student")),
            ],
        ),
        migrations.CreateModel(
            name="StudentEnrollment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("academic_section", models.CharField(max_length=80)),
                ("class_name", models.CharField(max_length=120)),
                ("status", models.CharField(choices=[("active", "Active"), ("completed", "Completed"), ("transferred_out", "Transferred out"), ("graduated", "Graduated"), ("withdrawn", "Withdrawn")], default="active", max_length=24)),
                ("is_billable", models.BooleanField(default=True)),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_enrollments", to="schools.school")),
                ("source_registration", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="enrollments", to="students.studentregistration")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="enrollments", to="students.student")),
            ],
            options={"ordering": ["-started_at", "-id"]},
        ),
        migrations.CreateModel(
            name="StudentLifecycleEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("workflow", models.CharField(max_length=40)),
                ("change", models.CharField(blank=True, max_length=250)),
                ("from_class", models.CharField(blank=True, max_length=120)),
                ("to_class", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("completed", "Completed"), ("cancelled", "Cancelled")], default="pending", max_length=16)),
                ("requested_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("approved_by", models.CharField(blank=True, max_length=200)),
                ("records_pack_ready", models.BooleanField(default=False)),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_lifecycle_events", to="schools.school")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lifecycle_events", to="students.student")),
            ],
            options={"ordering": ["-requested_at", "-id"]},
        ),
        migrations.CreateModel(
            name="SchoolRosterRevision",
            fields=[
                ("school", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name="roster_revision", serialize=False, to="schools.school")),
                ("revision", models.PositiveBigIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="admissionapplication",
            constraint=models.UniqueConstraint(fields=("school", "reference"), name="unique_admission_reference_per_school"),
        ),
        migrations.AddIndex(
            model_name="admissionapplication",
            index=models.Index(fields=["school", "stage"], name="students_adm_stage_idx"),
        ),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.UniqueConstraint(fields=("school", "admission_number"), name="unique_student_admission_number_per_school"),
        ),
        migrations.AddConstraint(
            model_name="student",
            constraint=models.UniqueConstraint(fields=("school", "student_code"), name="unique_student_code_per_school"),
        ),
        migrations.AddIndex(
            model_name="student",
            index=models.Index(fields=["school", "status"], name="students_school_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="studentregistration",
            constraint=models.UniqueConstraint(fields=("school", "registration_id"), name="unique_registration_id_per_school"),
        ),
        migrations.AddConstraint(
            model_name="studentregistration",
            constraint=models.UniqueConstraint(condition=~models.Q(admission_number=""), fields=("school", "admission_number"), name="unique_registration_admission_per_school"),
        ),
        migrations.AddConstraint(
            model_name="studentregistration",
            constraint=models.UniqueConstraint(condition=~models.Q(student_code=""), fields=("school", "student_code"), name="unique_registration_student_code_per_school"),
        ),
        migrations.AddConstraint(
            model_name="guardianlink",
            constraint=models.UniqueConstraint(fields=("student", "phone"), name="unique_guardian_phone_per_student"),
        ),
        migrations.AddConstraint(
            model_name="studentenrollment",
            constraint=models.UniqueConstraint(condition=models.Q(status="active"), fields=("student",), name="one_active_enrollment_per_student"),
        ),
        migrations.AddIndex(
            model_name="studentenrollment",
            index=models.Index(fields=["school", "status", "is_billable"], name="students_enrollment_bill_idx"),
        ),
        migrations.AddConstraint(
            model_name="studentlifecycleevent",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="unique_lifecycle_external_id_per_school"),
        ),
        migrations.AddIndex(
            model_name="studentlifecycleevent",
            index=models.Index(fields=["school", "status"], name="students_lifecycle_status_idx"),
        ),
    ]

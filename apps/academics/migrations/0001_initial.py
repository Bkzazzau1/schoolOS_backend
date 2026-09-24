import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("students", "0003_student_guardian_account_links"),
    ]

    operations = [
        migrations.CreateModel(
            name="AcademicClass",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=60)),
                ("name", models.CharField(max_length=120)),
                ("section", models.CharField(max_length=80)),
                ("level_order", models.PositiveSmallIntegerField()),
                ("stream", models.CharField(blank=True, max_length=60)),
                ("is_terminal", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("next_class", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="previous_classes", to="academics.academicclass")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="academic_classes", to="schools.school")),
            ],
            options={"ordering": ["level_order", "name", "code"]},
        ),
        migrations.CreateModel(
            name="AcademicSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=120)),
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField()),
                ("status", models.CharField(choices=[("planned", "Planned"), ("active", "Active"), ("closed", "Closed")], default="planned", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="academic_sessions", to="schools.school")),
            ],
            options={"ordering": ["-starts_on", "-created_at"]},
        ),
        migrations.CreateModel(
            name="AcademicTerm",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=80)),
                ("sequence", models.PositiveSmallIntegerField()),
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField()),
                ("status", models.CharField(choices=[("planned", "Planned"), ("active", "Active"), ("closed", "Closed")], default="planned", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="terms", to="academics.academicsession")),
            ],
            options={"ordering": ["session__starts_on", "sequence", "starts_on"]},
        ),
        migrations.CreateModel(
            name="EnrollmentAcademicContext",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source", models.CharField(default="automatic", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("academic_class", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="enrollment_contexts", to="academics.academicclass")),
                ("enrollment", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="academic_context", to="students.studentenrollment")),
                ("entry_term", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="entry_enrollment_contexts", to="academics.academicterm")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="enrollment_contexts", to="academics.academicsession")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ProgressionBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("status", models.CharField(choices=[("draft", "Draft"), ("review", "Ready for review"), ("applied", "Applied"), ("cancelled", "Cancelled")], default="draft", max_length=16)),
                ("approved_by", models.CharField(blank=True, max_length=200)),
                ("note", models.TextField(blank=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("from_session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="outgoing_progression_batches", to="academics.academicsession")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="progression_batches", to="schools.school")),
                ("source_class", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="progression_batches", to="academics.academicclass")),
                ("to_session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="incoming_progression_batches", to="academics.academicsession")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="ProgressionDecision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("outcome", models.CharField(choices=[("promote", "Promote"), ("repeat", "Repeat"), ("transfer_out", "Transfer out"), ("graduate", "Graduate"), ("hold", "Hold")], max_length=20)),
                ("records_pack_ready", models.BooleanField(default=False)),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="decisions", to="academics.progressionbatch")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="progression_decisions", to="students.student")),
                ("target_class", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="incoming_progression_decisions", to="academics.academicclass")),
            ],
            options={"ordering": ["student__surname", "student__first_name", "student__student_code"]},
        ),
        migrations.AddConstraint(
            model_name="academicclass",
            constraint=models.UniqueConstraint(fields=("school", "code"), name="unique_academic_class_code_per_school"),
        ),
        migrations.AddConstraint(
            model_name="academicclass",
            constraint=models.UniqueConstraint(fields=("school", "name"), name="unique_academic_class_name_per_school"),
        ),
        migrations.AddIndex(
            model_name="academicclass",
            index=models.Index(fields=["school", "is_active", "level_order"], name="academics_class_order_idx"),
        ),
        migrations.AddConstraint(
            model_name="academicsession",
            constraint=models.UniqueConstraint(fields=("school", "code"), name="unique_academic_session_code_per_school"),
        ),
        migrations.AddConstraint(
            model_name="academicsession",
            constraint=models.UniqueConstraint(condition=Q(("status", "active")), fields=("school",), name="one_active_academic_session_per_school"),
        ),
        migrations.AddConstraint(
            model_name="academicterm",
            constraint=models.UniqueConstraint(fields=("session", "code"), name="unique_academic_term_code_per_session"),
        ),
        migrations.AddConstraint(
            model_name="academicterm",
            constraint=models.UniqueConstraint(fields=("session", "sequence"), name="unique_academic_term_sequence_per_session"),
        ),
        migrations.AddConstraint(
            model_name="academicterm",
            constraint=models.UniqueConstraint(condition=Q(("status", "active")), fields=("session",), name="one_active_academic_term_per_session"),
        ),
        migrations.AddConstraint(
            model_name="progressionbatch",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="unique_progression_batch_external_id_per_school"),
        ),
        migrations.AddConstraint(
            model_name="progressiondecision",
            constraint=models.UniqueConstraint(fields=("batch", "student"), name="unique_progression_decision_per_batch_student"),
        ),
    ]

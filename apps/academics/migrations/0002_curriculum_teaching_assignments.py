import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("academics", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Subject",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=120)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="academic_subjects", to="schools.school")),
            ],
            options={"ordering": ["name", "code"]},
        ),
        migrations.CreateModel(
            name="ClassSubject",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("requirement", models.CharField(choices=[("compulsory", "Compulsory"), ("elective", "Elective")], default="compulsory", max_length=16)),
                ("periods_per_week", models.PositiveSmallIntegerField(default=1)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("academic_class", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="session_subjects", to="academics.academicclass")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="class_subjects", to="academics.academicsession")),
                ("subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="class_offerings", to="academics.subject")),
            ],
            options={"ordering": ["academic_class__level_order", "subject__name"]},
        ),
        migrations.CreateModel(
            name="StudentSubjectSelection",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("selected", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="student_selections", to="academics.classsubject")),
                ("selected_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="subject_selections", to="students.student")),
            ],
            options={"ordering": ["student__surname", "student__first_name", "class_subject__subject__name"]},
        ),
        migrations.CreateModel(
            name="TeachingAssignment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("teacher_staff_id", models.CharField(max_length=128)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("class_subject", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="teaching_assignment", to="academics.classsubject")),
                ("teacher_membership", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="canonical_teaching_assignments", to="schools.membership")),
            ],
            options={"ordering": ["class_subject__academic_class__level_order", "class_subject__subject__name"]},
        ),
        migrations.CreateModel(
            name="TeachingAssignmentEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("from_teacher_staff_id", models.CharField(blank=True, max_length=128)),
                ("to_teacher_staff_id", models.CharField(max_length=128)),
                ("reason", models.TextField(blank=True)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="academics.teachingassignment")),
            ],
            options={"ordering": ["-occurred_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(fields=("school", "code"), name="unique_subject_code_per_school"),
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(fields=("school", "name"), name="unique_subject_name_per_school"),
        ),
        migrations.AddConstraint(
            model_name="classsubject",
            constraint=models.UniqueConstraint(fields=("session", "academic_class", "subject"), name="unique_subject_per_class_session"),
        ),
        migrations.AddIndex(
            model_name="classsubject",
            index=models.Index(fields=["session", "academic_class", "is_active"], name="acad_cls_subject_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="studentsubjectselection",
            constraint=models.UniqueConstraint(fields=("student", "class_subject"), name="unique_student_subject_selection"),
        ),
        migrations.AddIndex(
            model_name="teachingassignment",
            index=models.Index(fields=["teacher_membership", "is_active"], name="acad_teacher_assign_idx"),
        ),
    ]

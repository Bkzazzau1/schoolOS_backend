import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("academics", "0001_initial"),
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="Subject",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(max_length=40)),
                ("name", models.CharField(max_length=120)),
                ("short_name", models.CharField(blank=True, max_length=40)),
                ("section", models.CharField(blank=True, help_text="Optional section scope. Blank means the subject may be used across sections.", max_length=80)),
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
                ("academic_class", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="class_subjects", to="academics.academicclass")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="class_subjects", to="academics.academicsession")),
                ("subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="class_subjects", to="academics.subject")),
            ],
            options={"ordering": ["academic_class__level_order", "subject__name"]},
        ),
        migrations.CreateModel(
            name="CurriculumTopic",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sequence", models.PositiveSmallIntegerField()),
                ("title", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="topics", to="academics.classsubject")),
                ("term", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="curriculum_topics", to="academics.academicterm")),
            ],
            options={"ordering": ["term__sequence", "sequence", "title"]},
        ),
        migrations.CreateModel(
            name="TeachingAssignment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=64)),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("handover_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assigned_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="teaching_assignments", to="academics.classsubject")),
                ("previous_assignment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="handover_successors", to="academics.teachingassignment")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="teaching_assignments", to="schools.school")),
                ("teacher_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="academic_teaching_assignments", to="schools.membership")),
            ],
            options={"ordering": ["-started_at", "-created_at"]},
        ),
        migrations.CreateModel(
            name="StudentSubjectSelection",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("selected_at", models.DateTimeField(auto_now_add=True)),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="student_selections", to="academics.classsubject")),
                ("enrollment_context", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subject_selections", to="academics.enrollmentacademiccontext")),
                ("selected_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
            ],
            options={"ordering": ["class_subject__subject__name"]},
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(fields=("school", "code"), name="subj_school_code_uq"),
        ),
        migrations.AddConstraint(
            model_name="subject",
            constraint=models.UniqueConstraint(fields=("school", "name"), name="subj_school_name_uq"),
        ),
        migrations.AddIndex(
            model_name="subject",
            index=models.Index(fields=["school", "is_active", "name"], name="subj_school_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="classsubject",
            constraint=models.UniqueConstraint(fields=("session", "academic_class", "subject"), name="class_subject_uq"),
        ),
        migrations.AddIndex(
            model_name="classsubject",
            index=models.Index(fields=["session", "academic_class", "is_active"], name="class_subject_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="curriculumtopic",
            constraint=models.UniqueConstraint(fields=("class_subject", "term", "sequence"), name="curr_topic_seq_uq"),
        ),
        migrations.AddConstraint(
            model_name="teachingassignment",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="teach_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="teachingassignment",
            constraint=models.UniqueConstraint(condition=Q(("ended_at__isnull", True)), fields=("class_subject",), name="teach_active_subject_uq"),
        ),
        migrations.AddIndex(
            model_name="teachingassignment",
            index=models.Index(fields=["teacher_membership", "ended_at"], name="teach_member_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="studentsubjectselection",
            constraint=models.UniqueConstraint(fields=("enrollment_context", "class_subject"), name="student_elective_uq"),
        ),
    ]

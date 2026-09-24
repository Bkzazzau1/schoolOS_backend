# Generated manually for the canonical SchoolOS assignment authority.
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("academics", "0003_elective_selection_history"),
        ("students", "0003_student_guardian_account_links"),
    ]

    operations = [
        migrations.CreateModel(
            name="Assignment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("assignment_type", models.CharField(choices=[("homework", "Homework"), ("classwork", "Classwork"), ("project", "Project"), ("revision", "Revision")], default="homework", max_length=16)),
                ("state", models.CharField(choices=[("draft", "Draft"), ("published", "Published"), ("closed", "Closed")], default="draft", max_length=16)),
                ("title", models.CharField(blank=True, max_length=240)),
                ("instructions", models.TextField(blank=True)),
                ("due_at", models.DateTimeField(blank=True, null=True)),
                ("maximum_score", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("author_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="authored_assignments", to="schools.membership")),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignments", to="academics.classsubject")),
                ("closed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="closed_assignments", to="schools.membership")),
                ("curriculum_topic", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assignments", to="academics.curriculumtopic")),
                ("last_edited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="edited_assignments", to="schools.membership")),
                ("published_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="published_assignments", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="academic_assignments", to="schools.school")),
                ("term", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignments", to="academics.academicterm")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="AssignmentPublication",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision", models.PositiveIntegerField()),
                ("snapshot", models.JSONField(default=dict)),
                ("published_at", models.DateTimeField(auto_now_add=True)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="publications", to="assignments.assignment")),
                ("published_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignment_publications", to="schools.membership")),
            ],
            options={"ordering": ["-revision"]},
        ),
        migrations.CreateModel(
            name="AssignmentRecipient",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("student_code", models.CharField(max_length=80)),
                ("student_name", models.CharField(max_length=320)),
                ("admission_number", models.CharField(blank=True, max_length=80)),
                ("assigned_at", models.DateTimeField(auto_now_add=True)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recipients", to="assignments.assignment")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignment_recipients", to="students.student")),
            ],
            options={"ordering": ["student_name", "student_code"]},
        ),
        migrations.CreateModel(
            name="AssignmentSubmission",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("state", models.CharField(choices=[("draft", "Draft"), ("submitted", "Submitted"), ("returned", "Returned for revision"), ("graded", "Graded")], default="draft", max_length=16)),
                ("response_text", models.TextField(blank=True)),
                ("attempt_number", models.PositiveIntegerField(default=0)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("is_late", models.BooleanField(default=False)),
                ("score", models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True)),
                ("feedback", models.TextField(blank=True)),
                ("graded_at", models.DateTimeField(blank=True, null=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="submissions", to="assignments.assignment")),
                ("graded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="graded_assignment_submissions", to="schools.membership")),
                ("recipient", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="submission", to="assignments.assignmentrecipient")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignment_submissions", to="schools.school")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignment_submissions", to="students.student")),
            ],
            options={"ordering": ["-submitted_at", "student__surname", "student__first_name"]},
        ),
        migrations.CreateModel(
            name="AssignmentSubmissionVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("attempt_number", models.PositiveIntegerField()),
                ("assignment_revision", models.PositiveIntegerField(default=1)),
                ("snapshot", models.JSONField(default=dict)),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="versions", to="assignments.assignmentsubmission")),
            ],
            options={"ordering": ["-attempt_number"]},
        ),
        migrations.CreateModel(
            name="AssignmentGradeEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision", models.PositiveIntegerField()),
                ("action", models.CharField(max_length=16)),
                ("score", models.DecimalField(blank=True, decimal_places=2, max_digits=9, null=True)),
                ("feedback", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assignment_grade_events", to="schools.membership")),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="grade_events", to="assignments.assignmentsubmission")),
            ],
            options={"ordering": ["-revision"]},
        ),
        migrations.AddConstraint(model_name="assignment", constraint=models.UniqueConstraint(fields=("school", "external_id"), name="assign_external_uq")),
        migrations.AddIndex(model_name="assignment", index=models.Index(fields=["school", "state", "due_at"], name="assign_school_state_idx")),
        migrations.AddIndex(model_name="assignment", index=models.Index(fields=["class_subject", "term", "state"], name="assign_subject_term_idx")),
        migrations.AddConstraint(model_name="assignmentpublication", constraint=models.UniqueConstraint(fields=("assignment", "revision"), name="assign_pub_revision_uq")),
        migrations.AddConstraint(model_name="assignmentrecipient", constraint=models.UniqueConstraint(fields=("assignment", "student"), name="assign_recipient_uq")),
        migrations.AddIndex(model_name="assignmentrecipient", index=models.Index(fields=["student", "assignment"], name="assign_student_idx")),
        migrations.AddConstraint(model_name="assignmentsubmission", constraint=models.UniqueConstraint(fields=("school", "external_id"), name="assign_sub_external_uq")),
        migrations.AddConstraint(model_name="assignmentsubmission", constraint=models.UniqueConstraint(fields=("assignment", "student"), name="assign_sub_student_uq")),
        migrations.AddIndex(model_name="assignmentsubmission", index=models.Index(fields=["assignment", "state", "submitted_at"], name="assign_sub_state_idx")),
        migrations.AddIndex(model_name="assignmentsubmission", index=models.Index(fields=["student", "state"], name="assign_sub_student_idx")),
        migrations.AddConstraint(model_name="assignmentsubmissionversion", constraint=models.UniqueConstraint(fields=("submission", "attempt_number"), name="assign_sub_attempt_uq")),
        migrations.AddConstraint(model_name="assignmentgradeevent", constraint=models.UniqueConstraint(fields=("submission", "revision"), name="assign_grade_revision_uq")),
    ]

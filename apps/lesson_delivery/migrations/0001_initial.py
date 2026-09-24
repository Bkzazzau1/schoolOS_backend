import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("academics", "0003_elective_selection_history"),
        ("lesson_attendance", "0001_initial"),
        ("schools", "0005_school_organization_type_location"),
        ("timetable", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LessonPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("lesson_date", models.DateField()),
                ("state", models.CharField(choices=[("draft", "Draft"), ("submitted", "Submitted"), ("approved", "Approved"), ("needs_changes", "Needs changes")], default="draft", max_length=20)),
                ("objectives", models.TextField(blank=True)),
                ("starter", models.TextField(blank=True)),
                ("activities", models.TextField(blank=True)),
                ("assessment", models.TextField(blank=True)),
                ("resources", models.TextField(blank=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_comment", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("author_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="authored_lesson_plans", to="schools.membership")),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_plans", to="academics.classsubject")),
                ("curriculum_topic", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_plans", to="academics.curriculumtopic")),
                ("last_edited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="edited_lesson_plans", to="schools.membership")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_lesson_plans", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_plans", to="schools.school")),
                ("submitted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="submitted_lesson_plans", to="schools.membership")),
                ("timetable_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_plans", to="timetable.timetableentry")),
            ],
            options={
                "ordering": ["-lesson_date", "timetable_entry__starts_at", "external_id"],
            },
        ),
        migrations.CreateModel(
            name="LessonPlanReview",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("plan_version", models.PositiveIntegerField()),
                ("decision", models.CharField(choices=[("approved", "Approved"), ("needs_changes", "Needs changes")], max_length=20)),
                ("comment", models.TextField(blank=True)),
                ("reviewed_at", models.DateTimeField(auto_now_add=True)),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reviews", to="lesson_delivery.lessonplan")),
                ("reviewer_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_plan_review_events", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_plan_reviews", to="schools.school")),
            ],
            options={"ordering": ["-reviewed_at"]},
        ),
        migrations.CreateModel(
            name="LessonDeliveryRecord",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("lesson_date", models.DateField()),
                ("state", models.CharField(choices=[("draft", "Draft"), ("delivered", "Delivered")], default="draft", max_length=16)),
                ("reflection", models.TextField(blank=True)),
                ("homework", models.TextField(blank=True)),
                ("topic_completed", models.BooleanField(default=False)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attendance_register", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="lesson_delivery_records", to="lesson_attendance.lessonattendanceregister")),
                ("curriculum_topic", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="delivery_records", to="academics.curriculumtopic")),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="delivery_records", to="lesson_delivery.lessonplan")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_delivery_records", to="schools.school")),
                ("teacher_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_delivery_records", to="schools.membership")),
                ("timetable_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="delivery_records", to="timetable.timetableentry")),
            ],
            options={
                "ordering": ["-lesson_date", "timetable_entry__starts_at", "external_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="lessonplan",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="lesson_plan_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="lessonplan",
            constraint=models.UniqueConstraint(fields=("timetable_entry", "lesson_date"), name="lesson_plan_occurrence_uq"),
        ),
        migrations.AddIndex(
            model_name="lessonplan",
            index=models.Index(fields=["school", "lesson_date", "state"], name="lesson_plan_school_date_idx"),
        ),
        migrations.AddIndex(
            model_name="lessonplan",
            index=models.Index(fields=["author_membership", "state"], name="lesson_plan_author_state_idx"),
        ),
        migrations.AddConstraint(
            model_name="lessonplanreview",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="lesson_plan_review_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="lessonplanreview",
            constraint=models.UniqueConstraint(fields=("plan", "plan_version"), name="lesson_plan_review_version_uq"),
        ),
        migrations.AddConstraint(
            model_name="lessondeliveryrecord",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="lesson_delivery_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="lessondeliveryrecord",
            constraint=models.UniqueConstraint(fields=("timetable_entry", "lesson_date"), name="lesson_delivery_occurrence_uq"),
        ),
        migrations.AddIndex(
            model_name="lessondeliveryrecord",
            index=models.Index(fields=["school", "lesson_date", "state"], name="lesson_del_school_date_idx"),
        ),
        migrations.AddIndex(
            model_name="lessondeliveryrecord",
            index=models.Index(fields=["curriculum_topic", "state"], name="lesson_del_topic_state_idx"),
        ),
    ]

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("academics", "0003_elective_selection_history"),
        ("lesson_delivery", "0001_initial"),
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="WeeklyLearningUpdate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=160)),
                ("week_start", models.DateField()),
                ("week_end", models.DateField()),
                ("state", models.CharField(choices=[("draft", "Draft"), ("published", "Published")], default="draft", max_length=16)),
                ("next_focus", models.TextField(blank=True)),
                ("support_note", models.TextField(blank=True)),
                ("parent_note", models.TextField(blank=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("author_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="authored_weekly_learning_updates", to="schools.membership")),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="weekly_learning_updates", to="academics.classsubject")),
                ("last_edited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="edited_weekly_learning_updates", to="schools.membership")),
                ("published_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="published_weekly_learning_updates", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_learning_updates", to="schools.school")),
                ("term", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="weekly_learning_updates", to="academics.academicterm")),
            ],
            options={
                "ordering": ["-week_start", "class_subject__academic_class__level_order", "class_subject__subject__name"],
            },
        ),
        migrations.CreateModel(
            name="WeeklyLearningPublication",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("snapshot", models.JSONField(default=dict)),
                ("published_at", models.DateTimeField(auto_now_add=True)),
                ("published_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="weekly_learning_publications", to="schools.membership")),
                ("update", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="publications", to="weekly_learning.weeklylearningupdate")),
            ],
            options={"ordering": ["-published_at"]},
        ),
        migrations.AddConstraint(
            model_name="weeklylearningupdate",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="weekly_learning_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="weeklylearningupdate",
            constraint=models.UniqueConstraint(fields=("class_subject", "term", "week_start"), name="weekly_learning_subject_week_uq"),
        ),
        migrations.AddIndex(
            model_name="weeklylearningupdate",
            index=models.Index(fields=["school", "week_start", "state"], name="weekly_learning_school_week_idx"),
        ),
        migrations.AddIndex(
            model_name="weeklylearningupdate",
            index=models.Index(fields=["class_subject", "week_start"], name="weekly_learning_subject_week_idx"),
        ),
        migrations.AddConstraint(
            model_name="weeklylearningpublication",
            constraint=models.UniqueConstraint(fields=("update", "revision"), name="weekly_learning_publication_revision_uq"),
        ),
    ]

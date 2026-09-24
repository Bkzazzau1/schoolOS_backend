import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("academics", "0002_subjects_curriculum_teaching"),
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.CreateModel(
            name="TimetableEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=64)),
                ("day_of_week", models.PositiveSmallIntegerField(choices=[(1, "Monday"), (2, "Tuesday"), (3, "Wednesday"), (4, "Thursday"), (5, "Friday"), (6, "Saturday"), (7, "Sunday")])),
                ("period_number", models.PositiveSmallIntegerField()),
                ("starts_at", models.TimeField()),
                ("ends_at", models.TimeField()),
                ("room", models.CharField(blank=True, max_length=120)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("class_subject", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="timetable_entries", to="academics.classsubject")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="timetable_entries", to="schools.school")),
                ("term", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="timetable_entries", to="academics.academicterm")),
            ],
            options={"ordering": ["term__starts_on", "day_of_week", "starts_at", "period_number"]},
        ),
        migrations.CreateModel(
            name="TimetableOverride",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=64)),
                ("lesson_date", models.DateField()),
                ("room", models.CharField(blank=True, max_length=120)),
                ("note", models.TextField(blank=True)),
                ("is_cancelled", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="schools.membership")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="timetable_overrides", to="schools.school")),
                ("substitute_teacher_membership", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="timetable_substitutions", to="schools.membership")),
                ("timetable_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="overrides", to="timetable.timetableentry")),
            ],
            options={"ordering": ["lesson_date", "timetable_entry__starts_at"]},
        ),
        migrations.AddConstraint(
            model_name="timetableentry",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="tt_entry_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="timetableentry",
            constraint=models.UniqueConstraint(fields=("term", "class_subject", "day_of_week", "period_number"), name="tt_entry_subject_period_uq"),
        ),
        migrations.AddIndex(
            model_name="timetableentry",
            index=models.Index(fields=["term", "day_of_week", "starts_at"], name="tt_term_day_time_idx"),
        ),
        migrations.AddIndex(
            model_name="timetableentry",
            index=models.Index(fields=["class_subject", "is_active"], name="tt_class_subject_idx"),
        ),
        migrations.AddConstraint(
            model_name="timetableoverride",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="tt_override_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="timetableoverride",
            constraint=models.UniqueConstraint(fields=("timetable_entry", "lesson_date"), name="tt_override_entry_date_uq"),
        ),
        migrations.AddIndex(
            model_name="timetableoverride",
            index=models.Index(fields=["lesson_date", "substitute_teacher_membership"], name="tt_override_date_teacher_idx"),
        ),
    ]

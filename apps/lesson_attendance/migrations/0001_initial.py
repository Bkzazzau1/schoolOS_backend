import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("academics", "0003_elective_selection_history"),
        ("students", "0003_student_guardian_account_links"),
        ("timetable", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LessonAttendanceRegister",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("external_id", models.CharField(max_length=128)),
                ("lesson_date", models.DateField()),
                ("state", models.CharField(choices=[("draft", "Draft"), ("submitted", "Submitted")], default="draft", max_length=16)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("curriculum_topic", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="attendance_registers", to="academics.curriculumtopic")),
                ("school", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lesson_attendance_registers", to="schools.school")),
                ("submitted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="submitted_lesson_attendance_registers", to="schools.membership")),
                ("teacher_membership", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_attendance_registers", to="schools.membership")),
                ("timetable_entry", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="attendance_registers", to="timetable.timetableentry")),
            ],
            options={
                "ordering": ["-lesson_date", "timetable_entry__starts_at", "external_id"],
            },
        ),
        migrations.CreateModel(
            name="LessonAttendanceEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("student_code", models.CharField(max_length=80)),
                ("student_name", models.CharField(max_length=320)),
                ("status", models.CharField(choices=[("unmarked", "Unmarked"), ("present", "Present"), ("absent", "Absent"), ("late", "Late"), ("excused", "Excused")], default="unmarked", max_length=16)),
                ("note", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("register", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="entries", to="lesson_attendance.lessonattendanceregister")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lesson_attendance_entries", to="students.student")),
            ],
            options={
                "ordering": ["student_name", "student_code"],
            },
        ),
        migrations.AddConstraint(
            model_name="lessonattendanceregister",
            constraint=models.UniqueConstraint(fields=("school", "external_id"), name="lesson_attendance_external_uq"),
        ),
        migrations.AddConstraint(
            model_name="lessonattendanceregister",
            constraint=models.UniqueConstraint(fields=("timetable_entry", "lesson_date"), name="lesson_attendance_occurrence_uq"),
        ),
        migrations.AddIndex(
            model_name="lessonattendanceregister",
            index=models.Index(fields=["school", "lesson_date", "state"], name="lesson_att_school_date_idx"),
        ),
        migrations.AddIndex(
            model_name="lessonattendanceregister",
            index=models.Index(fields=["teacher_membership", "lesson_date"], name="lesson_att_teacher_date_idx"),
        ),
        migrations.AddConstraint(
            model_name="lessonattendanceentry",
            constraint=models.UniqueConstraint(fields=("register", "student"), name="lesson_attendance_student_uq"),
        ),
        migrations.AddIndex(
            model_name="lessonattendanceentry",
            index=models.Index(fields=["student", "status"], name="lesson_att_student_status_idx"),
        ),
    ]

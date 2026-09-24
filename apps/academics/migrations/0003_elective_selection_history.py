from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("academics", "0002_subjects_curriculum_teaching"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="studentsubjectselection",
            name="student_elective_uq",
        ),
        migrations.AddField(
            model_name="studentsubjectselection",
            name="deselected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="studentsubjectselection",
            constraint=models.UniqueConstraint(
                fields=("enrollment_context", "class_subject"),
                condition=Q(deselected_at__isnull=True),
                name="student_elective_active_uq",
            ),
        ),
        migrations.AddIndex(
            model_name="studentsubjectselection",
            index=models.Index(
                fields=["enrollment_context", "class_subject", "deselected_at"],
                name="student_elective_hist_idx",
            ),
        ),
    ]

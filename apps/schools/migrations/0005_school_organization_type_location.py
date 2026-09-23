from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0001_initial"),
        ("schools", "0004_alter_membership_role_add_alumni"),
    ]

    operations = [
        migrations.AddField(
            model_name="school",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="schools",
                to="organizations.organization",
            ),
        ),
        migrations.AddField(
            model_name="school",
            name="school_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("nursery", "Nursery"),
                    ("primary", "Primary"),
                    ("secondary", "Secondary"),
                    ("nursery_primary", "Nursery + Primary"),
                    ("primary_secondary", "Primary + Secondary"),
                    ("nursery_primary_secondary", "Nursery + Primary + Secondary"),
                    ("college", "College"),
                    ("other", "Other"),
                ],
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="school",
            name="location",
            field=models.CharField(blank=True, default="", max_length=250),
        ),
        migrations.AddIndex(
            model_name="school",
            index=models.Index(
                fields=["organization", "is_active"],
                name="school_org_active_idx",
            ),
        ),
    ]

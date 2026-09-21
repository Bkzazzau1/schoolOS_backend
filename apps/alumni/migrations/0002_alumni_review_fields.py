from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("alumni", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="alumniprofile",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="alumniprofile",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="alumniprofile",
            name="verification_note",
            field=models.CharField(blank=True, max_length=500),
        ),
    ]

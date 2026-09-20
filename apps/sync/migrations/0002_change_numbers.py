import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("schools", "0002_school_official_email_school_official_sender_name"),
        ("sync", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SchoolSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last", models.PositiveBigIntegerField(default=0)),
                ("school", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="schools.school")),
            ],
        ),
        migrations.AddField(
            model_name="syncrecord",
            name="seq",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]

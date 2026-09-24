from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_login_identity_initial_password"),
        ("students", "0002_canonical_meter_authority"),
    ]

    operations = [
        migrations.AddField(
            model_name="student",
            name="account_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="student_profiles",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="guardianlink",
            name="account_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="guardian_links",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

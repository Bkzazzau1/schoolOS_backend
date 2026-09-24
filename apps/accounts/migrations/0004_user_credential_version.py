from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_login_identity_initial_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="credential_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]

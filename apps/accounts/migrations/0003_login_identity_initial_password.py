import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_user_email_verification"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="must_change_password",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="LoginIdentity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("student_admission", "Student admission ID"),
                            ("parent_phone", "Parent phone number"),
                        ],
                        max_length=32,
                    ),
                ),
                ("identifier", models.CharField(max_length=160)),
                ("normalized_identifier", models.CharField(max_length=160)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="login_identities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="loginidentity",
            constraint=models.UniqueConstraint(
                fields=("kind", "normalized_identifier"),
                name="unique_login_identity_by_kind",
            ),
        ),
        migrations.AddIndex(
            model_name="loginidentity",
            index=models.Index(
                fields=["user", "kind"],
                name="login_identity_user_kind_idx",
            ),
        ),
    ]

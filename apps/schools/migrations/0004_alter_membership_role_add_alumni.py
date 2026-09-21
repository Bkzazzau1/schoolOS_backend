from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schools", "0003_alter_membership_role"),
    ]

    operations = [
        migrations.AlterField(
            model_name="membership",
            name="role",
            field=models.CharField(
                choices=[
                    ("proprietor", "Proprietor"),
                    ("administrator", "Administrator"),
                    ("principal", "Principal"),
                    ("teacher", "Teacher"),
                    ("accountant", "Accountant"),
                    ("parent", "Parent"),
                    ("student", "Student"),
                    ("alumni", "Alumni"),
                    ("staff", "Staff"),
                    ("driver", "Driver"),
                ],
                max_length=20,
            ),
        ),
    ]

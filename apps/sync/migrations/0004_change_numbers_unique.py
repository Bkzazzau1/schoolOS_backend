from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("sync", "0003_number_existing_records")]

    operations = [
        migrations.AddConstraint(
            model_name="syncrecord",
            constraint=models.UniqueConstraint(fields=("school", "seq"), name="unique_sync_seq"),
        ),
    ]

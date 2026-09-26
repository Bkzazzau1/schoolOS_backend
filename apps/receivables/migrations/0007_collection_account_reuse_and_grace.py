"""What a family's collection account was promised (how long it is reused) and the wait after it is settled (the grace period)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("receivables", "0006_smart_collection_accounts")]

    operations = [
        migrations.AddField(model_name="familycollectionaccount", name="reuse_scope", field=models.CharField(blank=True, max_length=20)),
        migrations.AddField(model_name="familycollectionaccount", name="reuse_count", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="grace_until", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="after_grace", field=models.CharField(blank=True, max_length=8)),
    ]

"""Smart Money Collection offers Paystack and Monnify only, and the database now says so: the one-live-connection-per-provider rule covers
those two, and only a Paystack or Monnify connection (or the development sandbox) can be the school's active provider."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bankconnect', '0005_retire_remita_connections'),
        ('schools', '0005_school_organization_type_location'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='collectionproviderconnection',
            name='one_live_provider_connection_per_school',
        ),
        migrations.AddConstraint(
            model_name='collectionproviderconnection',
            constraint=models.UniqueConstraint(condition=models.Q(('provider__in', ('paystack', 'monnify')), models.Q(('status', 'revoked'), _negated=True)), fields=('school', 'provider'), name='one_live_provider_connection_per_school'),
        ),
        migrations.AddConstraint(
            model_name='collectionproviderconnection',
            constraint=models.CheckConstraint(condition=models.Q(('is_active_provider', False), ('provider__in', ('paystack', 'monnify', 'sandbox')), _connector='OR'), name='active_provider_is_a_collection_provider'),
        ),
    ]

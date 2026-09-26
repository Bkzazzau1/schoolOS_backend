"""Remita is not a Smart Money Collection provider: it is reserved for a separate Mandates / Direct Debit feature.

A Remita provider connection left by the earlier design is disconnected, and nothing is deleted: its sealed credential and callback address
are dropped, it stops being the school's active provider, and the school chooses Paystack or Monnify itself (no other provider is switched
on in its place). The rows stay, so payments recorded under them still read correctly. This runs on its own, before the migration that
adds the database rules, so those rules never meet a row that breaks them. On a school that never used Remita it does nothing.
"""

from django.db import migrations
from django.utils import timezone

REMITA = "remita"
WHY = "Remita is not a Smart Money Collection provider. It is reserved for Mandates / Direct Debit."


def retire_remita_connections(apps, schema_editor):
    Connection = apps.get_model("bankconnect", "CollectionProviderConnection")
    Audit = apps.get_model("bankconnect", "BankAuditEvent")
    now = timezone.now()
    for connection in Connection.objects.filter(provider=REMITA):
        was_active = connection.is_active_provider
        connection.sealed_credentials = b""
        connection.webhook_token_hash, connection.webhook_status = "", "not_configured"
        connection.token_expires_at = None
        connection.is_active_provider = False
        if connection.status != "revoked":
            connection.status, connection.disconnected_at, connection.last_error_code = "revoked", now, "provider_removed"
        connection.save()
        Audit.objects.create(school_id=connection.school_id, connection=connection, kind="provider_retired", detail={"provider": REMITA, "wasActive": was_active, "why": WHY})


class Migration(migrations.Migration):
    dependencies = [("bankconnect", "0004_collection_provider_connection")]

    operations = [migrations.RunPython(retire_remita_connections, migrations.RunPython.noop)]

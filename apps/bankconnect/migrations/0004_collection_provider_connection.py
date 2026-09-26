"""The school's own collection-provider connection (Paystack, Monnify, Remita), replacing the "connect a bank account" model.

The table is renamed, not rebuilt, so every payment, decision, allocation and audit row recorded against a connection keeps its
link. The bank-account columns stay (empty for a provider connection) so nothing older is lost.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("bankconnect", "0003_family_and_receivable_links"),
        ("receivables", "0003_credit_ledger_collection_accounts_statements"),
    ]

    operations = [
        migrations.RenameModel(old_name="BankConnection", new_name="CollectionProviderConnection"),
        migrations.RenameIndex(
            model_name="collectionproviderconnection", new_name="bankconnect_school__935715_idx", old_name="bankconnect_school__8276aa_idx",
        ),
        migrations.RemoveConstraint(model_name="collectionproviderconnection", name="unique_live_bank_account_per_school"),
        migrations.AlterField(
            model_name="collectionproviderconnection",
            name="connection_type",
            field=models.CharField(
                choices=[
                    ("direct_bank_api", "Direct bank API"), ("open_banking", "Open banking"),
                    ("collection_provider", "Collection provider"), ("sandbox", "Sandbox"),
                ],
                default="collection_provider", max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="collectionproviderconnection", name="environment",
            field=models.CharField(choices=[("live", "Live"), ("test", "Test")], default="live", max_length=8),
        ),
        migrations.AddField(model_name="collectionproviderconnection", name="merchant_name", field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name="collectionproviderconnection", name="merchant_reference", field=models.CharField(blank=True, max_length=60)),
        migrations.AddField(model_name="collectionproviderconnection", name="provider_settings", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="collectionproviderconnection", name="is_active_provider", field=models.BooleanField(default=False)),
        migrations.AddField(
            model_name="collectionproviderconnection", name="webhook_status",
            field=models.CharField(
                choices=[("not_configured", "Not set up"), ("awaiting_event", "Waiting for the first event"), ("active", "Active")],
                default="not_configured", max_length=16,
            ),
        ),
        migrations.AddField(model_name="collectionproviderconnection", name="webhook_confirmed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="collectionproviderconnection", name="last_verified_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddConstraint(
            model_name="collectionproviderconnection",
            constraint=models.UniqueConstraint(
                condition=models.Q(("provider__in", ("paystack", "monnify", "remita")), models.Q(("status", "revoked"), _negated=True)),
                fields=("school", "provider"), name="one_live_provider_connection_per_school",
            ),
        ),
        migrations.AddConstraint(
            model_name="collectionproviderconnection",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active_provider", True)), fields=("school",), name="one_active_collection_provider_per_school",
            ),
        ),
        migrations.CreateModel(
            name="SandboxProviderAccount",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("reference", models.CharField(max_length=80)),
                ("account_number", models.CharField(max_length=20)),
                ("status", models.CharField(default="active", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "connection",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE, related_name="sandbox_accounts", to="bankconnect.collectionproviderconnection"
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="sandboxprovideraccount",
            constraint=models.UniqueConstraint(fields=("connection", "reference"), name="unique_sandbox_account_reference"),
        ),
        migrations.AddConstraint(
            model_name="collectionproviderconnection",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_active_provider", False), ("status__in", ["connected", "needs_reauth", "error"]), _connector="OR"),
                name="active_provider_is_in_use",
            ),
        ),
    ]

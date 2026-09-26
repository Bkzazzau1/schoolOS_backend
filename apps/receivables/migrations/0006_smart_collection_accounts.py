"""A family has at most one live collection account per school, made by the school's active provider (Smart Money Collection).

Every account that exists before this is marked as recorded by hand (`legacy_manual`), so the rule that a provider-made account always
belongs to a provider connection does not touch it. Where a family held several live accounts (with different banks), the oldest stays
live and the others are CLOSED - never deleted - so the one-live-account rule can be enforced without losing any account's history.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone

LIVE = ("provisioning", "active", "settled", "grace", "dormant", "suspended", "closing")


def close_extra_live_accounts(apps, schema_editor):
    Account = apps.get_model("receivables", "FamilyCollectionAccount")
    seen = set()
    for account in Account.objects.filter(status__in=LIVE).order_by("school_id", "family_id", "created_at", "id"):
        key = (account.school_id, account.family_id)
        if key in seen:
            account.status = "closed"
            account.closed_at = timezone.now()
            account.close_reason = "Closed when a family was limited to one collection account per school."
            account.save(update_fields=["status", "closed_at", "close_reason"])
        else:
            seen.add(key)


class Migration(migrations.Migration):
    dependencies = [
        ("academics", "0003_elective_selection_history"),
        ("bankconnect", "0004_collection_provider_connection"),
        ("receivables", "0005_family_merge"),
        ("schools", "0005_school_organization_type_location"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="familycollectionaccount", name="one_live_account_per_family_and_provider"),
        migrations.AddField(
            model_name="familycollectionaccount", name="account_mode",
            field=models.CharField(choices=[("static", "Static"), ("dynamic", "Dynamic")], default="static", max_length=8),
        ),
        migrations.AddField(model_name="familycollectionaccount", name="close_reason", field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name="familycollectionaccount", name="closed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="collection_target_minor", field=models.BigIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="idempotency_key", field=models.CharField(blank=True, max_length=80)),
        # Every account that already exists was recorded by hand ...
        migrations.AddField(
            model_name="familycollectionaccount", name="origin",
            field=models.CharField(
                choices=[("provider", "Provider-generated"), ("legacy_manual", "Recorded by hand (legacy)")], default="legacy_manual", max_length=14
            ),
        ),
        # ... and from now on an account is provider-generated unless said otherwise.
        migrations.AlterField(
            model_name="familycollectionaccount", name="origin",
            field=models.CharField(
                choices=[("provider", "Provider-generated"), ("legacy_manual", "Recorded by hand (legacy)")], default="provider", max_length=14
            ),
        ),
        migrations.AddField(
            model_name="familycollectionaccount", name="scope_session",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="academics.academicsession"),
        ),
        migrations.AddField(
            model_name="familycollectionaccount", name="scope_term",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="academics.academicterm"),
        ),
        migrations.AddField(model_name="familycollectionaccount", name="settled_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="valid_from", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="familycollectionaccount", name="valid_until", field=models.DateField(blank=True, null=True)),
        migrations.AlterField(
            model_name="familycollectionaccount", name="status",
            field=models.CharField(
                choices=[
                    ("provisioning", "Being set up"), ("active", "Active"), ("settled", "Settled"), ("grace", "In grace period"),
                    ("dormant", "Dormant"), ("suspended", "Suspended"), ("closing", "Closing"), ("closed", "Closed"), ("failed", "Failed"),
                ],
                default="provisioning", max_length=14,
            ),
        ),
        migrations.RunPython(close_extra_live_accounts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="familycollectionaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["closed", "failed"]), _negated=True), fields=("school", "family"),
                name="one_live_collection_account_per_family_per_school",
            ),
        ),
        migrations.AddConstraint(
            model_name="familycollectionaccount",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key", ""), _negated=True), fields=("school", "idempotency_key"),
                name="one_collection_account_per_generation_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="familycollectionaccount",
            constraint=models.CheckConstraint(
                condition=models.Q(("origin", "legacy_manual"), ("connection__isnull", False), _connector="OR"), name="provider_account_has_a_connection"
            ),
        ),
    ]

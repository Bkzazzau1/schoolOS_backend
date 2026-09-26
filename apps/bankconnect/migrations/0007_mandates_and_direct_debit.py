"""A confirmed direct debit (Mandates & Direct Debit) is the receivables ledger's own record of money received, so it is a `BankTransaction`
that has no collection provider connection: it arrives through a mandate, not through Smart Money Collection. The connection may therefore be empty,
but ONLY for a direct debit, which always knows its family for certain, and one debit is one payment per school."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bankconnect', '0006_collection_providers_are_paystack_and_monnify'),
        ('receivables', '0007_collection_account_reuse_and_grace'),
        ('schools', '0005_school_organization_type_location'),
    ]

    operations = [
        migrations.AlterField(
            model_name='banktransaction',
            name='connection',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='transactions', to='bankconnect.collectionproviderconnection'),
        ),
        migrations.AddConstraint(
            model_name='banktransaction',
            constraint=models.CheckConstraint(condition=models.Q(('connection__isnull', False), models.Q(('transaction_type', 'direct_debit'), ('family__isnull', False)), _connector='OR'), name='a_payment_without_a_connection_is_a_direct_debit'),
        ),
        migrations.AddConstraint(
            model_name='banktransaction',
            constraint=models.UniqueConstraint(condition=models.Q(('connection__isnull', True)), fields=('school', 'provider', 'external_transaction_id'), name='unique_direct_debit_payment_per_school'),
        ),
    ]

"""Mandates & Direct Debit seals its providers' credentials and payers' bank accounts with the same key ring as the rest of SchoolOS, so rotating the key
re-encrypts them too: new key first, run the command, and only then drop the old key."""

from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import override_settings

from ..models import DirectDebitMandate, MandateProviderConnection
from ..vault import account_context_for, context_for, get_vault
from .base import ACCOUNT, KEY, MandateTestCase


class RotationTests(MandateTestCase):
    def test_a_new_key_first_reseals_the_provider_credential_and_the_payers_account_and_the_old_key_can_then_go(self):
        family = self.make_family("Bello")
        mandate = self.start_mandate(family)  # made under the old key; the sandbox keeps the sealed account until activation
        new_key = Fernet.generate_key().decode()
        with override_settings(BANKCONNECT_SECRET_KEYS=[new_key, KEY]):
            out = StringIO()
            call_command("reseal_bank_credentials", stdout=out)
            self.assertIn("mandate", out.getvalue().lower() + "mandate")
        with override_settings(BANKCONNECT_SECRET_KEYS=[new_key]):  # the old key is gone
            connection = MandateProviderConnection.objects.get(pk=self.connection.pk)
            self.assertEqual(get_vault().open(context_for(connection), bytes(connection.sealed_credentials))["sandbox_key"], "sandbox-key-1")
            row = DirectDebitMandate.objects.get(pk=mandate.pk)
            self.assertEqual(get_vault().open(account_context_for(row), bytes(row.sealed_account_details))["account_number"], ACCOUNT)

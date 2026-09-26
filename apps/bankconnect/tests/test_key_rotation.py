from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.test import override_settings

from .. import vault
from ..models import BankConnection
from .base import BankTestCase


class KeyRotationTests(BankTestCase):
    def setUp(self):
        super().setUp()
        self.connection = self.row(self.connected()[0])
        self.old_key = Fernet.generate_key().decode()
        self.new_key = Fernet.generate_key().decode()

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        try:
            call_command("reseal_bank_credentials", *args, stdout=out, stderr=err)
        finally:
            self.output, self.errors = out.getvalue(), err.getvalue()

    def seal_under(self, key):
        secret = self.secret_of(self.connection)
        self.connection.sealed_credentials = vault.FernetVault([key]).seal(vault.context_for(self.connection), secret)
        self.connection.save()

    def test_a_credential_moves_to_the_newest_key_and_still_works(self):
        self.seal_under(self.old_key)
        with override_settings(BANKCONNECT_SECRET_KEYS=[self.new_key, self.old_key]):
            self.run_command()
        self.assertIn("1 re-encrypted, 0 unreadable", self.output)
        self.connection.refresh_from_db()
        # Now the old key can be dropped: the new one alone opens it.
        opened = vault.FernetVault([self.new_key]).open(vault.context_for(self.connection), self.connection.sealed_credentials)
        self.assertEqual(opened["sandbox_key"], "sandbox-UNIQUE-KEY-777")
        with self.assertRaises(vault.VaultError):
            vault.FernetVault([self.old_key]).open(vault.context_for(self.connection), self.connection.sealed_credentials)

    def test_a_dry_run_changes_nothing(self):
        self.seal_under(self.old_key)
        before = bytes(BankConnection.objects.get().sealed_credentials)
        with override_settings(BANKCONNECT_SECRET_KEYS=[self.new_key, self.old_key]):
            self.run_command("--dry-run")
        self.assertIn("1 would be re-encrypted", self.output)
        self.assertEqual(bytes(BankConnection.objects.get().sealed_credentials), before)

    def test_running_it_again_is_harmless(self):
        self.seal_under(self.old_key)
        with override_settings(BANKCONNECT_SECRET_KEYS=[self.new_key, self.old_key]):
            self.run_command()
            self.run_command()
        self.connection.refresh_from_db()
        self.assertEqual(self.secret_of_with([self.new_key])["sandbox_key"], "sandbox-UNIQUE-KEY-777")

    def secret_of_with(self, keys):
        return vault.FernetVault(keys).open(vault.context_for(self.connection), self.connection.sealed_credentials)

    def test_a_credential_no_key_can_open_is_reported_and_the_run_fails_so_the_old_key_is_kept(self):
        self.seal_under(self.old_key)
        stranger = Fernet.generate_key().decode()
        with override_settings(BANKCONNECT_SECRET_KEYS=[self.new_key, stranger]):
            with self.assertRaises(CommandError) as raised:
                self.run_command()
        self.assertIn("Do NOT remove the old key", str(raised.exception))
        self.assertIn(f"{self.connection.id}: could not be opened", self.errors)
        self.assertNotIn("sandbox-UNIQUE-KEY-777", self.errors + self.output + str(raised.exception))

    def test_disconnected_accounts_hold_no_credential_and_are_skipped(self):
        self.api_post(f"connections/{self.connection.id}/disconnect/")
        self.run_command()
        self.assertIn("0 re-encrypted, 0 unreadable", self.output)

    @override_settings(BANKCONNECT_SECRET_KEYS=[])
    def test_with_no_keys_configured_it_refuses(self):
        with self.assertRaises(CommandError):
            self.run_command()

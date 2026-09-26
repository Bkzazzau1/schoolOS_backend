from django.core.management.base import BaseCommand, CommandError

from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.vault import VaultError, get_vault


class Command(BaseCommand):
    help = (
        "Re-encrypt every stored bank credential under the newest key in BANKCONNECT_SECRET_KEYS. "
        "To rotate the key: put the new key FIRST in the list (keep the old one after it), deploy, run this, "
        "and only then remove the old key. Safe to run again; a credential already on the newest key is unchanged."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Say what would change without changing anything.")

    def handle(self, *args, **options):
        try:
            vault = get_vault()
        except VaultError as error:
            raise CommandError(str(error))

        resealed = unreadable = 0
        for connection in CollectionProviderConnection.objects.exclude(sealed_credentials=b"").iterator():
            try:
                blob = vault.reseal(bytes(connection.sealed_credentials))
            except VaultError:
                # Never say anything about its contents: only which connection could not be opened.
                unreadable += 1
                self.stderr.write(f"{connection.id}: could not be opened with any configured key")
                continue
            resealed += 1
            if not options["dry_run"]:
                connection.sealed_credentials = blob
                connection.save(update_fields=["sealed_credentials", "updated_at"])

        verb = "would be re-encrypted" if options["dry_run"] else "re-encrypted"
        self.stdout.write(self.style.SUCCESS(f"Bank credentials: {resealed} {verb}, {unreadable} unreadable."))
        if unreadable:
            raise CommandError(
                f"{unreadable} credential(s) could not be opened. Do NOT remove the old key until they are dealt with."
            )

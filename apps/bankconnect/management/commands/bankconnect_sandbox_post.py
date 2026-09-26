from django.core.management.base import BaseCommand, CommandError

from apps.bankconnect import sandbox_tools, sync
from apps.bankconnect.models import BankConnection
from apps.bankconnect.providers import registry


class Command(BaseCommand):
    help = "Put a synthetic credit on a SANDBOX bank connection and sync it, to try the feature without a bank."

    def add_arguments(self, parser):
        parser.add_argument("connection", help="The sandbox connection's id.")
        parser.add_argument("--naira", type=int, required=True, help="Amount in whole naira.")
        parser.add_argument("--sender", default="", help="Who the money says it is from.")
        parser.add_argument("--narration", default="", help="The transfer narration, e.g. a student code.")
        parser.add_argument("--no-sync", action="store_true", help="Queue it without syncing.")

    def handle(self, *args, **options):
        if not registry.sandbox_enabled():
            raise CommandError("The sandbox is switched off (BANKCONNECT_ENABLE_SANDBOX).")
        try:
            connection = BankConnection.objects.get(id=options["connection"])
        except (BankConnection.DoesNotExist, ValueError):
            raise CommandError("No such connection.")
        if not connection.is_sandbox:
            raise CommandError("That is not a sandbox connection.")
        sandbox_tools.add_feed_item(
            connection, amount_minor=options["naira"] * 100, sender_name=options["sender"], narration=options["narration"]
        )
        self.stdout.write("Queued one synthetic credit.")
        if not options["no_sync"]:
            outcome = sync.sync_connection(connection)
            self.stdout.write(f"Synced: new={outcome.created} repeats={outcome.duplicates} ok={outcome.ok}")

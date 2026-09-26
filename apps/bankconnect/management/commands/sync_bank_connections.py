from django.core.management.base import BaseCommand, CommandError

from apps.bankconnect import sync
from apps.bankconnect.constants import ConnectionStatus
from apps.bankconnect.models import BankConnection


class Command(BaseCommand):
    help = (
        "Pull new transactions for every connected bank account that reports them. Safe to run as "
        "often as you like (cron): a transaction already held is never stored twice."
    )

    def add_arguments(self, parser):
        parser.add_argument("--school", help="Only this school (its slug).")
        parser.add_argument("--connection", help="Only this connection (its id).")
        parser.add_argument("--max-pages", type=int, default=sync.MAX_PAGES)

    def handle(self, *args, **options):
        connections = BankConnection.objects.filter(status=ConnectionStatus.CONNECTED).select_related("school")
        if options["school"]:
            connections = connections.filter(school__slug=options["school"])
        if options["connection"]:
            connections = connections.filter(id=options["connection"])

        ran = created = failed = 0
        for connection in connections:
            if not sync.can_sync(connection):
                continue
            ran += 1
            try:
                outcome = sync.sync_connection(connection, max_pages=options["max_pages"])
            except Exception as error:  # one account must never stop the others
                failed += 1
                self.stderr.write(f"{connection.id}: {type(error).__name__}")
                continue
            created += outcome.created
            if not outcome.ok:
                failed += 1
                self.stderr.write(f"{connection.id}: {outcome.error_code}")
        self.stdout.write(self.style.SUCCESS(f"Bank sync: connections={ran} new_transactions={created} failed={failed}"))
        if failed:
            raise CommandError(f"{failed} connection(s) could not be synced.")

from django.core.management.base import BaseCommand, CommandError

from apps.bankconnect import sandbox_tools
from apps.bankconnect.models import CollectionProviderConnection
from apps.bankconnect.providers import registry


class Command(BaseCommand):
    help = "Deliver a synthetic payment into a family collection account on a SANDBOX provider connection, through the real webhook path."

    def add_arguments(self, parser):
        parser.add_argument("connection", help="The sandbox connection's id.")
        parser.add_argument("--account", required=True, help="The family collection account number to pay into.")
        parser.add_argument("--naira", type=int, required=True, help="Amount in whole naira.")
        parser.add_argument("--sender", default="", help="Who the money says it is from.")

    def handle(self, *args, **options):
        if not registry.sandbox_enabled():
            raise CommandError("The sandbox is switched off (BANKCONNECT_ENABLE_SANDBOX).")
        try:
            connection = CollectionProviderConnection.objects.get(id=options["connection"])
        except (CollectionProviderConnection.DoesNotExist, ValueError):
            raise CommandError("No such connection.")
        if not connection.is_sandbox:
            raise CommandError("That is not a sandbox connection.")
        result = sandbox_tools.deliver(
            connection, amount_minor=options["naira"] * 100, sender_name=options["sender"], receiving_account_reference=options["account"]
        )
        self.stdout.write(f"Delivered: {result.outcome} (new payments: {result.created})")

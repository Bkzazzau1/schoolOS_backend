from django.core.management.base import BaseCommand, CommandError

from apps.billing.cycle import run_billing_cycles


class Command(BaseCommand):
    help = "Run idempotent SchoolOS SaaS billing cycles for every active organization."

    def handle(self, *args, **options):
        summary = run_billing_cycles()
        self.stdout.write(
            self.style.SUCCESS(
                "Billing cycle run: "
                f"processed={summary['processed']} "
                f"invoices={summary['invoicesIssued']} "
                f"scheduled={summary['scheduled']} "
                f"outstanding={summary['outstanding']} "
                f"skipped={summary['skipped']} "
                f"errors={len(summary['errors'])}"
            )
        )
        for problem in summary["errors"]:
            self.stderr.write(
                f"{problem['subscriptionId']}: {problem['error']}"
            )
        if summary["errors"]:
            raise CommandError(
                f"Billing cycle completed with {len(summary['errors'])} account error(s)."
            )

"""Carry out queued direct-debit jobs (sending approved debits, and asking providers what happened to the ones whose outcome is not known).

    python manage.py run_mandate_jobs            # everything that is due now, then stop
    python manage.py run_mandate_jobs --loop     # a worker: keep going, sleeping when nothing is due

It also asks each provider about every mandate it still has something to say about, so activations and cancellations are noticed even where a provider
sends no callback.
"""

import time

from django.core.management.base import BaseCommand

from apps.mandates import jobs, mandate_provider


class Command(BaseCommand):
    help = "Carry out queued direct-debit jobs and refresh watched mandates."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running as a worker.")
        parser.add_argument("--sleep", type=float, default=5.0, help="Seconds to wait when nothing is due (with --loop).")
        parser.add_argument("--no-mandates", action="store_true", help="Do not ask providers about watched mandates.")

    def handle(self, *args, **options):
        while True:
            ran = jobs.drain()
            changed = 0 if options["no_mandates"] else mandate_provider.sync_watched()
            if ran or changed:
                self.stdout.write(f"jobs run: {ran}, mandates changed: {changed}")
            if not options["loop"]:
                return
            if not ran:
                time.sleep(options["sleep"])

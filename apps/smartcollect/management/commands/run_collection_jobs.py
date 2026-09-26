import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.smartcollect import jobs, lifecycle, switching


class Command(BaseCommand):
    help = (
        "Carry out the provider calls Smart Money Collection has queued (making and retiring families' collection accounts), end grace "
        "periods that are over, and look again at any planned provider switch. Run once from cron, or as a worker with --loop."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, waiting between rounds (a worker).")
        parser.add_argument("--interval", type=int, default=15, help="Seconds to wait between rounds with --loop (default 15).")
        parser.add_argument("--limit", type=int, default=None, help="Run at most this many jobs per round.")

    def round(self, limit):
        ran = jobs.drain(limit=limit)
        moved = lifecycle.process_grace()
        from apps.schools.models import School

        for school in School.objects.filter(pk__in=switching.ProviderSwitch.objects.filter(status__in=("scheduled", "ready_to_switch")).values("school_id")):
            switching.refresh(school)
        return ran, moved

    def handle(self, *args, **options):
        while True:
            ran, moved = self.round(options["limit"])
            self.stdout.write(f"{timezone.now():%H:%M:%S} jobs run: {ran}, accounts past their grace period: {moved}")
            if not options["loop"]:
                return
            time.sleep(options["interval"])

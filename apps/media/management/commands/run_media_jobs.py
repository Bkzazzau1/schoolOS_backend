import time

from django.core.management.base import BaseCommand

from apps.media import jobs


class Command(BaseCommand):
    help = "Verify uploads, build thumbnails, and purge retired files' bytes. Run once, or --loop as a worker."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, sleeping between empty passes.")
        parser.add_argument("--sleep", type=float, default=5.0, help="Seconds to sleep when nothing was due (with --loop).")

    def handle(self, *args, **options):
        if not options["loop"]:
            done = jobs.drain()
            self.stdout.write(self.style.SUCCESS(f"Ran {done} job(s)."))
            return
        self.stdout.write("Watching for media jobs. Ctrl+C to stop.")
        while True:
            done = jobs.drain()
            if not done:
                time.sleep(options["sleep"])

from django.core.management.base import BaseCommand

from apps.students.services import refresh_all_roster_meters


class Command(BaseCommand):
    help = "Publish fresh authoritative student-roster billing meters for active schools."

    def handle(self, *args, **options):
        total = refresh_all_roster_meters()
        self.stdout.write(
            self.style.SUCCESS(
                f"Published canonical student-roster meters for {total} active schools."
            )
        )

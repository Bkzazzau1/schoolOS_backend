from django.core.management.base import BaseCommand

from apps.transferverify.network import ALERT_EXPIRY_DAYS, expire_stale_alerts


class Command(BaseCommand):
    help = f"Mark TransferVerify alerts EXPIRED once they have sat untouched for {ALERT_EXPIRY_DAYS} days."

    def handle(self, *args, **options):
        summary = expire_stale_alerts()
        self.stdout.write(self.style.SUCCESS(f"TransferVerify expiry sweep: expired={summary['expired']}"))

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.billing.cycle import publish_school_billing_meter
from apps.schools.models import School


class Command(BaseCommand):
    help = (
        "Publish one authoritative school billable-student count. "
        "This is an integration/bootstrap hook until the canonical Student module publishes automatically."
    )

    def add_arguments(self, parser):
        parser.add_argument("--school", required=True, help="School UUID")
        parser.add_argument("--count", required=True, type=int)
        parser.add_argument("--source", required=True)
        parser.add_argument("--version", required=True)
        parser.add_argument(
            "--measured-at",
            help="Optional ISO-8601 measurement time; defaults to now.",
        )

    def handle(self, *args, **options):
        try:
            school = School.objects.get(pk=options["school"])
        except (School.DoesNotExist, ValueError) as exc:
            raise CommandError("School not found.") from exc

        measured_at = timezone.now()
        raw_measured_at = options.get("measured_at")
        if raw_measured_at:
            measured_at = parse_datetime(raw_measured_at)
            if measured_at is None:
                raise CommandError("--measured-at must be a valid ISO-8601 timestamp.")
            if timezone.is_naive(measured_at):
                measured_at = timezone.make_aware(measured_at)

        try:
            meter = publish_school_billing_meter(
                school=school,
                billable_student_count=options["count"],
                source=options["source"],
                source_version=options["version"],
                measured_at=measured_at,
                authoritative=True,
                metadata={"publishedBy": "management_command"},
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Meter published: school={school.id} count={meter.billable_student_count} "
                f"source={meter.source} version={meter.source_version}"
            )
        )

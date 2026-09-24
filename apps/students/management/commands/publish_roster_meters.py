from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.schools.models import School
from apps.students.models import StudentRegistration
from apps.students.services import publish_school_roster_meter
from apps.sync.models import SyncRecord


class Command(BaseCommand):
    help = "Publish fresh authoritative student-roster billing meters for active schools."

    def handle(self, *args, **options):
        materialized = StudentRegistration.objects.filter(
            school_id=OuterRef("school_id"),
            registration_id=OuterRef("entity_id"),
        )
        missing_school_ids = set(
            SyncRecord.objects.filter(
                entity_type="student_registration",
                deleted=False,
            )
            .annotate(materialized=Exists(materialized))
            .filter(materialized=False)
            .values_list("school_id", flat=True)
        )

        published = 0
        skipped = []
        observation_key = f"daily:{timezone.localdate().isoformat()}"
        for school in School.objects.filter(is_active=True).iterator():
            if school.id in missing_school_ids:
                skipped.append(str(school.id))
                continue
            publish_school_roster_meter(
                school,
                observation_key=observation_key,
                reason="daily_roster_refresh",
            )
            published += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Published canonical student-roster meters for {published} active schools."
            )
        )
        if skipped:
            raise CommandError(
                "Roster meter withheld for schools with accepted legacy student-registration "
                "sync records that have not been materialized into the canonical roster: "
                + ", ".join(skipped[:20])
            )

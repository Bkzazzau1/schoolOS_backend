from django.core.management.base import BaseCommand

from apps.academics.curriculum_services import publish_teacher_assignment_link
from apps.schools.models import Membership, Role


class Command(BaseCommand):
    help = (
        "Rebuild private Teacher My Classes sync links from canonical active "
        "teaching assignments. Safe to run repeatedly."
    )

    def handle(self, *args, **options):
        refreshed = 0
        for membership in (
            Membership.objects.filter(role=Role.TEACHER, is_active=True)
            .select_related("school", "user")
            .iterator()
        ):
            publish_teacher_assignment_link(membership)
            refreshed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Teacher assignment links refreshed for {refreshed} active Teacher memberships."
            )
        )

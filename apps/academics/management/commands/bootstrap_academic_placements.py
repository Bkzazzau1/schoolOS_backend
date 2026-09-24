from django.core.management.base import BaseCommand

from apps.academics.services import attach_current_enrollment_context
from apps.students.models import EnrollmentStatus, StudentEnrollment
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link


class Command(BaseCommand):
    help = (
        "Attach existing active student enrollments to the configured active "
        "academic session/classes. Safe to run repeatedly."
    )

    def handle(self, *args, **options):
        attached = 0
        skipped = 0
        for enrollment in (
            StudentEnrollment.objects.filter(status=EnrollmentStatus.ACTIVE)
            .select_related("school", "student")
            .iterator()
        ):
            context = attach_current_enrollment_context(
                enrollment,
                source="bootstrap",
            )
            if context is None:
                skipped += 1
                continue
            attached += 1
            publish_student_class_link(enrollment.student)
            publish_parent_family_links_for_student(enrollment.student)

        self.stdout.write(
            self.style.SUCCESS(
                f"Academic placement bootstrap complete: {attached} attached/confirmed, {skipped} skipped."
            )
        )

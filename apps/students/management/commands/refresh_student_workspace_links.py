from django.core.management.base import BaseCommand

from apps.students.models import Student
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link


class Command(BaseCommand):
    help = (
        "Rebuild private Student profile/class links and Parent family links from "
        "the canonical roster. Safe to run repeatedly after deployment."
    )

    def handle(self, *args, **options):
        refreshed = 0
        for student in (
            Student.objects.select_related("school", "account_user")
            .prefetch_related("enrollments", "guardians", "lifecycle_events")
            .iterator()
        ):
            publish_student_class_link(student)
            publish_parent_family_links_for_student(student)
            refreshed += 1
        self.stdout.write(self.style.SUCCESS(f"Refreshed {refreshed} student workspace link(s)."))

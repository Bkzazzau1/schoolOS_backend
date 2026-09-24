from django.core.management.base import BaseCommand

from apps.schools.models import Membership, Role
from apps.students.models import EnrollmentStatus, Student
from apps.students.parent_sync import publish_parent_family_links_for_student
from apps.students.student_sync import publish_student_class_link

from apps.academics.curriculum_services import publish_teacher_assignment_link
from apps.academics.models import TeachingAssignment


class Command(BaseCommand):
    help = (
        "Republish canonical subject eligibility and teaching-assignment links "
        "for existing Student, Parent and Teacher workspaces. Safe to run repeatedly."
    )

    def handle(self, *args, **options):
        student_count = 0
        teacher_count = 0

        students = (
            Student.objects.filter(
                enrollments__status=EnrollmentStatus.ACTIVE,
                enrollments__academic_context__isnull=False,
            )
            .distinct()
            .iterator()
        )
        for student in students:
            publish_student_class_link(student)
            publish_parent_family_links_for_student(student)
            student_count += 1

        teacher_ids = (
            TeachingAssignment.objects.filter(ended_at__isnull=True)
            .values_list("teacher_membership_id", flat=True)
            .distinct()
        )
        teachers = Membership.objects.filter(
            id__in=teacher_ids,
            role=Role.TEACHER,
            is_active=True,
        ).select_related("school", "user")
        for teacher in teachers:
            publish_teacher_assignment_link(teacher)
            teacher_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Curriculum workspace refresh complete: "
                f"{student_count} students/families, {teacher_count} teachers."
            )
        )

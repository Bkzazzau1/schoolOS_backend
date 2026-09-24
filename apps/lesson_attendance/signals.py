from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.academics.models import EnrollmentAcademicContext, StudentSubjectSelection
from apps.students.models import Student, StudentEnrollment
from apps.timetable.teacher_sync import publish_school_teacher_timetable_links


def _publish_school(school):
    if school is not None:
        publish_school_teacher_timetable_links(school)


@receiver(post_save, sender=StudentEnrollment)
def refresh_teacher_lesson_rosters_for_enrollment(sender, instance, **kwargs):
    _publish_school(instance.school)


@receiver(post_save, sender=EnrollmentAcademicContext)
def refresh_teacher_lesson_rosters_for_context(sender, instance, **kwargs):
    _publish_school(instance.enrollment.school)


@receiver(post_save, sender=StudentSubjectSelection)
@receiver(post_delete, sender=StudentSubjectSelection)
def refresh_teacher_lesson_rosters_for_elective(sender, instance, **kwargs):
    _publish_school(instance.class_subject.session.school)


@receiver(post_save, sender=Student)
def refresh_teacher_lesson_rosters_for_student_identity(sender, instance, **kwargs):
    # Name/admission-number changes should update the private Teacher roster too.
    _publish_school(instance.school)

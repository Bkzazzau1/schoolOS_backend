from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.alumni.models import AlumniVerificationEvent

from .alumni_bridge import graduate_canonical_student_from_alumni_transition


@receiver(post_save, sender=AlumniVerificationEvent)
def mirror_alumni_transition_to_student_roster(sender, instance, created, **kwargs):
    if not created or instance.event != AlumniVerificationEvent.Event.TRANSITIONED:
        return
    graduate_canonical_student_from_alumni_transition(
        instance.profile,
        actor=instance.actor,
    )

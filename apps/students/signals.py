from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.alumni.models import AlumniVerificationEvent
from apps.billing.models import SchoolBillingMeterSnapshot

from .alumni_bridge import graduate_canonical_student_from_alumni_transition


@receiver(post_save, sender=AlumniVerificationEvent)
def mirror_alumni_transition_to_student_roster(sender, instance, created, **kwargs):
    if not created or instance.event != AlumniVerificationEvent.Event.TRANSITIONED:
        return
    graduate_canonical_student_from_alumni_transition(
        instance.profile,
        actor=instance.actor,
    )


@receiver(pre_save, sender=SchoolBillingMeterSnapshot)
def canonical_roster_owns_automatic_student_meter(sender, instance, **kwargs):
    """Only canonical roster observations may be authoritative after this app exists.

    The earlier billing slice deliberately supported a transitional manual meter.
    Keeping those observations readable is useful, but they must no longer drive
    automatic invoices once the canonical student domain is installed.
    """

    if instance.source != "canonical_student_roster":
        instance.authoritative = False

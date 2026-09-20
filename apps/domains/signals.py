import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.schools.models import School

from .hostnames import DomainError
from .services import ensure_platform_domain

logger = logging.getLogger(__name__)


@receiver(post_save, sender=School)
def give_new_school_its_domain(sender, instance, created, **kwargs):
    """Every new school gets its platform subdomain, however it was created."""
    if not created:
        return
    try:
        ensure_platform_domain(instance)
    except DomainError as error:
        # The school still exists; it just has no platform address until its short
        # name is fixed. An operator can see this in the admin.
        logger.warning("No platform domain for school %s: %s", instance.pk, error.message)

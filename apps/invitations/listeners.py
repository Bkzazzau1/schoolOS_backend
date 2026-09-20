"""Reacting to the staff feature: when someone is asked to register, send them their
link. The staff feature does not know this feature exists."""

import logging

from django.db import transaction
from django.dispatch import receiver

from apps.staff.signals import registration_requested

from .models import StaffLink
from .service import InvitationError, create_invitation

logger = logging.getLogger(__name__)


@receiver(registration_requested)
def send_link_when_registration_is_requested(sender, school, staff_id, email, system_role, name="", requested_by=None, **kwargs):
    if StaffLink.objects.filter(school=school, staff_id=staff_id).exists():
        return  # they already have a login: nothing to invite them to
    try:
        # A savepoint: if this fails, the change that asked for it is unaffected.
        with transaction.atomic():
            create_invitation(school, staff_id, email, system_role or "staff", created_by=requested_by)
    except InvitationError as error:
        logger.warning("No invitation for %s: %s", staff_id, error.message)
    except Exception:  # noqa: BLE001
        logger.exception("Could not create an invitation for %s", staff_id)

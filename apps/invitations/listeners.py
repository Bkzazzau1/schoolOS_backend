"""Reacting to the staff feature: when someone is asked to register, give them a
working login - immediately, with their own phone number as a temporary password
they must change on first sign-in, the same as a student/parent account. An emailed
registration link is only the fallback, for the one case that needs a person present
to prove they own the address (their email already belongs to an existing account).
The staff feature does not know this feature exists."""

import logging

from django.db import transaction
from django.dispatch import receiver

from apps.staff.signals import registration_requested

from .accept import provision_staff_login
from .models import StaffLink
from .service import InvitationError, create_invitation

logger = logging.getLogger(__name__)


@receiver(registration_requested)
def give_login_when_registration_is_requested(
    sender, school, staff_id, email, system_role, phone="", name="", requested_by=None, **kwargs
):
    if StaffLink.objects.filter(school=school, staff_id=staff_id).exists():
        return  # they already have a login: nothing to do

    first_name, _, last_name = name.strip().partition(" ")
    try:
        with transaction.atomic():
            provisioned = provision_staff_login(
                school, staff_id, email=email, phone=phone, role=system_role or "staff",
                first_name=first_name, last_name=last_name,
            )
    except InvitationError as error:
        logger.warning("Could not provision a login directly for %s: %s", staff_id, error.message)
        provisioned = None
    except Exception:  # noqa: BLE001
        logger.exception("Could not provision a login directly for %s", staff_id)
        provisioned = None
    if provisioned is not None:
        return

    try:
        # A savepoint: if this fails, the change that asked for it is unaffected.
        with transaction.atomic():
            create_invitation(school, staff_id, email, system_role or "staff", created_by=requested_by)
    except InvitationError as error:
        logger.warning("No invitation for %s: %s", staff_id, error.message)
    except Exception:  # noqa: BLE001
        logger.exception("Could not create an invitation for %s", staff_id)

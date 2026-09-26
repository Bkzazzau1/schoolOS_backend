"""Who may do what in Mandates & Direct Debit.

Four separate responsibilities, each the owner's by default and each delegable to a trusted person through a duty (a job assignment):

* **provider manage** (`finance.mandate_provider_manage`): the school's own Remita / Lendsqr credentials and callbacks. The only authority
  that can put a provider secret into SchoolOS. It is NOT implied by the Finance Office, an Accountant title, billing authority, or any
  Smart Money Collection duty;
* **manage** (`finance.mandate_manage`): start a mandate for a payer, watch it, resend activation instructions, suspend or cancel it and
  choose a family's primary mandate. It is never the payer's consent: nobody at the school can give that for them;
* **prepare** (`finance.mandate_prepare`): the MAKER of a direct-debit batch;
* **approve** (`finance.mandate_approve`): the CHECKER of a direct-debit batch.

Looking at mandates is for the owner, the Finance Office and anyone holding one of these duties. Nobody else at the school, and never across
schools. A payer sees only their own mandates, through the parent routes (see `payer`).
"""

from uuid import UUID

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.owner.jobs.access import JOB, has_duty
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .constants import DUTY_APPROVE, DUTY_MANAGE, DUTY_PREPARE, DUTY_PROVIDER_MANAGE, MANDATE_DUTIES

NEED_VIEW, NEED_PROVIDER, NEED_MANAGE, NEED_PREPARE, NEED_APPROVE = "view", "provider", "manage", "prepare", "approve"
#: Non-staff roles can never hold any authority here, whatever a stray record says.
_OUTSIDERS = (Role.PARENT, Role.STUDENT, Role.ALUMNI)


def _holds(membership, *duties) -> bool:
    return bool(membership.is_active) and membership.role not in _OUTSIDERS and any(has_duty(membership, d) for d in duties)


def _owner(membership) -> bool:
    return bool(membership.is_active) and membership.role == Role.PROPRIETOR


def can_manage_providers(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_PROVIDER_MANAGE)


def can_manage(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_MANAGE)


def can_prepare(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_PREPARE)


def can_approve(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_APPROVE)


def can_view(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role in (Role.PROPRIETOR, Role.ACCOUNTANT) or _holds(membership, *MANDATE_DUTIES)
    )


_CHECKS = {
    NEED_VIEW: (can_view, "Mandates are for the owner and the Finance Office."),
    NEED_PROVIDER: (can_manage_providers, "Only the owner, or someone the owner has authorised, can manage the school's mandate providers."),
    NEED_MANAGE: (can_manage, "Only the owner, or someone the owner has authorised, can manage mandates."),
    NEED_PREPARE: (can_prepare, "Only the owner, or someone the owner has authorised, can prepare a direct-debit batch."),
    NEED_APPROVE: (can_approve, "Only the owner, or someone the owner has authorised, can approve a direct-debit batch."),
}


def permissions_of(membership) -> dict:
    """What this person may do, for a screen to offer only what the server will accept."""
    return {
        "canView": can_view(membership), "canManageProviders": can_manage_providers(membership), "canManage": can_manage(membership),
        "canPrepare": can_prepare(membership), "canApprove": can_approve(membership),
    }


def recipients(school) -> list[Membership]:
    """Everyone at the school who may look at mandates: the owner, the finance office, and anyone the owner gave a mandate duty."""
    holders = set()
    for row in SyncRecord.objects.filter(school=school, entity_type=JOB, deleted=False):
        if row.payload.get("status") == "active" and set(MANDATE_DUTIES) & set(row.payload.get("duties") or []):
            try:
                holders.add(UUID(str(row.payload.get("membershipId"))))
            except ValueError:
                continue
    people = Membership.objects.select_related("school").filter(school=school, is_active=True)
    return [m for m in people if m.role in (Role.PROPRIETOR, Role.ACCOUNTANT) or (m.id in holders and m.role not in _OUTSIDERS)]


def _named_membership(request):
    query = getattr(request, "query_params", {})
    value = query.get("membership")
    data = getattr(request, "data", None)
    if not value and hasattr(data, "get"):
        value = data.get("membership")
    return value or None


def acting_membership(request, school_id, *, need: str = NEED_VIEW):
    """The membership the person is acting as, checked for this school and this level of authority."""
    membership = require_membership(request.user, school_id, membership_id=_named_membership(request))
    check, message = _CHECKS[need]
    if not check(membership):
        raise PermissionDenied(message)
    return membership

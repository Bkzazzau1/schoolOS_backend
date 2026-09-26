"""Who may do what in Smart Money Collection.

Four separate responsibilities, each the owner's by default and each delegable to a trusted person through a
duty (a job assignment):

* **provider manage** (`finance.collection_provider_manage`): the school's own Paystack / Monnify / Remita
  credentials, the webhook, the active provider and provider switches. This is the only authority that can put a
  provider secret into SchoolOS. It is NOT implied by the Finance Office, by an Accountant title, or by billing
  authority (`finance.billing_authority`), which decides what families owe and has nothing to do with payment secrets;
* **policy manage** (`finance.collection_policy_manage`): the school's collection policy and its overrides;
* **prepare** (`finance.collection_prepare`): the MAKER of a collection batch;
* **approve** (`finance.collection_approve`): the CHECKER of a collection batch.

Looking at collections (what arrived, which accounts exist, the review queue) is for the owner, the Finance Office and
anyone holding one of these duties. Nobody else, whatever their role, and never across schools.
"""

from uuid import UUID

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.owner.jobs.access import JOB, has_duty
from apps.schools.models import Membership, Role
from apps.sync.models import SyncRecord

from .constants import (
    DUTY_APPROVE,
    DUTY_MANAGE_CONNECTIONS,
    DUTY_POLICY_MANAGE,
    DUTY_PREPARE,
    DUTY_PROVIDER_MANAGE,
    SMART_COLLECTION_DUTIES,
)

NEED_VIEW, NEED_PROVIDER, NEED_POLICY, NEED_PREPARE, NEED_APPROVE = "view", "provider", "policy", "prepare", "approve"
#: Non-staff roles can never hold any authority here, whatever a stray record says.
_OUTSIDERS = (Role.PARENT, Role.STUDENT, Role.ALUMNI)


def _holds(membership, *duties) -> bool:
    return bool(membership.is_active) and membership.role not in _OUTSIDERS and any(has_duty(membership, d) for d in duties)


def _owner(membership) -> bool:
    return bool(membership.is_active) and membership.role == Role.PROPRIETOR


def can_manage_providers(membership) -> bool:
    # The earlier bank-connections duty is honoured as provider-management authority, so an assignment already made keeps working.
    return _owner(membership) or _holds(membership, DUTY_PROVIDER_MANAGE, DUTY_MANAGE_CONNECTIONS)


def can_manage_policy(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_POLICY_MANAGE)


def can_prepare(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_PREPARE)


def can_approve(membership) -> bool:
    return _owner(membership) or _holds(membership, DUTY_APPROVE)


def can_view_collections(membership) -> bool:
    return bool(membership.is_active) and (
        membership.role in (Role.PROPRIETOR, Role.ACCOUNTANT)
        or _holds(membership, *SMART_COLLECTION_DUTIES, DUTY_MANAGE_CONNECTIONS)
    )


#: Kept for the code and tests written before the four duties existed.
can_manage_connections = can_manage_providers

_CHECKS = {
    NEED_VIEW: (can_view_collections, "Collections are for the owner and the Finance Office."),
    NEED_PROVIDER: (can_manage_providers, "Only the owner, or someone the owner has authorised, can manage the school's collection providers."),
    NEED_POLICY: (can_manage_policy, "Only the owner, or someone the owner has authorised, can change the school's collection policy."),
    NEED_PREPARE: (can_prepare, "Only the owner, or someone the owner has authorised, can prepare a collection batch."),
    NEED_APPROVE: (can_approve, "Only the owner, or someone the owner has authorised, can approve a collection batch."),
}


def permissions_of(membership) -> dict:
    """What this person may do, for a screen to offer only what the server will accept."""
    return {
        "canView": can_view_collections(membership), "canManageProviders": can_manage_providers(membership),
        "canManagePolicy": can_manage_policy(membership), "canPrepare": can_prepare(membership), "canApprove": can_approve(membership),
    }


def collections_recipients(school) -> list[Membership]:
    """Everyone at the school who may look at collections: the owner, the finance office, and anyone the owner gave a
    collection duty. These are the people told when money arrives."""
    wanted = set(SMART_COLLECTION_DUTIES) | {DUTY_MANAGE_CONNECTIONS}
    holders = set()
    for row in SyncRecord.objects.filter(school=school, entity_type=JOB, deleted=False):
        if row.payload.get("status") == "active" and wanted & set(row.payload.get("duties") or []):
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


def acting_membership(request, school_id, *, manage: bool = False, need: str | None = None):
    """The membership the person is acting as, checked for this school and this level of authority.
    `manage=True` is the older spelling of `need="provider"`."""
    need = need or (NEED_PROVIDER if manage else NEED_VIEW)
    membership = require_membership(request.user, school_id, membership_id=_named_membership(request))
    check, message = _CHECKS[need]
    if not check(membership):
        raise PermissionDenied(message)
    return membership

"""Who may do what with what families owe.

Two levels, deliberately different:

* **Billing authority** decides the obligations themselves: fee schedules, due dates, publishing
  charges, discounts, scholarships, waivers and other adjustments. The proprietor has it; so does an
  active holder of the `finance.billing_authority` duty, which the proprietor gives through the
  existing job-assignment system. A job title never carries it: an Accountant has no more authority
  here than any other member until the proprietor says so.
* **Operating** the ledger - looking at families and statements, working out where a payment went,
  correcting an allocation - is the daily work of the finance office. It changes nothing about what
  a family owes.

Parents, students and alumni are never billing authorities or operators, whatever a stray record
says: nobody decides their own family's fees.
"""

from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import require_membership
from apps.owner.jobs.access import JOB, has_duty
from apps.schools.models import Role

from .constants import BILLING_AUTHORITY_DUTY, OPERATE_DUTIES
from .errors import Refused

#: Roles that can never hold financial authority in the school.
NON_STAFF_ROLES = (Role.PARENT, Role.STUDENT, Role.ALUMNI)


def _staff_side(membership) -> bool:
    return bool(membership.is_active) and membership.role not in NON_STAFF_ROLES


def can_manage_billing(membership) -> bool:
    """Whether this person may determine what families owe."""
    return _staff_side(membership) and (
        membership.role == Role.PROPRIETOR or has_duty(membership, BILLING_AUTHORITY_DUTY)
    )


def can_operate_receivables(membership) -> bool:
    """Whether this person may look at families, statements and payments and correct allocations."""
    return _staff_side(membership) and (
        membership.role in (Role.PROPRIETOR, Role.ACCOUNTANT)
        or any(has_duty(membership, duty) for duty in OPERATE_DUTIES)
    )


def require_billing_authority(actor, school) -> None:
    """The actor must be a billing authority AT THIS SCHOOL: a membership from another school never counts."""
    if actor is None or actor.school_id != school.id or not can_manage_billing(actor):
        raise Refused(
            "Only the owner, or someone the owner has given billing authority, can decide what families owe.",
            "not_billing_authority",
        )


def require_operator(actor, school) -> None:
    """The actor must work the ledger (owner, Finance Office, or a duty holder) AT THIS SCHOOL."""
    if actor is None or actor.school_id != school.id or not can_operate_receivables(actor):
        raise Refused("Only the owner or the Finance Office can do this.", "not_authorised")


def billing_authorities(school) -> list:
    """Everyone at the school who may determine fees: the proprietor(s) and the duty holders. These
    are the people told when something needs a decision."""
    from apps.schools.models import Membership
    from apps.sync.models import SyncRecord

    # One query for everyone holding the duty, instead of one per membership. Same rule as `has_duty`: the
    # grant must be active and linked to that membership.
    holders = {
        str(row.payload.get("membershipId"))
        for row in SyncRecord.objects.filter(school=school, entity_type=JOB, deleted=False)
        if row.payload.get("status") == "active" and BILLING_AUTHORITY_DUTY in (row.payload.get("duties") or [])
    }
    return [
        m for m in Membership.objects.select_related("school").filter(school=school, is_active=True)
        if m.role not in NON_STAFF_ROLES and (m.role == Role.PROPRIETOR or str(m.id) in holders)
    ]


def acting_membership(request, school_id, *, manage: bool = False):
    """The membership a request acts as, checked for this school and this level of authority."""
    query = getattr(request, "query_params", {})
    data = getattr(request, "data", None)
    named = query.get("membership") or (data.get("membership") if hasattr(data, "get") else None)
    membership = require_membership(request.user, school_id, membership_id=named or None)
    if manage:
        if not can_manage_billing(membership):
            raise PermissionDenied("Only the owner, or someone the owner has given billing authority, can decide what families owe.")
    elif not can_operate_receivables(membership):
        raise PermissionDenied("Family accounts are for the owner and the Finance Office.")
    return membership

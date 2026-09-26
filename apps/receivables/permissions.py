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
from apps.owner.jobs.access import has_duty
from apps.schools.models import Role

from .constants import BILLING_AUTHORITY_DUTY, OPERATE_DUTIES

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


def billing_authorities(school) -> list:
    """Everyone at the school who may determine fees: the proprietor(s) and the duty holders. These
    are the people told when something needs a decision."""
    from apps.schools.models import Membership

    return [m for m in Membership.objects.select_related("school").filter(school=school, is_active=True) if can_manage_billing(m)]


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

"""What a person may do with payroll, worked out on the server.

Mirrors the app (`payrollAuthoritiesFor`): the owner may do everything; a finance
officer can always view and prepare, since that is their job; approving a batch and
releasing payment need an authority the owner assigned AND a login linked to that
assignment (the server sets `status: active` and `membershipId` only when the
person's account is linked, so an assignment nobody has claimed gives no power).
"""

from apps.schools.models import Membership
from apps.sync.models import SyncRecord

from .constants import AUTHORITIES

AUTHORIZER = "owner_payroll_authorizer"
PAYROLL_AUTHORITIES = AUTHORITIES - {"approveStaff"}  # approving staff is not a payroll power


def payroll_authorities(membership: Membership) -> set[str]:
    if membership.role == "proprietor":
        return set(PAYROLL_AUTHORITIES)
    granted: set[str] = {"view", "prepare"} if membership.role == "accountant" else set()
    for record in SyncRecord.objects.filter(school=membership.school, entity_type=AUTHORIZER, deleted=False):
        p = record.payload
        if p.get("status") == "active" and p.get("membershipId") == str(membership.id):
            granted |= set(p.get("authorities") or []) & PAYROLL_AUTHORITIES
    if granted & {"approve", "pay"}:
        granted.add("view")  # approving or releasing a payment means seeing what is paid
    return granted


def members_with(school, authority: str) -> list[Membership]:
    """Active members of the school who hold this payroll authority."""
    candidates = Membership.objects.filter(school=school, is_active=True).select_related("school", "user")
    return [m for m in candidates if authority in payroll_authorities(m)]

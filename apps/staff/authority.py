"""Who is allowed to decide on new staff.

The owner always can. Anyone else can only if the owner assigned them the
"approveStaff" authority AND their login has been linked to that assignment. The
link (`status: active` and `membershipId` on the authorizer record) is set by the
server when the person's account is linked to their staff record, never by the
app, so an assignment nobody has claimed gives no power.
"""

from apps.sync.models import SyncRecord

from .constants import AUTHORIZER

APPROVE_STAFF = "approveStaff"


def can_approve_staff(membership) -> bool:
    if membership.role == "proprietor":
        return True
    records = SyncRecord.objects.filter(school=membership.school, entity_type=AUTHORIZER, deleted=False)
    for record in records:
        p = record.payload
        if (
            p.get("status") == "active"
            and p.get("membershipId") == str(membership.id)
            and APPROVE_STAFF in (p.get("authorities") or [])
        ):
            return True
    return False

from django.db import transaction

from apps.staff.constants import AUTHORIZER, PROFILE
from apps.sync import records
from apps.sync.models import SyncRecord

from .accept import JOB
from .models import StaffLink
from .service import InvitationError, record, revoke


@transaction.atomic
def unlink(school, staff_id: str, *, by) -> None:
    """Break the tie between a staff record and its login (the person left, or was
    linked to the wrong account). Owner only; the caller has checked.

    The person's authority and jobs are revoked and their staff membership is
    switched off, so they can no longer act as staff. Their user account is not
    deleted, and anything else they are (a parent, for example) is untouched.
    """
    link = StaffLink.objects.select_related("membership").filter(school=school, staff_id=staff_id).first()
    if link is None:
        raise InvitationError("not_linked", "This staff member has no login to unlink.", 404)
    membership = link.membership
    link.delete()

    profile = records.read(school, PROFILE, staff_id)
    if profile is not None:
        records.write(school, PROFILE, staff_id, {**profile, "linkedMembershipId": ""}, by=by)
    for entity_type, field in ((AUTHORIZER, "staffId"), (JOB, "registeredStaffId")):
        for row in SyncRecord.objects.filter(school=school, entity_type=entity_type, deleted=False):
            p = row.payload
            if p.get(field) == staff_id and p.get("status") in ("active", "pendingActivation"):
                records.write(school, entity_type, row.entity_id,
                              {**p, "status": "revoked", "revokedByMembershipId": str(by.id)}, by=by)
    membership.is_active = False
    membership.save(update_fields=["is_active"])
    revoke(school, staff_id, by=by)

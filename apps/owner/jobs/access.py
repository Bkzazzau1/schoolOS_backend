"""Whether someone holds a duty the owner gave them through a job assignment.

An assignment gives power only while it is `active` and linked to this login. The
server sets both when the person's account is linked to their staff record, so an
assignment nobody has claimed gives none.
"""

from apps.sync.models import SyncRecord

JOB = "owner_job_assignment"


def has_duty(membership, duty: str) -> bool:
    rows = SyncRecord.objects.filter(school=membership.school, entity_type=JOB, deleted=False)
    return any(
        r.payload.get("status") == "active"
        and r.payload.get("membershipId") == str(membership.id)
        and duty in (r.payload.get("duties") or [])
        for r in rows
    )

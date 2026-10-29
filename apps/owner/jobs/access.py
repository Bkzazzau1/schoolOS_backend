"""Whether someone holds a duty the owner gave them through a job assignment.

An assignment gives power only while it is `active` and linked to this login. The
server sets both when the person's account is linked to their staff record, so an
assignment nobody has claimed gives none.
"""

from apps.sync.models import SyncRecord
from apps.structure.constants import SECTION

JOB = "owner_job_assignment"


def _active_assignments(membership):
    rows = SyncRecord.objects.filter(school=membership.school, entity_type=JOB, deleted=False)
    return [
        r.payload
        for r in rows
        if r.payload.get("status") == "active" and r.payload.get("membershipId") == str(membership.id)
    ]


def has_duty(membership, duty: str) -> bool:
    return any(duty in (payload.get("duties") or []) for payload in _active_assignments(membership))


def campus_scope_for(membership) -> str | None:
    """The one real campus this membership's authority is limited to, from a real active job
    assignment the owner gave them - either a direct campus grant, or a sectionHead assignment
    whose section itself has a real campus. None means whole-school: the default for everyone
    until the owner sets a scoped assignment up, so nothing changes for a school that never does."""
    for payload in _active_assignments(membership):
        campus_id = payload.get("campusId")
        if campus_id:
            return campus_id
        section_id = payload.get("sectionId")
        if not section_id:
            continue
        section = SyncRecord.objects.filter(
            school=membership.school, entity_type=SECTION, entity_id=section_id, deleted=False
        ).first()
        if section and section.payload.get("campusId"):
            return section.payload["campusId"]
    return None

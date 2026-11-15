"""Whether someone holds a duty the owner gave them through a job assignment.

An assignment gives power only while it is `active` and linked to this login. The
server sets both when the person's account is linked to their staff record, so an
assignment nobody has claimed gives none.
"""

from apps.core.errors import Rejected
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


def stage_scope_for(membership) -> str | None:
    """The one academic stage (a real section's own "stage" - Nursery, Primary,
    Secondary, ...) this membership's authority is limited to, from a real active
    sectionHead job assignment the owner gave them. None means whole-school: the
    default for everyone until the owner sets one up, so nothing changes for a
    school that never does.

    This is what lets anyone - not only a Principal - be scoped to one stage's own
    data: the owner assigns them as sectionHead of a Nursery or Primary section the
    same way a Secondary sectionHead assignment would, through the same Job
    Assignments screen, and apps.academics.curriculum_services enforces it the same
    way it already enforces Principal's own Secondary-only scope.
    """
    for payload in _active_assignments(membership):
        if payload.get("role") != "sectionHead":
            continue
        section_id = payload.get("sectionId")
        if not section_id:
            continue
        section = SyncRecord.objects.filter(
            school=membership.school, entity_type=SECTION, entity_id=section_id, deleted=False
        ).first()
        if section and section.payload.get("stage"):
            return section.payload["stage"]
    return None


def require_stage_access(membership, stage: str, *, action: str = "manage") -> None:
    """Raises Rejected unless this membership may act on data belonging to this one
    academic stage (Nursery, Primary, Secondary, ...).

    A Principal is always scoped to Secondary - the one hardcoded case, since that
    role exists for exactly this. Anyone else is scoped only if the owner gave them
    an active sectionHead job assignment for a section of a different stage (see
    stage_scope_for) - everyone else, including the owner, is whole-school by
    default, so nothing changes for a school that never sets one up.
    """
    if membership.role == "principal":
        allowed = "secondary"
    else:
        allowed = stage_scope_for(membership)
    if allowed and stage.strip().lower() != allowed.strip().lower():
        raise Rejected(f"This membership can only {action} {allowed.title()} records.")

from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice, text
from apps.schools.models import Role
from apps.sync.models import SyncRecord
from apps.sync.registry import EntityHandler, MutationContext

from .constants import APPOINTMENT, APPOINTMENT_READERS, DEPUTY, HOD, LEVELS, SECTION, SECTION_HEAD

MAX_CHAIN = 20


class AppointmentHandler(EntityHandler):
    """A leadership post in a section: its head, deputies, heads of department
    and coordinators.

    The rules the app checks on the device are kept here, so no other device can
    break them: the section exists, it has one head, everyone else reports to a
    head or deputy of the same section, an HOD has a department, and nobody
    reports in a circle. A post's section and level are fixed once created (to
    change either, appoint someone new), and a post cannot be deleted.
    """

    entity_type = APPOINTMENT
    roles = frozenset({Role.PROPRIETOR})

    def visible(self, membership, payload):
        return payload if membership.role in APPOINTMENT_READERS else None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        p, school = ctx.payload, ctx.membership.school
        if text(p, "id", max_len=128) != ctx.entity_id:
            raise Rejected("id must match the record.")
        level = choice(p.get("level"), LEVELS, "level")
        section_id = text(p, "sectionId", max_len=128)
        old = ctx.existing
        if old and (old["level"] != level or old["sectionId"] != section_id):
            raise Rejected("A post's section and level cannot change. Appoint someone new instead.")

        # Lock the section so two devices cannot both make a head at the same moment.
        section = SyncRecord.objects.select_for_update().filter(
            school=school, entity_type=SECTION, entity_id=section_id, deleted=False
        ).first()
        if section is None:
            raise Rejected("That section does not exist.")
        others = {
            r.entity_id: r.payload
            for r in SyncRecord.objects.filter(school=school, entity_type=APPOINTMENT, deleted=False)
            if r.entity_id != ctx.entity_id
        }

        department = text(p, "department", max_len=120) if level == HOD else ""
        if level == HOD and not department:
            raise Rejected("Enter a department for a head of department.")
        reports_to = None
        if level == SECTION_HEAD:
            if any(o["sectionId"] == section_id and o["level"] == SECTION_HEAD for o in others.values()):
                raise Rejected("This section already has a Section Head. Replace them instead of adding a second.")
        else:
            reports_to = text(p, "reportsTo", max_len=128)
            manager = others.get(reports_to)
            if manager is None or manager["sectionId"] != section_id or manager["level"] not in (SECTION_HEAD, DEPUTY):
                raise Rejected("Choose a reporting manager from the same section: its head or a deputy.")
            self._no_loop(others, ctx.entity_id, reports_to)

        return {
            "id": ctx.entity_id,
            "person": text(p, "person", max_len=120),
            "title": text(p, "title", max_len=120),
            "level": level,
            "sectionId": section_id,
            "department": department or None,
            "reportsTo": reports_to,
            "updatedByMembershipId": str(ctx.membership.id),
            "updatedAt": ctx.now,
        }

    @staticmethod
    def _no_loop(others: dict, own_id: str, manager_id: str) -> None:
        seen, current = {own_id}, manager_id
        for _ in range(MAX_CHAIN):
            if current is None:
                return
            if current in seen:
                raise Rejected("A post cannot report to itself, directly or through others.")
            seen.add(current)
            current = (others.get(current) or {}).get("reportsTo")

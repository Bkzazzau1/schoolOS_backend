from apps.staff.constants import DIRECTORY
from apps.structure.constants import APPOINTMENT, SECTION, SECTION_HEAD

from .data import payloads


def _names(section: dict) -> set[str]:
    """The ways a staff record may name this section (its name, stage or id)."""
    return {section[k].strip().lower() for k in ("name", "stage", "id") if section.get(k)}


def summary(school) -> dict:
    sections = payloads(school, SECTION)
    heads = {a["sectionId"]: a for a in payloads(school, APPOINTMENT) if a["level"] == SECTION_HEAD}
    people = payloads(school, DIRECTORY)
    counted: set[str] = set()
    rows = []
    for section in sections:
        mine = [p for p in people if (p.get("section") or "").strip().lower() in _names(section)]
        counted |= {p["_id"] for p in mine}
        head = heads.get(section["id"])
        rows.append({
            "id": section["id"], "name": section["name"], "stage": section["stage"], "campus": section["campus"],
            "classes": section["classes"], "staff": len(mine),
            "head": {"name": head["person"], "title": head["title"]} if head else None,
        })
    return {"sections": rows, "staffInNoSection": sum(1 for p in people if p["_id"] not in counted)}

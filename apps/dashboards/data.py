"""Reading the school's records for the dashboards.

Everything a dashboard shows is worked out here from the records the server really
holds. Nothing is estimated, and nothing is invented for data the server does not
have yet (students, attendance, results, fee payments).
"""

from apps.sync.models import SyncRecord


def payloads(school, entity_type: str) -> list[dict]:
    rows = SyncRecord.objects.filter(school=school, entity_type=entity_type, deleted=False).order_by("entity_id")
    return [{**r.payload, "_id": r.entity_id} for r in rows]

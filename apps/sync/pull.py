"""Devices downloading records.

A device remembers the last change number it read (its cursor) and asks for
everything after it. Each record carries the school's change number from its last
change, so a page is "the next N changes", in order, and nothing can be skipped
(see models.next_seq).

What a person receives is decided per record type by the handler's `visible`. A
record they may not see is simply not sent, but the cursor still moves past it.
"""

from dataclasses import dataclass, field

from django.conf import settings

from . import registry
from .models import SyncRecord

DEFAULT_LIMIT = 200
MAX_LIMIT = 500


@dataclass
class Page:
    records: list[dict] = field(default_factory=list)
    cursor: int = 0
    has_more: bool = False


def _visible_payload(membership, entity_type: str, payload: dict):
    handler = registry.get(entity_type)
    if handler is None:
        # Kinds nobody has written rules for exist only while developing, and only the owner reads them.
        if settings.SYNC_ALLOW_UNLISTED_ENTITY_TYPES and membership.role == "proprietor":
            return payload
        return None
    return handler.visible(membership, payload)


def pull(membership, since: int, limit: int = DEFAULT_LIMIT) -> Page:
    """The changes after `since` that this membership may see, oldest first."""
    limit = max(1, min(limit, MAX_LIMIT))
    rows = list(SyncRecord.objects.filter(school=membership.school, seq__gt=since).order_by("seq")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    page = Page(cursor=rows[-1].seq if rows else since, has_more=has_more)
    for row in rows:
        payload = _visible_payload(membership, row.entity_type, row.payload)
        if payload is None:
            continue
        page.records.append(
            {
                "entityType": row.entity_type,
                "entityId": row.entity_id,
                "version": row.version,
                "deleted": row.deleted,
                "payload": {} if row.deleted else payload,
            }
        )
    return page

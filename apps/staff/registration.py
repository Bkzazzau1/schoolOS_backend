"""A staff member completing their own registration.

The app does this through `sync/push/` (the profile handler). The web page and the
`staff/me/onboarding/` endpoint do it through `submit` here. `submit` does not have
its own rules: it builds the change and runs it through the same profile handler,
so the two ways in cannot drift apart. Same validation, same phone/NIN uniqueness,
same "only the linked person, only while a request is open".
"""

import copy
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.core.errors import Rejected
from apps.schools.models import Membership
from apps.sync import records, registry
from apps.sync.models import SyncRecord

from .constants import INVITE_PENDING, PROFILE, SUBMITTED
from .profiles.sections import PERSONAL_KEYS
from .profiles.rules import SCHOOL_CONTROLLED

NO_REQUEST = "There is no open registration request for you."


def open_request(membership: Membership) -> SyncRecord | None:
    """The profile record waiting for this membership to register, if any."""
    records_ = SyncRecord.objects.filter(school=membership.school, entity_type=PROFILE, deleted=False)
    for record in records_:
        p = record.payload
        if p.get("linkedMembershipId") == str(membership.id) and p.get("onboardingStatus") == INVITE_PENDING:
            return record
    return None


def find_open_request(user, membership_id=None) -> tuple[Membership, SyncRecord] | None:
    """The first of the person's memberships that has a registration waiting."""
    memberships = Membership.objects.filter(user=user, is_active=True, school__is_active=True).select_related("school")
    if membership_id is not None:
        try:
            memberships = memberships.filter(id=UUID(str(membership_id)))
        except ValueError:
            return None
    for membership in memberships:
        record = open_request(membership)
        if record is not None:
            return membership, record
    return None


def submit(membership: Membership, *, personal: dict, payment: dict, documents: dict | None = None) -> str:
    """Submit the registration for review. Returns the staff id.

    `documents` maps a required document's name to a note saying how it was
    provided; a document with a note is marked received.
    """
    record = open_request(membership)
    if record is None:
        raise Rejected(NO_REQUEST)
    old = record.payload
    payload = copy.deepcopy(old)

    merged = {**old.get("personal", {})}
    for key in PERSONAL_KEYS:
        # Employment terms are the school's to set, so they are never taken from here.
        if key in personal and key not in SCHOOL_CONTROLLED:
            merged[key] = personal[key]
    payload["personal"] = merged
    payload["payment"] = payment or {}
    notes = documents or {}
    payload["documents"] = [
        {**d, "status": "received", "reference": str(notes[d["name"]]).strip()}
        if d.get("status") == "requested" and str(notes.get(d["name"], "")).strip()
        else d
        for d in old.get("documents", [])
    ]
    payload["onboardingStatus"] = SUBMITTED

    handler = registry.get(PROFILE)
    ctx = registry.MutationContext(
        membership=membership, operation="update", entity_type=PROFILE, entity_id=record.entity_id,
        payload=payload, existing=old, now=timezone.now().isoformat(),
    )
    with transaction.atomic():
        handler.authorize(ctx)
        stored = handler.clean(ctx)
        records.write(membership.school, PROFILE, record.entity_id, stored, by=membership)
        handler.after_write(ctx, stored)
    return record.entity_id

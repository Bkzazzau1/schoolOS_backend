from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.core.errors import Rejected
from apps.core.permissions import active_membership

from . import registry
from .models import MutationLog, SyncRecord

ACCEPTED = "accepted"
CONFLICT = "conflict"
REJECTED = "rejected"


@dataclass(frozen=True)
class Outcome:
    disposition: str
    server_version: int | None = None
    message: str = ""


def apply_mutation(user, data: dict) -> Outcome:
    """Decide one queued mutation from the app and apply it if allowed.

    Returns accepted, conflict (someone else changed the record first) or
    rejected (not allowed or not valid). The same mutation id always gets the
    same answer, so retrying after a lost response is safe.

    Raises PermissionDenied when the caller has no such membership. That is a
    problem with the caller's identity rather than with the mutation, so it is
    not recorded as a decision.
    """
    membership = active_membership(
        user, data["tenantId"], membership_id=data["membershipId"]
    )
    if membership is None:
        raise PermissionDenied("You are not a member of this school.")

    with transaction.atomic():
        previous = MutationLog.objects.filter(
            school=membership.school, mutation_id=data["id"]
        ).first()
        if previous is not None:
            if previous.membership_id != membership.id:
                return Outcome(REJECTED, message="This mutation id is already in use.")
            return Outcome(previous.disposition, previous.server_version, previous.message)

        outcome = _decide(membership, data)
        MutationLog.objects.create(
            school=membership.school,
            mutation_id=data["id"],
            membership=membership,
            entity_type=data["entityType"],
            entity_id=data["entityId"],
            operation=data["operation"],
            disposition=outcome.disposition,
            server_version=outcome.server_version,
            message=outcome.message,
        )
        return outcome


def _decide(membership, data: dict) -> Outcome:
    entity_type = data["entityType"]
    entity_id = data["entityId"]
    operation = data["operation"]
    base_version = data.get("baseVersion")

    handler = registry.get(entity_type)
    if handler is None and not settings.SYNC_ALLOW_UNLISTED_ENTITY_TYPES:
        return Outcome(REJECTED, message="This kind of record is not accepted yet.")

    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=membership.school, entity_type=entity_type, entity_id=entity_id)
        .first()
    )
    ctx = registry.MutationContext(
        membership=membership,
        operation=operation,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=data.get("payload") or {},
        existing=record.payload if record is not None and not record.deleted else None,
        now=timezone.now().isoformat(),
    )

    try:
        if handler is not None:
            handler.authorize(ctx)
        if operation == "create":
            return _create(membership, record, ctx, handler)
        return _change(membership, record, ctx, handler, base_version)
    except Rejected as rejected:
        return Outcome(REJECTED, message=rejected.message)


def _stored_payload(handler, ctx):
    return handler.clean(ctx) if handler is not None else ctx.payload


def _create(membership, record, ctx, handler) -> Outcome:
    if record is not None:
        return Outcome(CONFLICT, record.version, "This record already exists on the server.")
    payload = _stored_payload(handler, ctx)
    try:
        with transaction.atomic():
            created = SyncRecord.objects.create(
                school=membership.school,
                entity_type=ctx.entity_type,
                entity_id=ctx.entity_id,
                payload=payload,
                updated_by=membership,
            )
            if handler is not None:
                handler.after_write(ctx, payload)  # a Rejected here undoes the create
    except IntegrityError:
        # Another device created it at the same moment.
        existing = SyncRecord.objects.get(
            school=membership.school, entity_type=ctx.entity_type, entity_id=ctx.entity_id
        )
        return Outcome(CONFLICT, existing.version, "This record already exists on the server.")
    return Outcome(ACCEPTED, created.version)


def _change(membership, record, ctx, handler, base_version) -> Outcome:
    if record is None:
        return Outcome(REJECTED, message="This record does not exist on the server.")
    if record.deleted:
        return Outcome(CONFLICT, record.version, "This record was deleted on the server.")
    # A changed record is a conflict only when the app says which version it
    # started from. Chained offline edits (create then update before the first
    # sync) legitimately have no base version.
    if base_version is not None and base_version != record.version:
        return Outcome(CONFLICT, record.version, "This record changed on the server first.")

    stored = _stored_payload(handler, ctx) if ctx.operation == "update" else None
    with transaction.atomic():
        if stored is not None:
            record.payload = stored
        else:
            record.deleted = True
        record.version += 1
        record.updated_by = membership
        record.save()
        if stored is not None and handler is not None:
            handler.after_write(ctx, stored)  # a Rejected here undoes the update
    return Outcome(ACCEPTED, record.version)

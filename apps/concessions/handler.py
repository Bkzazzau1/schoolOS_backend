from datetime import datetime
from typing import Any

from django.utils.dateparse import parse_datetime

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.notifications.services import notify
from apps.owner.jobs.access import has_duty
from apps.schools.models import Membership
from apps.sync.registry import EntityHandler, MutationContext

from .constants import (
    APPROVED, CONCESSION, DECISIONS, DECLINED, ID, MAX_FEE, MAX_NOTE, PENDING, ROLE_LABELS, SUBMIT_DUTY, TYPES,
)


def _date(iso: str) -> str:
    """The short date the app shows, like "02 Sep 2026"."""
    return datetime.strftime(parse_datetime(iso), "%d %b %Y")


def can_submit(membership: Membership) -> bool:
    return membership.role in ("proprietor", "accountant") or has_duty(membership, SUBMIT_DUTY)


class ConcessionHandler(EntityHandler):
    """A scholarship or discount on a student's fee: someone in finance asks, the owner decides.

    Nothing reduces a family's fee until the owner approves it. The request's amounts,
    student and type are fixed once it is sent, so what the owner approves is exactly
    what was asked. Who asked, who decided and when are set by the server, and a
    decision is final. Requests cannot be deleted.
    """

    entity_type = CONCESSION

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A concession request cannot be deleted.")
        if ctx.operation == "create" and not can_submit(ctx.membership):
            raise Rejected("Only the owner, Finance Office, or someone given this duty can ask for a concession.")
        if ctx.operation == "update" and ctx.membership.role != "proprietor":
            raise Rejected("Only the owner can decide a concession request.")

    def visible(self, membership, payload):
        if (
            can_submit(membership)
            or payload.get("requestedByMembershipId") == str(membership.id)
        ):
            return payload
        return None

    def clean(self, ctx: MutationContext) -> dict[str, Any]:
        if not ID.match(ctx.entity_id):
            raise Rejected("That request number is not valid.")
        if text(ctx.payload, "id", max_len=64) != ctx.entity_id:
            raise Rejected("id must match the record.")
        return self._decide(ctx) if ctx.existing else self._create(ctx)

    def _create(self, ctx: MutationContext) -> dict[str, Any]:
        p = ctx.payload
        if p.get("status") != PENDING:
            raise Rejected("A new request starts as pending approval.")
        kind = choice(p.get("type"), TYPES, "type")
        gross = integer(p, "grossFee", maximum=MAX_FEE)
        amount = integer(p, "amount", maximum=MAX_FEE)
        if amount <= 0:
            raise Rejected("Enter the amount to take off the fee.")
        if amount > gross:
            raise Rejected("The concession cannot be more than the term fee.")
        label = "Scholarship" if kind == "scholarship" else "Discount"
        return {
            "id": ctx.entity_id,
            "student": text(p, "student", max_len=120),
            "className": text(p, "className", max_len=60),
            "type": kind,
            "grossFee": gross,
            "amount": amount,
            "reason": text(p, "reason", max_len=MAX_NOTE, required=False) or f"{label} request",
            "requestedBy": text(p, "requestedBy", max_len=120),
            "requestedByRole": ROLE_LABELS.get(ctx.membership.role, ctx.membership.role.title()),
            "requestedByMembershipId": str(ctx.membership.id),
            "requestedAt": _date(ctx.now),
            "createdAt": ctx.now,
            "status": PENDING,
            "decidedBy": None,
            "decidedAt": None,
            "decisionNote": None,
        }

    def _decide(self, ctx: MutationContext) -> dict[str, Any]:
        old, p = ctx.existing, ctx.payload
        if old["status"] != PENDING:
            raise Rejected(f"This request was already {'approved' if old['status'] == APPROVED else 'declined'}.")
        status = choice(p.get("status"), DECISIONS, "status")
        note = text(p, "decisionNote", max_len=MAX_NOTE, required=status == DECLINED)
        return {
            **old,
            "status": status,
            "decidedBy": "Proprietor",
            "decidedByMembershipId": str(ctx.membership.id),
            "decidedAt": _date(ctx.now),
            "decidedAtIso": ctx.now,
            "decisionNote": note or None,
        }

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        money = f"₦{stored['amount']:,}"
        if ctx.operation == "create":
            for owner in Membership.objects.filter(school=school, role="proprietor", is_active=True).select_related("school"):
                if owner.id != ctx.membership.id:
                    notify(owner, "concession_request", "Concession to approve",
                           f"{stored['requestedBy']} asked for a {stored['type']} of {money} for {stored['student']}.",
                           {"requestId": ctx.entity_id})
            return
        requester = Membership.objects.filter(id=stored["requestedByMembershipId"]).select_related("school").first()
        if requester is not None and requester.id != ctx.membership.id:
            verdict = "approved" if stored["status"] == APPROVED else "declined"
            note = f" {stored['decisionNote']}" if stored["decisionNote"] else ""
            notify(requester, "concession_decided", f"Concession {verdict}",
                   f"The {stored['type']} of {money} for {stored['student']} was {verdict}.{note}",
                   {"requestId": ctx.entity_id, "status": stored["status"]})

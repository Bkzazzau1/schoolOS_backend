from datetime import datetime
from typing import Any
from uuid import UUID

from django.utils.dateparse import parse_datetime

from apps.core.errors import Rejected
from apps.core.validation import choice, integer, text
from apps.notifications.services import notify
from apps.owner.jobs.access import has_duty
from apps.receivables import adjustments, ledger
from apps.receivables.errors import Refused
from apps.receivables.models import ReceivableStatus, StudentReceivable
from apps.receivables.permissions import billing_authorities, can_manage_billing
from apps.schools.models import Membership
from apps.students.models import EnrollmentStatus, StudentEnrollment
from apps.sync.registry import EntityHandler, MutationContext

from .constants import (
    APPROVED, CONCESSION, DECISIONS, DECLINED, ID, MAX_FEE, MAX_NOTE, PENDING, ROLE_LABELS, SUBMIT_DUTY, TYPES,
)


def _date(iso: str) -> str:
    """The short date the app shows, like "02 Sep 2026"."""
    return datetime.strftime(parse_datetime(iso), "%d %b %Y")


def can_submit(membership: Membership) -> bool:
    return membership.role in ("proprietor", "accountant") or has_duty(membership, SUBMIT_DUTY)


def _display_name(membership: Membership) -> str:
    """Who a person is, for the record: their name, or failing that what they are at the school."""
    name = (membership.user.get_full_name() or "").strip()
    return name or ROLE_LABELS.get(membership.role, membership.role.title())


class ConcessionHandler(EntityHandler):
    """A scholarship or discount on a student's fee: someone in finance asks, and someone with billing
    authority decides - the owner, or whoever the owner has given the finance.billing_authority duty.

    Nothing reduces a family's fee until that person approves it. The request's amounts, student and
    type are fixed once it is sent, so what is approved is exactly what was asked. Who asked, who decided
    and when are set by the server (the real people, never a title), and a decision is final. Requests
    cannot be deleted.

    A request may name the charge it is for (`receivableId`). Then the student, class and fee come from
    the school's own records, not from the app, and approving it takes the amount off that charge in the
    receivables ledger - in the same transaction, so a request is never "approved" without its effect.
    A request that names no charge is the older kind: a decision on record, applied by hand.
    """

    entity_type = CONCESSION

    def authorize(self, ctx: MutationContext) -> None:
        if ctx.operation == "delete":
            raise Rejected("A concession request cannot be deleted.")
        if ctx.operation == "create" and not can_submit(ctx.membership):
            raise Rejected("Only the owner, Finance Office, or someone given this duty can ask for a concession.")
        if ctx.operation == "update" and not can_manage_billing(ctx.membership):
            raise Rejected("Only the owner, or someone the owner has given billing authority, can decide a concession request.")

    def visible(self, membership, payload):
        if (
            can_submit(membership)
            or can_manage_billing(membership)
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
        amount = integer(p, "amount", maximum=MAX_FEE)
        if amount <= 0:
            raise Rejected("Enter the amount to take off the fee.")
        # A request for a real charge takes the fee from the school's own records; only an older request that
        # names no charge relies on the figure the app typed.
        linked = self._linked_charge(ctx, amount)
        gross = linked["grossFee"] if linked else integer(p, "grossFee", maximum=MAX_FEE)
        if amount > gross:
            raise Rejected("The concession cannot be more than the term fee.")
        label = "Scholarship" if kind == "scholarship" else "Discount"
        return {
            "id": ctx.entity_id,
            "student": linked["student"] if linked else text(p, "student", max_len=120),
            "className": linked["className"] if linked else text(p, "className", max_len=60),
            "type": kind,
            "grossFee": linked["grossFee"] if linked else gross,
            "amount": amount,
            "reason": text(p, "reason", max_len=MAX_NOTE, required=False) or f"{label} request",
            "requestedBy": text(p, "requestedBy", max_len=120),
            "requestedByName": _display_name(ctx.membership),
            "requestedByRole": ROLE_LABELS.get(ctx.membership.role, ctx.membership.role.title()),
            "requestedByMembershipId": str(ctx.membership.id),
            "receivableId": linked["receivableId"] if linked else None,
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
            "decidedBy": _display_name(ctx.membership),
            "decidedByRole": ROLE_LABELS.get(ctx.membership.role, ctx.membership.role.title()),
            "decidedByMembershipId": str(ctx.membership.id),
            "decidedAt": _date(ctx.now),
            "decidedAtIso": ctx.now,
            "decisionNote": note or None,
        }

    def _linked_charge(self, ctx: MutationContext, amount_naira: int) -> dict | None:
        """The charge a request names, checked against the school's own records. None if it names none."""
        raw = ctx.payload.get("receivableId")
        if raw in (None, ""):
            return None
        try:
            receivable_id = UUID(str(raw))
        except ValueError:
            raise Rejected("That charge is not valid.")
        receivable = StudentReceivable.objects.select_related("student").filter(school=ctx.membership.school, id=receivable_id).first()
        if receivable is None or receivable.status == ReceivableStatus.VOID:
            raise Rejected("That charge was not found, or it has been voided.")
        parts = list(
            StudentReceivable.objects.filter(charge_key=receivable.charge_key, family=receivable.family).exclude(status=ReceivableStatus.VOID)
        )
        figures = ledger.positions(parts)
        payable = sum(figures[p.id].net for p in parts)
        if amount_naira * 100 > payable:
            raise Rejected("The concession cannot be more than what is still payable on this charge.")
        enrollment = StudentEnrollment.objects.filter(student=receivable.student, status=EnrollmentStatus.ACTIVE).first()
        return {
            "receivableId": str(receivable.id),
            "student": receivable.student.full_name[:120],
            "className": (enrollment.class_name if enrollment else "")[:60],
            "grossFee": sum(p.gross_amount_minor for p in parts) // 100,
        }

    def _apply_to_ledger(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        receivable = StudentReceivable.objects.filter(school=ctx.membership.school, id=stored["receivableId"]).first()
        requester = Membership.objects.filter(id=stored["requestedByMembershipId"]).select_related("school").first()
        if receivable is None:
            raise Rejected("The charge this request was for no longer exists.")
        try:
            adjustments.adjust_charge(
                receivable, kind=stored["type"], amount_minor=stored["amount"] * 100, reason=stored["reason"],
                actor=ctx.membership, requested_by=requester, source_ref=f"concession:{ctx.entity_id}",
                metadata={"concessionRequest": ctx.entity_id},
            )
        except Refused as refused:
            # The approval is undone with it: a request is never approved without its effect.
            raise Rejected(refused.message)

    def after_write(self, ctx: MutationContext, stored: dict[str, Any]) -> None:
        school = ctx.membership.school
        money = f"₦{stored['amount']:,}"
        if ctx.operation == "update" and stored["status"] == APPROVED and stored.get("receivableId"):
            self._apply_to_ledger(ctx, stored)
        if ctx.operation == "create":
            for authority in billing_authorities(school):
                if authority.id != ctx.membership.id:
                    notify(authority, "concession_request", "Concession to approve",
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

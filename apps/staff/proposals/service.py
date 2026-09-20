"""Deciding on a proposal. This is done on the server, in one transaction.

The app used to write the staff entry, the salary, the profile and the decision
from the approver's own phone. A phone cannot be trusted to do all of that, or to
stop halfway, so approving is now one server action: everything happens, or
nothing does.
"""

import hashlib

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from apps.core.errors import Rejected
from apps.notifications.services import notify
from apps.schools.models import Membership
from apps.sync import records

from .. import identity
from ..authority import can_approve_staff
from ..constants import (
    APPROVED, DEFAULT_DOCUMENTS, DELEGATE_ROLES, DIRECTORY, INVITE_PENDING, PENDING, PROFILE,
    PROPOSAL, REJECTED, SALARY, SYSTEM_ROLES,
)
from ..signals import registration_requested, staff_approved

MAX_SALARY = 1_000_000_000


def staff_id_for(proposal_id: str) -> str:
    """The staff id is derived from the proposal id, so it never depends on chance."""
    return "STAFF-" + hashlib.sha256(proposal_id.encode()).hexdigest()[:16]


def _load_pending(actor: Membership, proposal_id: str):
    from apps.sync.models import SyncRecord

    record = (
        SyncRecord.objects.select_for_update()
        .filter(school=actor.school, entity_type=PROPOSAL, entity_id=proposal_id, deleted=False)
        .first()
    )
    if record is None:
        raise Rejected("Proposal not found.")
    return record


def _require_approver(actor: Membership, proposal: dict) -> bool:
    """Owner, or someone the owner assigned. Returns True for the owner."""
    if not can_approve_staff(actor):
        raise PermissionDenied("Only the owner, or someone the owner has authorized, can decide on staff.")
    is_owner = actor.role == "proprietor"
    if not is_owner and proposal.get("proposedByMembershipId") == str(actor.id):
        raise Rejected("You proposed this staff member, so someone else must decide it.")
    return is_owner


def _tell_proposer(school, proposal: dict, title: str, message: str, proposal_id: str) -> None:
    proposer = Membership.objects.filter(id=proposal.get("proposedByMembershipId"), school=school).first()
    if proposer is not None:
        notify(proposer, "staff_proposal_decided", title, message, {"proposalId": proposal_id})


def approve(actor: Membership, proposal_id: str, *, gross=None, deductions=None, system_role=None) -> dict:
    """Make the proposed person a staff member: directory entry, salary on payroll,
    profile with their registration request. Returns {"staffId", "alreadyApproved"}."""
    school = actor.school
    with transaction.atomic():
        record = _load_pending(actor, proposal_id)
        p = record.payload
        if p["status"] == APPROVED:
            return {"staffId": p["createdStaffId"], "alreadyApproved": True}
        if p["status"] != PENDING:
            raise Rejected("This proposal was rejected.")
        is_owner = _require_approver(actor, p)

        role = system_role or p.get("systemRole") or ""
        if role not in SYSTEM_ROLES:
            raise Rejected("Choose the role for this staff member first.")
        if not is_owner:
            if system_role is not None and system_role != p.get("systemRole"):
                raise Rejected("Only the owner can change the proposed role.")
            if role not in DELEGATE_ROLES:
                raise Rejected(f"Only the owner can approve a {SYSTEM_ROLES[role]}.")
            if (gross is not None and gross != p["gross"]) or (deductions is not None and deductions != p["deductions"]):
                raise Rejected("Only the owner can change the proposed salary.")
        final_gross = p["gross"] if gross is None else gross
        final_deductions = p["deductions"] if deductions is None else deductions
        if isinstance(final_gross, bool) or not isinstance(final_gross, int) or not 0 < final_gross <= MAX_SALARY:
            raise Rejected("Enter a gross salary above zero.")
        if isinstance(final_deductions, bool) or not isinstance(final_deductions, int) or not 0 <= final_deductions <= final_gross:
            raise Rejected("Deductions cannot exceed the gross salary.")

        staff_id = staff_id_for(proposal_id)
        now = _now()
        # The numbers stay unique: they pass from the proposal to the new staff member.
        identity.transfer(school, "proposal", proposal_id, "staff", staff_id, p["name"])
        identity.set_claims(school, "staff", staff_id, p["name"], phone=p["phone"], nin=p["nin"])

        records.write(school, DIRECTORY, staff_id, {
            "id": staff_id, "name": p["name"], "role": p["roleTitle"], "section": p["workArea"],
            "fileStatus": "Missing document", "staffCategory": "approved", "systemRole": role,
            "approvedFromProposal": proposal_id, "createdByMembershipId": str(actor.id), "createdAt": now,
        }, by=actor)
        records.write(school, SALARY, staff_id, {
            "staffId": staff_id, "name": p["name"], "role": p["roleTitle"],
            "gross": final_gross, "deductions": final_deductions, "onPayroll": True,
            "history": [{"at": now, "gross": final_gross, "deductions": final_deductions,
                         "onPayroll": True, "byMembershipId": str(actor.id)}],
            "updatedAt": now,
        }, by=actor)
        records.write(school, PROFILE, staff_id, new_profile(staff_id, p, role, actor, now), by=actor)
        records.write(school, PROPOSAL, proposal_id, {
            **p, "status": APPROVED, "createdStaffId": staff_id, "approvedSystemRole": role,
            "approvedGross": final_gross, "approvedDeductions": final_deductions,
            "decidedByMembershipId": str(actor.id), "decidedByRole": actor.role, "decidedAt": now,
        }, by=actor)
        _tell_proposer(school, p, "Staff proposal approved",
                       f"{p['name']} was approved as {SYSTEM_ROLES[role]}. Their registration request has been queued.",
                       proposal_id)
    # After the transaction commits, so a listener that fails cannot undo the approval.
    staff_approved.send(
        sender=None, school=school, staff_id=staff_id, email=p["email"], system_role=role,
        name=p["name"], proposal_id=proposal_id,
    )
    registration_requested.send(
        sender=None, school=school, staff_id=staff_id, email=p["email"], system_role=role,
        name=p["name"], requested_by=actor,
    )
    return {"staffId": staff_id, "alreadyApproved": False}


def reject(actor: Membership, proposal_id: str, note: str = "") -> None:
    school = actor.school
    if len(note) > 200:
        raise Rejected("The reason is too long.")
    with transaction.atomic():
        record = _load_pending(actor, proposal_id)
        p = record.payload
        if p["status"] != PENDING:
            raise Rejected("This proposal has already been decided.")
        _require_approver(actor, p)
        identity.release(school, "proposal", proposal_id)  # their numbers are free again
        records.write(school, PROPOSAL, proposal_id, {
            **p, "status": REJECTED, "decisionNote": note.strip(),
            "decidedByMembershipId": str(actor.id), "decidedByRole": actor.role, "decidedAt": _now(),
        }, by=actor)
        reason = f" Reason: {note.strip()}" if note.strip() else ""
        _tell_proposer(school, p, "Staff proposal declined", f"{p['name']} was not approved.{reason}", proposal_id)


def new_profile(staff_id: str, proposal: dict, role: str, actor: Membership, now: str) -> dict:
    """The profile a newly approved staff member starts with: their phone, NIN and
    email from the proposal, the documents to collect, and a registration request
    waiting to be sent."""
    return {
        "staffId": staff_id,
        "personal": {
            "phone": proposal["phone"], "nin": proposal["nin"], "email": proposal["email"], "address": "",
            "dateOfBirth": "", "gender": "", "stateOfOrigin": "", "nextOfKinName": "",
            "nextOfKinPhone": "", "employmentDate": "", "employmentType": "",
        },
        "academics": [], "credentials": [], "reviews": [],
        "payment": {"bankName": "", "accountName": "", "accountNumber": ""},
        "documents": [{"name": n, "status": "requested", "reference": ""} for n in DEFAULT_DOCUMENTS],
        "onboardingStatus": INVITE_PENDING,
        "onboardingEmail": proposal["email"],
        "linkedMembershipId": "",
        "systemRole": role,
        "updatedAt": now,
        "updatedByMembershipId": str(actor.id),
    }


def _now() -> str:
    from django.utils import timezone

    return timezone.now().isoformat()

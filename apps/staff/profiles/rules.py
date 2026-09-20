"""Who may change which part of a staff profile.

There are four kinds of person, and one person can be more than one:

  editor    the owner and the principal: everything except bank details.
  inviter   editors and the administrator: may send the registration request.
  self      the staff member themselves (their login is linked to the record):
            their own details, their bank account, and which documents they have
            provided. Nothing else.
  anyone else: nothing.

Each function takes the stored section (`old`), the section the app sent, cleaned
(`new`), and who is asking. It returns nothing and raises Rejected if the change
is not allowed. If `new == old`, nothing changed and everything is allowed.
"""

from dataclasses import dataclass

from apps.core.errors import Rejected
from apps.core.identity import normalize_nin, normalize_phone

from ..constants import (
    DEFAULT_DOCUMENTS, EDITOR_ROLES, INVITE_PENDING, INVITER_ROLES, NONE, REVIEWED, SUBMITTED,
)

#: Set by the school, so the staff member cannot change them about themselves.
SCHOOL_CONTROLLED = ("employmentDate", "employmentType")


@dataclass(frozen=True)
class Who:
    membership_id: str
    role: str
    linked_id: str

    @property
    def editor(self) -> bool:
        return self.role in EDITOR_ROLES

    @property
    def inviter(self) -> bool:
        return self.role in INVITER_ROLES

    @property
    def self_(self) -> bool:
        return bool(self.linked_id) and self.linked_id == self.membership_id


def check_personal(old: dict, new: dict, who: Who) -> None:
    if new == old or who.editor:
        return
    if who.self_:
        if any(new[k] != old[k] for k in SCHOOL_CONTROLLED):
            raise Rejected("Your employment date and type are set by the school.")
        return
    raise Rejected("You cannot change this staff member's personal details.")


def check_editor_only(old, new, who: Who, what: str) -> None:
    if new != old and not who.editor:
        raise Rejected(f"Only the owner or principal can change {what}.")


def check_payment(old: dict, new: dict, who: Who) -> None:
    if new != old and not who.self_:
        raise Rejected("Only the staff member can change their own bank details.")


def check_documents(old: list, new: list, who: Who, *, inviting: bool) -> None:
    if new == old or who.editor:
        return
    if who.self_:
        if [d["name"] for d in new] != [d["name"] for d in old]:
            raise Rejected("You cannot add or remove required documents.")
        for before, after in zip(old, new):
            if before != after and not (
                before["status"] == "requested" and after["status"] == "received" and after["reference"]
            ):
                raise Rejected("You can only mark a requested document as provided, and say how.")
        return
    if who.inviter and inviting:
        added = new[len(old):]
        if new[: len(old)] == old and all(
            d["name"] in DEFAULT_DOCUMENTS and d["status"] == "requested" and not d["reference"] for d in added
        ):
            return
        raise Rejected("A registration request may only add the standard required documents.")
    raise Rejected("You cannot change this staff member's documents.")


def check_reviews(old: list, new: list, who: Who, *, now: str) -> list:
    """Reviews only ever grow. The past cannot change, and each new one is stamped
    with who wrote it and when by the server, not by the app."""
    key = lambda r: (r.get("period"), r.get("rating"), r.get("notes", ""))  # noqa: E731
    if [key(r) for r in new[: len(old)]] != [key(r) for r in old] or len(new) < len(old):
        raise Rejected("Performance reviews cannot be changed or removed, only added.")
    added = new[len(old):]
    if added and not who.editor:
        raise Rejected("Only the owner or principal can add performance reviews.")
    return old + [
        {**r, "at": now, "reviewerRole": who.role, "reviewerMembershipId": who.membership_id} for r in added
    ]


def check_onboarding(
    old_status: str, old_email: str, new_status: str, new_email: str, who: Who, personal: dict, payment: dict
) -> None:
    """The registration request moves none/reviewed/submitted -> invitePending
    (sent), invitePending -> submitted (the staff member), and -> reviewed (owner
    or principal)."""
    if (new_status, new_email) == (old_status, old_email):
        return
    if new_status == INVITE_PENDING:
        if not who.inviter:
            raise Rejected("You cannot send a registration request.")
        if not new_email:
            raise Rejected("Enter the staff member's email address.")
        return
    if new_email != old_email:
        raise Rejected("The registration email can only be changed by sending a new request.")
    if new_status == SUBMITTED:
        if not who.self_:
            raise Rejected("Only the staff member can submit their own registration.")
        if old_status != INVITE_PENDING:
            raise Rejected("There is no open registration request for you.")
        _require_complete(personal, payment)
        return
    if new_status == REVIEWED:
        if not who.editor:
            raise Rejected("Only the owner or principal can mark a registration as reviewed.")
        if old_status == NONE:
            raise Rejected("No registration request has been sent.")
        return
    raise Rejected("That registration status change is not allowed.")


def _require_complete(personal: dict, payment: dict) -> None:
    """What the staff member must have filled in before they can submit."""
    if not (
        normalize_phone(personal["phone"]) and normalize_nin(personal["nin"])
        and personal["address"] and personal["dateOfBirth"]
        and personal["nextOfKinName"] and normalize_phone(personal["nextOfKinPhone"])
    ):
        raise Rejected(
            "Enter your phone, NIN, address, date of birth and your next of kin with a valid phone number."
        )
    if not (payment["bankName"] and payment["accountName"] and payment["accountNumber"]):
        raise Rejected("Enter your bank, the account name and a 10-digit account number.")

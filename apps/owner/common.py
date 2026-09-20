from typing import Any

from apps.core.errors import Rejected
from apps.core.validation import choice

PENDING = "pendingActivation"
ACTIVE = "active"
REVOKED = "revoked"


def resolve_status(payload: dict, existing: dict[str, Any] | None) -> str:
    """The status an owner's record may take.

    The owner can leave a grant waiting for the person to activate their
    account, or revoke it. Only the server makes a grant active, when the
    person's account is linked. The app echoes 'active' back when it edits a
    record that already is, and that is accepted only if it really is.
    """
    status = choice(payload.get("status"), {PENDING, ACTIVE, REVOKED}, "status")
    if status == ACTIVE and (existing or {}).get("status") != ACTIVE:
        raise Rejected("Only activating the person's account can make this active.")
    return status


def server_owned_link(existing: dict[str, Any] | None) -> str | None:
    """The account link is never taken from the app: it is kept as the server
    has it, and set only when an account is linked."""
    return (existing or {}).get("membershipId")

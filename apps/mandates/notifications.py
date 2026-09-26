"""Telling people what happens to a mandate or a debit. Best effort: a notification that cannot be made never undoes what happened, and none carries a
bank account number or a credential.

Two audiences: the PAYER (the guardian whose mandate it is, if they have a signed-in account) is told what they must do and what was done to their
account; the people who manage mandates are told what needs a person. Whether a payer must be given notice before a debit, and how long before, is a
legal and provider matter SchoolOS has not verified, so no notice period is assumed here: what is sent is a plain statement of what happened.
"""

import logging

from apps.notifications.services import notify_many
from apps.schools.models import Membership, Role

from . import permissions
from .constants import DEFAULT_CURRENCY

logger = logging.getLogger(__name__)


def _money(minor: int) -> str:
    return f"{DEFAULT_CURRENCY} {minor / 100:,.2f}"


def payer_recipients(payer) -> list[Membership]:
    """The payer's own signed-in parent account, if they have one."""
    user_id = payer.guardian.account_user_id
    if not user_id:
        return []
    return list(Membership.objects.select_related("school").filter(school=payer.school, role=Role.PARENT, is_active=True, user_id=user_id))


def _tell(recipients, kind: str, title: str, message: str, data: dict) -> None:
    try:
        if recipients:
            notify_many(recipients, kind, title, message, data)
    except Exception:  # noqa: BLE001 - telling someone must never undo what happened
        logger.exception("A mandate notification could not be made")


def _data(mandate) -> dict:
    return {"mandateId": str(mandate.id), "familyId": str(mandate.family_id)}


def needs_authorisation(mandate) -> None:
    _tell(
        payer_recipients(mandate.payer), "mandate_authorisation_needed", "Direct debit: please review and authorise",
        f"{mandate.family.display_name}: review the direct debit mandate on your {mandate.bank_name or 'bank'} account ({mandate.account_mask}) "
        "and authorise it yourself if you agree. Nothing is debited until you do and your bank has activated it.", _data(mandate),
    )


def needs_activation(mandate) -> None:
    _tell(
        payer_recipients(mandate.payer), "mandate_activation_needed", "Direct debit: activate your mandate",
        f"Your direct debit mandate ({mandate.account_mask}) is set up but not active yet. Follow the activation steps in the app.", _data(mandate),
    )


def activated(mandate) -> None:
    _tell(payer_recipients(mandate.payer), "mandate_activated", "Direct debit mandate active",
          f"Your direct debit mandate on {mandate.account_mask} is active.", _data(mandate))
    _tell(permissions.recipients(mandate.school), "mandate_activated", "A mandate is now debit-ready",
          f"{mandate.family.display_name}: the mandate on {mandate.account_mask} is debit-ready.", _data(mandate))


def problem(mandate, what: str) -> None:
    _tell(payer_recipients(mandate.payer), "mandate_problem", "Direct debit mandate: something needs attention", what, _data(mandate))
    _tell(permissions.recipients(mandate.school), "mandate_problem", "A mandate needs attention", f"{mandate.family.display_name}: {what}", _data(mandate))


def cancelled(mandate) -> None:
    _tell(payer_recipients(mandate.payer), "mandate_cancelled", "Direct debit mandate cancelled",
          f"Your direct debit mandate on {mandate.account_mask} was cancelled.", _data(mandate))
    _tell(permissions.recipients(mandate.school), "mandate_cancelled", "A mandate was cancelled",
          f"{mandate.family.display_name}: the mandate on {mandate.account_mask} was cancelled.", _data(mandate))


def debit_succeeded(instruction, amount_minor: int) -> None:
    mandate = instruction.mandate
    _tell(payer_recipients(mandate.payer), "debit_succeeded", "School fees debited",
          f"{_money(amount_minor)} was debited from your account ({mandate.account_mask}) for {instruction.family.display_name}'s school fees.",
          {**_data(mandate), "instructionId": str(instruction.id)})


def debit_failed(instruction, why: str) -> None:
    mandate = instruction.mandate
    _tell(payer_recipients(mandate.payer), "debit_failed", "A school fee debit did not go through",
          f"A debit on your account ({mandate.account_mask}) for {instruction.family.display_name}'s school fees did not go through: {why}", {**_data(mandate), "instructionId": str(instruction.id)})
    _tell(permissions.recipients(instruction.school), "debit_failed", "A direct debit failed",
          f"{instruction.family.display_name}: {why}", {**_data(mandate), "instructionId": str(instruction.id)})


def batch_waiting(batch) -> None:
    _tell([m for m in permissions.recipients(batch.school) if permissions.can_approve(m) and m.id != batch.submitted_by_id], "debit_batch_waiting",
          "A direct-debit batch is waiting for approval", f"{batch.title or 'A batch'}: {batch.total_items} debits, {_money(batch.total_amount_minor)}.",
          {"batchId": str(batch.id)})


def batch_rejected(batch) -> None:
    _tell([m for m in permissions.recipients(batch.school) if m.id in (batch.prepared_by_id, batch.submitted_by_id)], "debit_batch_rejected",
          "A direct-debit batch was rejected", f"{batch.title or 'A batch'}: {batch.rejection_reason}", {"batchId": str(batch.id)})

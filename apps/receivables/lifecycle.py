"""What has to follow a change to what a family owes.

Every ledger change - new charges, an adjustment, a payment, credit used or taken back - can change
whether the family owes anything. This is where the consequences are drawn together, so no caller has
to remember them. After a change, in the same transaction and holding the family's row:

1. any credit the family holds is used against what it owes (credit and debt never sit side by side);
2. every live charge's status is brought into line with the ledger;
3. the family's collection account is made active (it owes) or dormant (it does not);
4. the people who should know are told.
"""

from dataclasses import dataclass

from django.db import transaction

from . import collection_accounts, credit, ledger, notifications


@dataclass
class PaymentEvent:
    """A bank payment that has just been allocated, for the messages that follow it."""

    transaction: object
    result: object
    before: ledger.FamilyPosition


@transaction.atomic
def settle_family(family, actor=None, *, payment: PaymentEvent | None = None, today=None, announce_reactivation: bool = True) -> ledger.FamilyPosition:
    family = credit.lock_family(family)
    credit.apply_available(family, actor=actor, today=today)
    ledger.refresh_statuses(ledger.live_receivables(family))
    after = ledger.family_position(family, today=today)
    changes = collection_accounts.sync_state(family, after, actor=actor)
    notifications.after_settlement(family, after, payment=payment, account_changes=changes if announce_reactivation else [])
    return after


def after_new_charges(schedule, report, actor, announce: bool = True) -> None:
    """New charges were raised for the families in `report`: settle each (credit is used, accounts wake up)
    and, on first publication, tell the school and the families."""
    from apps.receivables.models import Family

    families = list(Family.objects.filter(id__in=report.families).order_by("id"))
    for family in families:
        settle_family(family, actor)
    if announce:
        notifications.schedule_published(schedule, report, families)

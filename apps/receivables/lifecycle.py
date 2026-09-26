"""What has to follow a change to what a family owes.

Every ledger change - new charges, an adjustment, a payment, a credit applied - can change whether the
family owes anything. This module is where the consequences are drawn together, so no caller has to
remember them: available credit is used, the family's collection account is made active or dormant,
and the right people are told. It is filled in step by step alongside the ledger.
"""


def after_new_charges(schedule, report, actor, announce: bool = True) -> None:
    """New charges were raised for the families in `report`."""

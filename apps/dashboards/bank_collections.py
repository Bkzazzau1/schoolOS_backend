"""The money the school has actually received, for the owner and finance dashboards.

This is the bank-connection layer's own summary, unchanged, so the dashboard and the Collections
screen can never disagree. Until an account is connected it says so (`available: false`) instead of
showing zeros as if they were facts.
"""

from apps.bankconnect import summary as bank_summary


def summary(school) -> dict:
    return bank_summary.build(school, recent=5)


def outstanding_balances_missing(money: dict) -> list[str]:
    """"Outstanding balances" is a figure the school's fee ledger gives once it has charges; before then it is not available."""
    return [] if money.get("outstandingFeesAvailable") else ["outstanding balances"]

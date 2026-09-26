"""Providers that can ISSUE a payment account for a family, and how each one's account looks.

An issuer is a provider adapter for one job: given a family and the school's connection to a provider, obtain
the account that family will pay into. It is deliberately separate from the bank connector that READS the
school's payments, because a provider may do one and not the other, and it is provider-agnostic in what it
returns: an `IssuedAccount` is whatever that provider gave (a number, a provider reference, a name, extra
facts for the payer), never a fixed "ten digits".

No real provider is implemented. A real issuer is written only against that provider's own published
documentation, never guessed, so every listed bank answers "cannot issue yet" and says why. What works today:

* the SANDBOX issuer (only where the sandbox is switched on) makes clearly-labelled test accounts, so the whole
  path - issue, show to the parent, receive a payment, settle the family - can be exercised end to end;
* a person recording, by hand, the account a bank has given a family (`collection_accounts.register`), for any
  provider and any format.

School fees are the SCHOOL's money. An issuer belongs to the school's own bank or collection provider; it is never
SchoolOS's own payment account, and SchoolOS's own income (schools paying for SchoolOS) is a separate matter
entirely (`apps.billing`).
"""

import hashlib
from dataclasses import dataclass, field

from apps.bankconnect.providers import registry as connectors
from apps.bankconnect.providers.base import STATUS_PENDING_DOCS

from .account_shapes import GENERIC, AccountShape
from .errors import Refused


@dataclass(frozen=True)
class IssuedAccount:
    account_number: str = ""
    external_account_ref: str = ""
    account_name: str = ""
    bank_name: str = ""
    #: Extra facts for the payer, as `[{"label", "value"}]`.
    public_details: list = field(default_factory=list)
    #: Safe-to-keep facts from the provider. Never credentials.
    provider_meta: dict = field(default_factory=dict)


class AccountIssuer:
    """Subclass, set `provider` and `shape`, and implement `issue`."""

    provider: str = ""
    shape: AccountShape = GENERIC
    status: str = "implemented"

    def issue(self, *, family, connection, attempt: int = 0) -> IssuedAccount:
        raise NotImplementedError

    @property
    def can_issue(self) -> bool:
        return True


class PendingIssuer(AccountIssuer):
    """A listed provider that cannot issue family accounts yet: SchoolOS has not been given its verified documentation."""

    status = STATUS_PENDING_DOCS

    def __init__(self, provider: str, name: str):
        self.provider, self.name = provider, name

    @property
    def can_issue(self) -> bool:
        return False

    def issue(self, *, family, connection, attempt: int = 0) -> IssuedAccount:
        raise Refused(
            f"{self.name} cannot issue accounts for families yet: SchoolOS has not been given its verified documentation. "
            "You can record the account the bank gave a family by hand instead.",
            "issuer_unavailable",
        )


class SandboxIssuer(AccountIssuer):
    """Test accounts for the sandbox connection: deterministic, ten digits, and labelled as not real."""

    provider = "sandbox"
    status = "sandbox"
    shape = AccountShape(
        number_label="Test account number", number_example="9123456789",
        payer_note="This is a test account. No real money can be paid into it.",
    )

    def issue(self, *, family, connection, attempt: int = 0) -> IssuedAccount:
        digest = hashlib.sha256(f"sandbox:{family.id}:{attempt}".encode()).hexdigest()
        number = "9" + str(int(digest, 16) % 10**9).zfill(9)
        return IssuedAccount(
            account_number=number, external_account_ref=f"sbx-{digest[:16]}", account_name=f"TEST {family.display_name}"[:200],
            bank_name="SchoolOS Test Bank",
            public_details=[{"label": "Test only", "value": "Not a real bank account"}], provider_meta={"test": True},
        )


_SANDBOX = SandboxIssuer()


def issuer_for(provider: str) -> AccountIssuer | None:
    """The issuer for a provider code, or None for a provider SchoolOS does not know at all."""
    if provider == _SANDBOX.provider:
        return _SANDBOX if connectors.sandbox_enabled() else None
    info = next((p for p in connectors.all_providers() if p.code == provider), None)
    return PendingIssuer(info.code, info.display_name) if info is not None else None


def shape_for(provider: str) -> AccountShape:
    """How a provider's account looks. A provider (or a hand-typed one) with no shape of its own gets the broad default."""
    issuer = issuer_for(provider)
    return issuer.shape if issuer is not None else GENERIC


def describe() -> list[dict]:
    """Every provider a school may hold family accounts with, with its shape and whether SchoolOS can issue for it."""
    out = []
    for info in connectors.all_providers():
        issuer = issuer_for(info.code)
        out.append({
            "code": info.code, "displayName": info.display_name, "canIssue": issuer.can_issue, "issuerStatus": issuer.status,
            "shape": issuer.shape.as_dict(),
        })
    return out

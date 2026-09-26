"""The one interface every bank or collection-provider connector implements, and the one shape
their transactions are translated into.

A connector only implements what its provider really offers. What it offers is declared in its
`Capabilities`; the rest of SchoolOS reads the flags and never assumes a provider can do something.
Calling something a connector does not support raises `NotSupported`.

`secret` is what the vault opened, plus a `connection_id` the service adds at call time (never
stored) for connectors that need to know which connection they are serving.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from ..constants import ConnectionType


class ConnectorError(Exception):
    """A provider call failed. `code` is a short, stable word the app can show; the message is
    written by SchoolOS, never copied from the provider, because those can echo what was sent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotSupported(ConnectorError):
    def __init__(self, what: str):
        super().__init__("not_supported", f"This provider does not support {what}.")


class BadCredentials(ConnectorError):
    def __init__(self):
        super().__init__("bad_credentials", "The provider did not accept these credentials.")


class InvalidSignature(ConnectorError):
    def __init__(self):
        super().__init__("invalid_signature", "The callback's signature is not valid.")


@dataclass(frozen=True)
class Capabilities:
    supports_transaction_sync: bool = False
    supports_webhooks: bool = False
    supports_balance: bool = False
    supports_historical_transactions: bool = False
    supports_realtime_transactions: bool = False
    supports_outbound_payments: bool = False
    supports_account_verification: bool = False
    supports_token_refresh: bool = False

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    #: A secret is never echoed back and is masked while typed.
    secret: bool = True
    required: bool = True


#: How a connector says it is connected.
METHOD_CREDENTIALS = "credentials"
METHOD_AUTHORIZATION = "authorization"

STATUS_IMPLEMENTED = "implemented"
STATUS_SANDBOX = "sandbox"
STATUS_PENDING_DOCS = "pending_verified_documentation"


@dataclass(frozen=True)
class ProviderInfo:
    code: str
    display_name: str
    icon: str
    connection_type: str
    capabilities: Capabilities
    credential_fields: tuple[CredentialField, ...] = ()
    #: The ways a person can connect it: credentials the bank issued, or approving access on the
    #: bank's own page. A provider that authorises never asks for a password.
    connect_methods: tuple[str, ...] = (METHOD_CREDENTIALS,)
    production_status: str = STATUS_PENDING_DOCS
    description: str = ""

    @property
    def is_sandbox(self) -> bool:
        return self.connection_type == ConnectionType.SANDBOX

    @property
    def implemented(self) -> bool:
        return self.production_status in (STATUS_IMPLEMENTED, STATUS_SANDBOX)


@dataclass(frozen=True)
class AccountIdentity:
    """What the provider itself says the account is - the proof the school connected what it meant to."""

    bank_name: str
    account_name: str
    #: Full number, held only in memory long enough to mask and fingerprint it.
    account_number: str
    external_account_id: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectionGrant:
    identity: AccountIdentity
    #: What must be kept to talk to the provider again. Sealed by the vault, never shown.
    secret: dict
    token_expires_at: datetime | None = None


@dataclass(frozen=True)
class AuthorizationStart:
    """Where to send the person to approve access (the bank's own page), never a form for a password."""

    authorization_url: str
    state: str


@dataclass(frozen=True)
class NormalizedTransaction:
    external_transaction_id: str
    direction: str
    amount_minor: int
    transaction_date: datetime
    currency: str = "NGN"
    transaction_reference: str = ""
    provider_session_id: str = ""
    transaction_type: str = ""
    sender_name: str = ""
    sender_account_number: str = ""
    sender_bank: str = ""
    narration: str = ""
    value_date: date | None = None
    balance_after_minor: int | None = None
    raw_provider_reference: str = ""


@dataclass(frozen=True)
class SyncPage:
    transactions: list
    next_cursor: dict
    has_more: bool = False


class BankConnector:
    """Subclass, set `info`, and implement only what `info.capabilities` claims."""

    info: ProviderInfo

    # -- connecting -----------------------------------------------------------------------------

    def begin_authorization(self, *, redirect_uri: str, state: str) -> AuthorizationStart:
        raise NotSupported("authorising access through the provider")

    def connect(self, *, credentials: dict | None = None, authorization_code: str | None = None) -> ConnectionGrant:
        raise NotSupported("connecting")

    def test_connection(self, secret: dict) -> AccountIdentity:
        raise NotSupported("testing a connection")

    def refresh_access_token(self, secret: dict) -> tuple[dict, datetime | None]:
        raise NotSupported("refreshing access")

    def disconnect(self, secret: dict) -> None:
        """Revoke access at the provider where it can be revoked. The default has nothing to revoke."""

    # -- reading --------------------------------------------------------------------------------

    def get_account_details(self, secret: dict) -> AccountIdentity:
        raise NotSupported("account details")

    def get_balance(self, secret: dict) -> int | None:
        raise NotSupported("balance")

    def fetch_transactions(self, secret: dict, cursor: dict, limit: int = 100) -> SyncPage:
        raise NotSupported("transaction sync")

    def fetch_transaction(self, secret: dict, external_transaction_id: str) -> NormalizedTransaction | None:
        raise NotSupported("looking up a single transaction")

    def verify_transaction(self, secret: dict, external_transaction_id: str) -> bool:
        raise NotSupported("verifying a transaction")

    def handle_webhook(self, *, raw_body: bytes, headers: dict, secret: dict) -> list[NormalizedTransaction]:
        """Verify the provider's signature FIRST (raise `InvalidSignature`), then translate."""
        raise NotSupported("webhooks")

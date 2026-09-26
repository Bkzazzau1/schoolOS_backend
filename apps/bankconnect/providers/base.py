"""The one interface every collection-provider connector implements, and the one shape their payments are translated into.

Smart Money Collection works with the SCHOOL'S OWN account at a payment provider (Paystack or Monnify): the school
onboards with the provider directly, receives its own credentials and enters them into SchoolOS. A connector uses those
credentials to create the collection account a family pays into, to verify and read the provider's payment events, and to
retire an account. The provider moves and settles the money; SchoolOS never receives or holds it.

Providers do not behave alike, so a connector states what it really does in its `CollectionCapabilities` and the rest of
SchoolOS reads the flags instead of assuming. Calling something a connector does not support raises `NotSupported`. Everything
provider-specific - endpoints, field names, signatures - stays inside the adapter, and is written only against the provider's
own published documentation, never guessed.

`secret` is what the vault opened, plus a `connection_id` the service adds at call time (never stored) for connectors that
need to know which connection they are serving. A connector never logs, returns or puts a secret in an error message: messages
are written by SchoolOS, never copied from the provider, because those can echo what was sent.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

# -- errors -----------------------------------------------------------------------------------------


class ConnectorError(Exception):
    """A provider call failed. `code` is a short, stable word the app can show; `message` is SchoolOS's own."""

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


class ProviderUnavailable(ConnectorError):
    """The provider could not be reached, or did not answer in time. The OUTCOME IS UNKNOWN: the request may have been
    carried out. Callers retry, and look the operation up first where the provider allows it, never assuming it failed."""

    def __init__(self, message: str = "The provider did not answer. Try again in a moment."):
        super().__init__("provider_unavailable", message)


class ProviderRejected(ConnectorError):
    """The provider answered and refused this request. Retrying the same request will not help."""


class AlreadyExists(ConnectorError):
    """The provider says this operation's reference was already used - so the earlier attempt reached it. The caller should
    look up what exists rather than create it again."""

    def __init__(self):
        super().__init__("already_exists", "The provider already has a record with this reference.")


# -- what a provider is -----------------------------------------------------------------------------

STATUS_IMPLEMENTED = "implemented"
STATUS_SANDBOX = "sandbox"
ENV_LIVE, ENV_TEST = "live", "test"


@dataclass(frozen=True)
class CollectionCapabilities:
    #: The provider can give a family its own collection account.
    supports_family_collection_accounts: bool = False
    #: An account that is reused across collections.
    supports_static_accounts: bool = False
    #: An account made for one collection and its amount.
    supports_dynamic_accounts: bool = False
    supports_account_deactivation: bool = False
    supports_account_reactivation: bool = False
    supports_account_closure: bool = False
    supports_webhooks: bool = False
    #: A payment can be looked up at the provider by its reference, to verify an event or recover after a timeout.
    supports_transaction_requery: bool = False
    #: The provider needs identity details of the person an account is made for (for example a BVN or NIN).
    requires_customer_kyc: bool = False

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    #: A secret is never echoed back and is masked while typed.
    secret: bool = True
    required: bool = True
    help: str = ""


@dataclass(frozen=True)
class SettingField:
    """A non-secret choice the school makes for a provider, such as which bank a Paystack account is issued from."""

    name: str
    label: str
    choices: tuple = ()
    default: str = ""
    required: bool = False


@dataclass(frozen=True)
class WebhookSetup:
    #: "dashboard": the school pastes SchoolOS's address into the provider's dashboard (no API sets it). "api": SchoolOS sets it.
    mode: str = "dashboard"
    #: Where in the provider's dashboard it goes.
    where: str = ""
    #: "hmac_sha512": the provider signs the body and SchoolOS verifies it. "requery": the provider does not sign, so
    #: SchoolOS confirms every event by asking the provider directly.
    verification: str = "hmac_sha512"
    events: tuple = ()
    note: str = ""


@dataclass(frozen=True)
class ProviderInfo:
    code: str
    display_name: str
    icon: str
    environments: tuple
    credential_fields: tuple
    capabilities: CollectionCapabilities
    setting_fields: tuple = ()
    #: One line telling the school what to do at the provider before entering credentials.
    onboarding: str = ""
    webhook: WebhookSetup = WebhookSetup()
    production_status: str = STATUS_IMPLEMENTED
    description: str = ""
    #: How a family sees this provider's account: what it calls the number, and how to pay it.
    account_label: str = "Account number"
    payer_note: str = ""
    connection_type: str = "collection_provider"
    #: What the provider needs to know about the family's payer before it will make an account, from: "name", "email", "phone",
    #: "identity" (a BVN or NIN). Smart Money Collection shows a family that lacks one of them as "details missing" and never sends a
    #: request the provider would refuse.
    customer_requirements: tuple = ("email",)

    @property
    def is_sandbox(self) -> bool:
        return self.production_status == STATUS_SANDBOX

    @property
    def implemented(self) -> bool:
        return self.production_status in (STATUS_IMPLEMENTED, STATUS_SANDBOX)


@dataclass(frozen=True)
class MerchantProfile:
    """What the provider says the connected merchant is: the proof the school connected what it meant to."""

    display_name: str = ""
    #: A masked public identifier (never a secret).
    reference: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ValidatedConnection:
    merchant: MerchantProfile
    #: What must be kept to talk to the provider again. Sealed by the vault, never shown.
    secret: dict
    settings: dict = field(default_factory=dict)
    token_expires_at: datetime | None = None
    #: Safe facts learned while verifying (never credentials).
    meta: dict = field(default_factory=dict)


# -- provisioning -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerDetails:
    """Who a family collection account is made for: the family's payer. Providers need a name, an email and a phone;
    some need identity details as well."""

    name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    bvn: str = ""
    nin: str = ""


@dataclass(frozen=True)
class ProvisionRequest:
    #: The same value on every retry of one account: what makes generation idempotent.
    idempotency_key: str
    #: A reference SchoolOS chooses and the provider stores against the account (where it accepts one).
    account_reference: str
    family_code: str
    family_name: str
    customer: CustomerDetails
    account_name: str
    #: "static" or "dynamic".
    mode: str = "static"
    currency: str = "NGN"
    #: The collection target, in minor units. Only a dynamic account is made for an amount.
    amount_minor: int | None = None
    valid_until: date | None = None
    description: str = ""
    #: Where a provider call that takes several steps has got to (for example the customer it already made). It belongs to
    #: the caller's job and is saved even when a call fails, so a retry carries on instead of starting over.
    checkpoint: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProvisionedAccount:
    #: The account number a payer transfers to.
    account_number: str = ""
    #: The provider's own id for the account, kept for life so it can be looked up, deactivated or closed.
    provider_account_ref: str = ""
    #: What the provider puts on the payment event to say which account was paid (when it differs from the number).
    lookup_ref: str = ""
    account_name: str = ""
    bank_name: str = ""
    number_label: str = "Account number"
    public_details: list = field(default_factory=list)
    provider_meta: dict = field(default_factory=dict)
    #: False while the provider is still setting the account up (it finishes by event or by lookup).
    ready: bool = True


@dataclass(frozen=True)
class ProviderAccountState:
    #: "active", "inactive" (deactivated), "closed", "pending" or "unknown".
    status: str
    account_number: str = ""
    detail: str = ""


@dataclass(frozen=True)
class VerifiedPayment:
    """What the provider itself says about one payment, from asking it directly."""

    found: bool
    paid: bool = False
    amount_minor: int = 0
    currency: str = "NGN"
    reference: str = ""
    #: What identifies the account or reference the money was paid into.
    receiving_reference: str = ""
    provider_status: str = ""


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
    #: The account the money was paid INTO, as the provider reports it (for a family collection account this is what
    #: identifies the family with certainty, far more reliably than anything guessed from a narration).
    receiving_account_reference: str = ""


@dataclass(frozen=True)
class AccountEvent:
    """A provider telling us an account it was asked to make is ready (`ready`) or could not be made (`failed`)."""

    kind: str
    reference: str = ""
    account_number: str = ""


@dataclass(frozen=True)
class WebhookOutcome:
    transactions: list = field(default_factory=list)
    account_events: list = field(default_factory=list)
    #: Something the provider sent that means nothing to SchoolOS (another event type): acknowledged, not processed.
    ignored: bool = False


class CollectionConnector:
    """Subclass, set `info`, and implement only what `info.capabilities` claims."""

    info: ProviderInfo

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment: str, credentials: dict, settings: dict) -> ValidatedConnection:
        """Verify what the school entered with the provider and say who the merchant is. Raises `BadCredentials` for a
        refused credential. Nothing is stored here."""
        raise NotSupported("verifying credentials")

    def get_merchant_profile(self, secret: dict, *, environment: str, settings: dict) -> MerchantProfile:
        raise NotSupported("reading the merchant profile")

    # -- family collection accounts -------------------------------------------------------------

    def provision_family_collection_account(self, secret: dict, request: ProvisionRequest, *, environment: str, settings: dict) -> ProvisionedAccount:
        raise NotSupported("collection accounts for families")

    def get_collection_account(self, secret: dict, *, account_ref: str, environment: str, settings: dict) -> ProviderAccountState:
        raise NotSupported("looking up a collection account")

    def deactivate_collection_account(self, secret: dict, *, account_ref: str, environment: str, settings: dict) -> None:
        raise NotSupported("deactivating a collection account")

    def reactivate_collection_account(self, secret: dict, *, account_ref: str, environment: str, settings: dict) -> None:
        raise NotSupported("reactivating a collection account")

    def close_collection_account(self, secret: dict, *, account_ref: str, environment: str, settings: dict) -> None:
        raise NotSupported("closing a collection account")

    # -- payments -------------------------------------------------------------------------------

    def verify_transaction(self, secret: dict, *, reference: str, environment: str, settings: dict) -> VerifiedPayment:
        raise NotSupported("looking up a payment")

    def requery_transaction(self, secret: dict, *, reference: str, environment: str, settings: dict) -> VerifiedPayment:
        """Ask the provider about a payment again (after a timeout, or to confirm an unsigned event)."""
        return self.verify_transaction(secret, reference=reference, environment=environment, settings=settings)

    def handle_webhook(self, *, raw_body: bytes, headers: dict, secret: dict, environment: str, settings: dict) -> WebhookOutcome:
        """Verify the provider's authenticity check FIRST (raise `InvalidSignature`), then translate. Never trust the body
        before this has passed."""
        raise NotSupported("webhooks")

    def disconnect(self, secret: dict) -> None:
        """Nothing to revoke at the provider by default: the school revokes its own keys in the provider's dashboard."""

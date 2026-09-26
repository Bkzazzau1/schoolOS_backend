"""The one interface every direct-debit provider implements, and the shapes their answers are translated into.

Mandates & Direct Debit works with the SCHOOL'S OWN relationship with a direct-debit provider (Remita, Lendsqr): the school enters the credentials
the provider issued to it, a payer authorises a mandate on their own bank account, and the school may then send debit instructions on it. The
provider executes the debit and moves the money; SchoolOS never holds it.

Providers do not behave alike (how a payer activates, how long it takes before a debit works, whether a debit can be started through the API at
all), so a connector states what it really does in `MandateCapabilities` and the rest of SchoolOS reads the flags instead of assuming. Calling
something a connector does not offer raises `NotSupported`; something the provider may offer but whose documentation SchoolOS has not been able
to verify raises `PendingDocumentation`, which fails safely and is never guessed around. Everything provider-specific - endpoints, hashes, field
names, status codes - stays inside the adapter and is written only against the provider's own published documentation.

`secret` is what the vault opened, plus a `connection_id` the service adds at call time (never stored). A connector never logs, returns or puts a
secret, a bank account number or a raw provider message in an error: messages are written by SchoolOS.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

# The provider-neutral pieces are shared with Smart Money Collection's connectors: the errors, the transport seam and the form-field shapes.
from apps.bankconnect.providers.base import (  # noqa: F401 - re-exported
    ENV_LIVE,
    ENV_TEST,
    STATUS_IMPLEMENTED,
    STATUS_SANDBOX,
    AlreadyExists,
    BadCredentials,
    ConnectorError,
    CredentialField,
    InvalidSignature,
    MerchantProfile,
    NotSupported,
    ProviderRejected,
    ProviderUnavailable,
    SettingField,
    ValidatedConnection,
    WebhookSetup,
)

from ..constants import DebitOutcome, MandateStatus


class PendingDocumentation(NotSupported):
    """The provider may offer this, but SchoolOS has not been able to verify it in the provider's published documentation. It is refused
    rather than guessed."""

    def __init__(self, what: str, provider: str = "This provider"):
        ConnectorError.__init__(
            self, "pending_documentation",
            f"{provider}'s published documentation does not describe {what} in enough detail for SchoolOS to offer it yet.",
        )


@dataclass(frozen=True)
class MandateCapabilities:
    """What a provider really does. The app only offers what is switched on."""

    supports_bank_verification: bool = False
    #: The payer can authorise inside SchoolOS (a bank OTP entered in the app) rather than only at the provider.
    supports_provider_hosted_consent: bool = False
    supports_remote_cancellation: bool = False
    supports_suspension: bool = False
    supports_reactivation: bool = False
    supports_fixed_amount_mandate: bool = False
    supports_variable_amount_mandate: bool = False
    #: A debit instruction can be sent through the API on demand.
    supports_manual_debit: bool = False
    #: The provider debits on a schedule by itself (SchoolOS Version 1 never asks it to).
    supports_scheduled_debit: bool = False
    supports_webhooks: bool = False
    supports_status_requery: bool = False
    #: A debit's status can be asked for by its reference.
    supports_debit_status: bool = False
    supports_balance_lookup: bool = False
    #: The payer activates by entering a bank OTP in SchoolOS (`request_activation` / `confirm_activation`).
    supports_otp_activation: bool = False
    #: The payer activates by printing and signing a mandate form for their bank.
    supports_form_activation: bool = False
    #: The payer activates by an activation transfer from the account.
    supports_transfer_activation: bool = False
    #: The provider needs the payer's full account number again on every debit (SchoolOS then keeps it sealed for as long as the mandate lives).
    requires_account_number_for_debit: bool = False
    #: The provider needs an id of the payer in ITS own system before a mandate can be made (Lendsqr's customer id).
    requires_provider_customer: bool = False

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class MandateProviderInfo:
    code: str
    display_name: str
    icon: str
    environments: tuple
    credential_fields: tuple
    capabilities: MandateCapabilities
    setting_fields: tuple = ()
    #: One line telling the school what to do at the provider before entering credentials.
    onboarding: str = ""
    webhook: WebhookSetup = WebhookSetup()
    production_status: str = STATUS_IMPLEMENTED
    description: str = ""
    #: What must be true before this provider can be used LIVE (never a claim that a school is eligible).
    live_note: str = ""
    #: What the payer must know about how the mandate is activated.
    activation_note: str = ""
    #: Facts about a payer the provider needs before it will make a mandate: "name", "email", "phone".
    payer_requirements: tuple = ("name", "email", "phone")
    #: The provider's own wording for the mandate's maximum: what it limits.
    maximum_note: str = ""
    #: What the maximum limits: "single_debit" (each debit) or "calendar_month" (the total debited in a month, with a count of debits).
    maximum_scope: str = "single_debit"

    @property
    def implemented(self) -> bool:
        return self.production_status in (STATUS_IMPLEMENTED, STATUS_SANDBOX)

    @property
    def is_sandbox(self) -> bool:
        return self.production_status == STATUS_SANDBOX


@dataclass(frozen=True)
class BankInfo:
    """A bank the provider can make a mandate on."""

    code: str
    name: str
    #: The payer can activate in SchoolOS with an OTP (where the provider says so).
    self_activation: bool = False
    #: What activating costs the payer or how it works at this bank, in the provider's words, when the provider says.
    activation_amount_minor: int | None = None
    activation_note: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PayerDetails:
    """What the provider needs to know about the payer."""

    name: str
    email: str = ""
    phone: str = ""


@dataclass(frozen=True)
class CreateMandateRequest:
    #: The same value on every retry of one mandate: what makes creation idempotent.
    request_ref: str
    payer: PayerDetails
    bank_code: str
    account_number: str
    #: The most that can be debited on the mandate at one time, in minor units.
    maximum_amount_minor: int
    start_date: date
    end_date: date
    max_debits: int | None = None
    description: str = ""
    #: The payer's id in the provider's own system, where the provider needs one.
    provider_customer_ref: str = ""
    #: The provider's own reference for a mandate that was already made (a retry of a call whose answer never arrived).
    existing_ref: str = ""


@dataclass(frozen=True)
class ProviderMandate:
    """What the provider says about a mandate, in SchoolOS's words plus the provider's own."""

    provider_ref: str
    #: One of `PROVIDER_REPORTED`.
    status: str
    provider_status: str = ""
    provider_status_code: str = ""
    #: The mandate code / number the provider quotes when it debits.
    mandate_code: str = ""
    activated_at: datetime | None = None
    #: When debits become possible, if the provider says (or SchoolOS's adapter configuration says) so. Empty until it is known.
    debit_ready_at: datetime | None = None
    #: The last moment the payer can activate, when the provider says so.
    activation_deadline: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool = False
    #: Safe facts the payer or a person needs to act (never a secret, never an account number).
    activation: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ActivationChallenge:
    """The bank asked the payer for something (a one-time password and card digits) before it will activate the mandate."""

    challenge_ref: str
    #: `[{"name", "label", "description"}]`: what the payer is asked to type.
    fields: tuple = ()
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DebitRequest:
    #: The same value on every attempt of one debit: what makes it safe to ask the provider whether it already happened.
    request_ref: str
    mandate_ref: str
    amount_minor: int
    mandate_code: str = ""
    #: For the providers that need them on every debit.
    funding_account: str = ""
    funding_bank_code: str = ""
    narration: str = ""
    currency: str = "NGN"


@dataclass(frozen=True)
class DebitResult:
    """What the provider says about one debit, in SchoolOS's words plus the provider's own. Only `success` ever settles anything."""

    #: One of `DebitOutcome`.
    outcome: str
    provider_status: str = ""
    provider_status_code: str = ""
    provider_reference: str = ""
    amount_minor: int | None = None
    #: A short stable word for why a debit failed (`insufficient_funds`, ...); empty otherwise.
    failure_code: str = ""
    at: datetime | None = None
    meta: dict = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        return self.outcome == DebitOutcome.SUCCESS


@dataclass(frozen=True)
class ProviderEvent:
    """A provider callback, once understood. It carries only what to look up: what the provider then says decides everything."""

    kind: str  # "mandate" | "debit"
    #: The provider's own reference for the mandate.
    mandate_ref: str = ""
    #: SchoolOS's reference for the debit, when the provider quotes it.
    request_ref: str = ""


@dataclass(frozen=True)
class WebhookOutcome:
    events: list = field(default_factory=list)
    #: Something the provider sent that means nothing to SchoolOS: acknowledged, not processed.
    ignored: bool = False


class MandateConnector:
    """Subclass, set `info`, and implement only what `info.capabilities` claims."""

    info: MandateProviderInfo

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment: str, credentials: dict, settings: dict) -> ValidatedConnection:
        """Verify what the school entered with the provider and say who the merchant is. Raises `BadCredentials` for a refused credential.
        Nothing is stored here."""
        raise NotSupported("verifying credentials")

    def get_merchant_profile(self, secret: dict, *, environment: str, settings: dict) -> MerchantProfile:
        raise NotSupported("reading the merchant profile")

    def list_supported_banks(self, secret: dict, *, environment: str, settings: dict) -> list[BankInfo]:
        raise NotSupported("listing banks")

    def verify_bank_account(self, secret: dict, *, environment: str, settings: dict, bank_code: str, account_number: str) -> str:
        """The account holder's name as the bank gives it."""
        raise NotSupported("verifying a bank account")

    # -- a mandate ------------------------------------------------------------------------------

    def create_mandate(self, secret: dict, request: CreateMandateRequest, *, environment: str, settings: dict) -> ProviderMandate:
        raise NotSupported("creating a mandate")

    def get_mandate(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ProviderMandate:
        """The mandate as the provider has it now (what a person is shown)."""
        return self.get_mandate_status(secret, mandate_ref=mandate_ref, environment=environment, settings=settings, **hints)

    def get_mandate_status(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ProviderMandate:
        """Ask the provider where the mandate stands. `hints` carry what an adapter may need to ask (a request reference, a customer id)."""
        raise NotSupported("asking about a mandate")

    def request_activation(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ActivationChallenge:
        raise NotSupported("activating a mandate with a bank one-time password")

    def confirm_activation(
        self, secret: dict, *, mandate_ref: str, challenge_ref: str, answers: dict, environment: str, settings: dict, **hints
    ) -> ProviderMandate:
        raise NotSupported("activating a mandate with a bank one-time password")

    def cancel_mandate(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ProviderMandate:
        raise NotSupported("cancelling a mandate")

    def suspend_mandate(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ProviderMandate:
        raise NotSupported("suspending a mandate")

    def reactivate_mandate(self, secret: dict, *, mandate_ref: str, environment: str, settings: dict, **hints) -> ProviderMandate:
        raise NotSupported("reactivating a mandate")

    # -- a debit --------------------------------------------------------------------------------

    def create_debit(self, secret: dict, request: DebitRequest, *, environment: str, settings: dict) -> DebitResult:
        """Send one debit instruction. `ProviderUnavailable` means the OUTCOME IS UNKNOWN: the caller asks (`get_debit_status`) and never
        simply repeats."""
        raise NotSupported("debit instructions")

    def get_debit_status(self, secret: dict, *, mandate_ref: str, request_ref: str, environment: str, settings: dict, **hints) -> DebitResult:
        raise NotSupported("asking about a debit")

    # -- callbacks ------------------------------------------------------------------------------

    def handle_webhook(self, *, raw_body: bytes, headers: dict, secret: dict, environment: str, settings: dict) -> WebhookOutcome:
        """Read one callback. Verify its authenticity FIRST, in the provider's documented way; where the provider signs nothing, return only
        what to look up (the caller asks the provider, and its answer - not the body - decides). Raises `InvalidSignature`."""
        raise NotSupported("callbacks")


def mandate_status_from(value: str) -> str:
    """Guard: an adapter must only ever report a status SchoolOS knows."""
    if value not in MandateStatus.values:
        raise ProviderRejected("provider_unreadable", "The provider's answer could not be understood.")
    return value

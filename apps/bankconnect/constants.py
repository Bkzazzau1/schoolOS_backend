"""Words the bank-connection feature shares with the app. Each list mirrors the app's."""

from django.db import models

#: Smart Money Collection duties. The owner always holds them and can give any of them to a trusted person
#: through a job assignment; being in Finance is never enough by itself, and billing authority
#: (`finance.billing_authority`) is a different responsibility that does NOT open the school's provider secrets.
#:
#: - provider manage: connect, replace, test and disable the school's own Paystack / Monnify credentials,
#:   set up the webhook, choose the active provider and schedule or apply a provider switch;
#: - policy manage: change the school's collection policy (defaults, session/term/family overrides);
#: - prepare: the MAKER of a collection batch (build the preview, select families, resolve overrides, submit);
#: - approve: the CHECKER of a collection batch (approve or reject; never for a batch they prepared).
DUTY_PROVIDER_MANAGE = "finance.collection_provider_manage"
DUTY_POLICY_MANAGE = "finance.collection_policy_manage"
DUTY_PREPARE = "finance.collection_prepare"
DUTY_APPROVE = "finance.collection_approve"
SMART_COLLECTION_DUTIES = (DUTY_PROVIDER_MANAGE, DUTY_POLICY_MANAGE, DUTY_PREPARE, DUTY_APPROVE)
#: The earlier duty for connecting the school's bank accounts. Kept so an assignment already made keeps working:
#: it is honoured as provider-management authority and nothing more.
DUTY_MANAGE_CONNECTIONS = "finance.bank_connections"

#: The only providers Smart Money Collection offers. The SCHOOL onboards with the provider directly (KYC),
#: receives its own credentials and enters them into SchoolOS; the provider moves and settles the money.
PROVIDER_PAYSTACK = "paystack"
PROVIDER_MONNIFY = "monnify"
SMART_PROVIDERS = (PROVIDER_PAYSTACK, PROVIDER_MONNIFY)
#: Codes that once appeared in Smart Money Collection and no longer do. Remita is reserved for a separate Mandates / Direct Debit
#: domain, so it is never offered, connected, made active, batched or switched to. A row that still carries one of these codes is
#: history: it is kept so the payments and accounts recorded under it still read correctly, and nothing new is ever made under it.
RETIRED_PROVIDERS = ("remita",)
#: Development and tests only; never offered to a school in production.
PROVIDER_SANDBOX = "sandbox"
#: What a school's collection provider connection may be: a supported provider, or (in development) the sandbox.
COLLECTION_PROVIDER_CODES = SMART_PROVIDERS + (PROVIDER_SANDBOX,)


class Environment(models.TextChoices):
    LIVE = "live", "Live"
    TEST = "test", "Test"


class WebhookStatus(models.TextChoices):
    #: No callback address has been issued yet.
    NOT_CONFIGURED = "not_configured", "Not set up"
    #: The address exists, but no signed or verified event has reached it, so it is not known to work.
    AWAITING_EVENT = "awaiting_event", "Waiting for the first event"
    #: A verified event has arrived. Only then is the webhook called active.
    ACTIVE = "active", "Active"


class ConnectionType(models.TextChoices):
    #: The school holds corporate API credentials issued by its own bank.
    DIRECT_BANK_API = "direct_bank_api", "Direct bank API"
    #: The school authorises SchoolOS through a token; no internet-banking password is ever held.
    OPEN_BANKING = "open_banking", "Open banking"
    #: A payment provider (Monnify, Paystack...) that reports collections it processed. This is
    #: *not* access to the school's underlying bank account and is never treated as such.
    COLLECTION_PROVIDER = "collection_provider", "Collection provider"
    #: Synthetic data for testing. Never counted as the school's money.
    SANDBOX = "sandbox", "Sandbox"


class ConnectionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONNECTED = "connected", "Connected"
    DISABLED = "disabled", "Disabled"
    NEEDS_REAUTH = "needs_reauth", "Needs re-authorisation"
    ERROR = "error", "Error"
    REVOKED = "revoked", "Disconnected"


class Purpose(models.TextChoices):
    TUITION = "tuition", "Tuition"
    TRANSPORT = "transport", "Transport"
    BOOKS = "books", "Books"
    UNIFORMS = "uniforms", "Uniforms"
    CAPITAL = "capital", "Capital projects"
    GENERAL = "general", "General collections"
    OTHER = "other", "Other"


class Direction(models.TextChoices):
    CREDIT = "credit", "Credit"
    DEBIT = "debit", "Debit"


class ReconStatus(models.TextChoices):
    MATCHED = "matched", "Matched"
    PARTIALLY_MATCHED = "partially_matched", "Partially matched"
    POSSIBLE_MATCH = "possible_match", "Possible match"
    UNMATCHED = "unmatched", "Unmatched"
    DUPLICATE = "duplicate", "Duplicate"
    REVERSED = "reversed", "Reversed"
    REFUNDED = "refunded", "Refunded"
    REQUIRES_REVIEW = "requires_review", "Requires review"
    #: Decided by a person: real money that belongs to no student.
    UNRELATED_INCOME = "unrelated_income", "Unrelated income"
    #: Set aside by a person for someone to look into.
    INVESTIGATING = "investigating", "Under investigation"
    #: Money going out of the account. It is kept, but there is nothing to match it to.
    NOT_APPLICABLE = "not_applicable", "Not applicable"


#: A person still has something to do with these.
NEEDS_A_PERSON = (
    ReconStatus.POSSIBLE_MATCH,
    ReconStatus.REQUIRES_REVIEW,
    ReconStatus.UNMATCHED,
    ReconStatus.DUPLICATE,
    ReconStatus.PARTIALLY_MATCHED,
    ReconStatus.INVESTIGATING,
)

#: Money counts as reconciled once it is matched to a student, or a person has said what it is.
RECONCILED = (ReconStatus.MATCHED, ReconStatus.UNRELATED_INCOME)

DEFAULT_CURRENCY = "NGN"

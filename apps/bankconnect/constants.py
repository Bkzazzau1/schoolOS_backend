"""Words the bank-connection feature shares with the app. Each list mirrors the app's."""

from django.db import models

#: A duty the owner can give a finance officer (through a job assignment) so they may connect,
#: rotate and disconnect the school's bank accounts. Being in Finance is not enough by itself.
DUTY_MANAGE_CONNECTIONS = "finance.bank_connections"


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

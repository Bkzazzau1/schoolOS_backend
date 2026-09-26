"""The words Mandates & Direct Debit uses. Each list mirrors the app's.

Mandates & Direct Debit is a payment path of its own. It shares the receivables ledger with Smart Money Collection (what a family owes is
decided there and only there) and nothing else: not the provider connections, not the batches, not the jobs.
"""

from django.db import models

# -- who may (owner-delegated duties; being in Finance is never enough) -----------------------------------
#: - provider manage: connect, replace, test and disable the school's own Remita / Lendsqr credentials and set up their callbacks;
#: - manage: start a mandate for a payer, watch its status, resend activation instructions, suspend or cancel it, choose the primary one;
#: - prepare: the MAKER of a direct-debit batch (choose the mandates, review the amounts, submit);
#: - approve: the CHECKER of a direct-debit batch (approve or reject; never one they prepared or changed).
DUTY_PROVIDER_MANAGE = "finance.mandate_provider_manage"
DUTY_MANAGE = "finance.mandate_manage"
DUTY_PREPARE = "finance.mandate_prepare"
DUTY_APPROVE = "finance.mandate_approve"
MANDATE_DUTIES = (DUTY_PROVIDER_MANAGE, DUTY_MANAGE, DUTY_PREPARE, DUTY_APPROVE)

# -- providers ------------------------------------------------------------------------------------------
#: The only providers Mandates & Direct Debit works with. Neither is a Smart Money Collection provider, and a school may connect both at
#: once: there is no "active" mandate provider, because each mandate records the connection it was made under.
PROVIDER_REMITA = "remita"
PROVIDER_LENDSQR = "lendsqr"
MANDATE_PROVIDERS = (PROVIDER_REMITA, PROVIDER_LENDSQR)
#: Development and tests only; never offered to a school in production.
PROVIDER_SANDBOX = "sandbox"
MANDATE_PROVIDER_CODES = MANDATE_PROVIDERS + (PROVIDER_SANDBOX,)

DEFAULT_CURRENCY = "NGN"


class Environment(models.TextChoices):
    LIVE = "live", "Live"
    TEST = "test", "Test"


class ConnectionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONNECTED = "connected", "Connected"
    DISABLED = "disabled", "Disabled"
    NEEDS_REAUTH = "needs_reauth", "Needs re-authorisation"
    ERROR = "error", "Error"
    REVOKED = "revoked", "Disconnected"


class WebhookStatus(models.TextChoices):
    NOT_CONFIGURED = "not_configured", "Not set up"
    AWAITING_EVENT = "awaiting_event", "Waiting for the first event"
    ACTIVE = "active", "Active"


#: A connection still in use (its credential may be asked to work).
LIVE_CONNECTION = (ConnectionStatus.CONNECTED, ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR)
NOT_CLOSED_CONNECTION = LIVE_CONNECTION + (ConnectionStatus.DISABLED,)


# -- a mandate ------------------------------------------------------------------------------------------


class MandateStatus(models.TextChoices):
    #: The record exists (the payer's bank details are sealed); the provider has not been asked for anything yet.
    DRAFT = "draft", "Draft"
    #: Waiting for the PAYER to review and authorise it themselves. Staff cannot do this for them.
    PENDING_CONSENT = "pending_consent", "Waiting for the payer's authorisation"
    #: The provider has the mandate; the payer still has to activate it (bank OTP, a signed form, or an activation transfer).
    PENDING_ACTIVATION = "pending_activation", "Waiting for the payer to activate it"
    #: The payer has done their part and the provider is confirming it.
    ACTIVATING = "activating", "Being activated"
    #: The provider says it is activated but debits are not possible yet (for example the bank's own setup, which takes time).
    PENDING_PROVIDER_SETUP = "pending_provider_setup", "Waiting for the provider to finish setting it up"
    #: Debit-ready: the provider will accept a debit instruction on it.
    ACTIVE = "active", "Active"
    #: Stopped for now (by the school or the provider). No debit is possible.
    SUSPENDED = "suspended", "Suspended"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"
    FAILED = "failed", "Failed"


#: A mandate that is still part of the payer's arrangements (it can still become, or already is, usable).
LIVE_MANDATE = (
    MandateStatus.DRAFT, MandateStatus.PENDING_CONSENT, MandateStatus.PENDING_ACTIVATION, MandateStatus.ACTIVATING,
    MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVE, MandateStatus.SUSPENDED,
)
ENDED_MANDATE = (MandateStatus.CANCELLED, MandateStatus.EXPIRED, MandateStatus.FAILED)
#: What the provider may report about a mandate, in SchoolOS's words (a subset of `MandateStatus`).
PROVIDER_REPORTED = (
    MandateStatus.PENDING_ACTIVATION, MandateStatus.ACTIVATING, MandateStatus.PENDING_PROVIDER_SETUP, MandateStatus.ACTIVE,
    MandateStatus.SUSPENDED, MandateStatus.CANCELLED, MandateStatus.EXPIRED, MandateStatus.FAILED,
)


class ConsentChannel(models.TextChoices):
    #: The payer, signed in as themselves, reviewed and authorised the mandate in the SchoolOS app.
    PAYER_APP = "payer_app", "Authorised by the payer in the app"
    #: The provider's own authorisation (the bank's OTP, a signed mandate form, an activation transfer from the payer's account) is
    #: the evidence; SchoolOS keeps the provider's reference rather than a copy of it.
    PROVIDER_HOSTED = "provider_hosted", "Authorised with the provider"


class ConsentRoute(models.TextChoices):
    """How the payer is to authorise, chosen when the mandate is started."""

    PAYER_APP = "payer_app", "In the SchoolOS app"
    PROVIDER = "provider", "With the provider"


# -- a debit batch --------------------------------------------------------------------------------------


class BatchStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_APPROVAL = "pending_approval", "Waiting for approval"
    REJECTED = "rejected", "Rejected"
    APPROVED = "approved", "Approved"
    PROCESSING = "processing", "Debiting"
    PARTIALLY_SUCCESSFUL = "partially_successful", "Completed with failures"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


EDITABLE_BATCH = (BatchStatus.DRAFT, BatchStatus.REJECTED)
#: Everything not yet settled for good (an unsettled instruction may still be retried or resolved).
OPEN_BATCH = (
    BatchStatus.DRAFT, BatchStatus.PENDING_APPROVAL, BatchStatus.REJECTED, BatchStatus.APPROVED, BatchStatus.PROCESSING,
    BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED,
)


class InstructionStatus(models.TextChoices):
    PENDING = "pending", "Waiting"
    SKIPPED = "skipped", "Not selected"
    #: The provider is being asked (or was asked and has not answered yet).
    DEBITING = "debiting", "Being debited"
    #: The provider did not answer, so the debit may or may not have happened. It is asked again (never repeated) before anything else.
    UNKNOWN = "unknown", "Outcome not yet known"
    SUCCESS = "success", "Debited"
    FAILED = "failed", "Failed"
    RETRYING = "retrying", "Trying again"
    CANCELLED = "cancelled", "Cancelled"


#: A debit that went through (or may have): these are never run again, and never edited.
FROZEN_INSTRUCTION = (InstructionStatus.SUCCESS, InstructionStatus.DEBITING, InstructionStatus.UNKNOWN)


class Eligibility(models.TextChoices):
    ELIGIBLE = "eligible", "Ready to debit"
    NO_MANDATE = "no_mandate", "No mandate"
    NOT_READY = "not_ready", "Mandate not ready"
    NOTHING_DUE = "nothing_due", "Nothing to collect"
    PROVIDER_UNAVAILABLE = "provider_unavailable", "Provider not working"
    PROVIDER_CANNOT_DEBIT = "provider_cannot_debit", "Provider cannot debit yet"


#: What the provider reports about a debit, in SchoolOS's words. Only SUCCESS ever settles a family's charges.
class DebitOutcome(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCESS = "success", "Successful"
    FAILED = "failed", "Failed"
    REVERSED = "reversed", "Reversed"
    REFUNDED = "refunded", "Refunded"
    UNKNOWN = "unknown", "Unknown"
    #: The provider confirms it has no record of this request: it never happened, so it can be tried again.
    NOT_FOUND = "not_found", "Not found"


class JobKind(models.TextChoices):
    DEBIT = "debit", "Send a debit instruction"
    REQUERY = "requery", "Ask the provider what happened to a debit"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY = "retry", "Waiting to retry"
    SUCCEEDED = "succeeded", "Done"
    FAILED = "failed", "Failed"


#: A provider that does not answer is asked again this many times before the instruction is left for a person.
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = (30, 120, 600, 1800, 3600)
LEASE_SECONDS = 180

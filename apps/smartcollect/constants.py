"""The words Smart Money Collection shares with the app. Each list mirrors the app's."""

from django.db import models

from apps.receivables.models import AccountMode  # noqa: F401 - static / dynamic, one definition for the account and the policy


class ReuseScope(models.TextChoices):
    """How long a STATIC account is reused before the school makes another."""

    ONE_TERM = "one_term", "One term"
    SELECTED_TERMS = "selected_terms", "A number of terms"
    ONE_SESSION = "one_session", "One session"
    MULTIPLE_SESSIONS = "multiple_sessions", "A number of sessions"
    UNTIL_DATE = "until_date", "Until a date"
    INDEFINITELY = "indefinitely", "Indefinitely"
    UNTIL_REPLACED = "until_replaced", "Until replaced"


class SettlementAction(models.TextChoices):
    """What happens to a family's account once the family has paid everything it owes."""

    CLOSE_IMMEDIATELY = "close_immediately", "Close it straight away"
    DORMANT_IMMEDIATELY = "dormant_immediately", "Make it dormant straight away"
    GRACE_THEN_DORMANT = "grace_then_dormant", "Wait, then make it dormant"
    GRACE_THEN_CLOSE = "grace_then_close", "Wait, then close it"
    MANUAL = "manual", "Leave it for a person"
    PROVIDER_NATIVE = "provider_native", "Let the provider decide"


GRACE_ACTIONS = (SettlementAction.GRACE_THEN_DORMANT, SettlementAction.GRACE_THEN_CLOSE)


class ArrearsPolicy(models.TextChoices):
    """What a family's collection target contains when it still owes for earlier terms."""

    CARRY_FORWARD = "carry_forward", "Carry the previous balance forward"
    CURRENT_TERM_ONLY = "current_term_only", "Current term only"
    CUSTOM_SELECTION = "custom_selection", "Choose which balances to include"


class EligibilityPolicy(models.TextChoices):
    """Whether a family that still owes for an earlier term is given a dynamic account for the new one."""

    EXCLUDE = "exclude", "Do not include them"
    NEEDS_OVERRIDE = "needs_override", "Include them only with an override"
    INCLUDE = "include", "Include them"
    MANUAL_APPROVAL = "manual_approval", "Include them after a person approves each"


class SwitchPolicy(models.TextChoices):
    """What happens to the accounts already made when the school changes its active provider."""

    RETIRE_WHEN_SETTLED = "retire_when_settled", "Keep each until its family has paid"
    RETIRE_AT_SWITCH = "retire_at_switch", "Retire them all when the switch is applied"
    MANUAL = "manual", "Leave them for a person"


class ReasonPolicy(models.TextChoices):
    OPTIONAL = "optional", "A reason is optional"
    REQUIRED_ALWAYS = "required_always", "A reason is always required"
    REQUIRED_SENSITIVE = "required_sensitive", "A reason is required for sensitive overrides"


class OverrideScope(models.TextChoices):
    SESSION = "session", "Session"
    TERM = "term", "Term"
    BATCH = "batch", "Batch"
    FAMILY = "family", "Family"


#: Nearest wins: a family beats its batch, which beats its term, which beats its session, which beats the school's default.
SCOPE_ORDER = (OverrideScope.SESSION, OverrideScope.TERM, OverrideScope.BATCH, OverrideScope.FAMILY)


class ExpiryKind(models.TextChoices):
    ONE_TIME = "one_time", "Once"
    END_OF_TERM = "end_of_term", "Until the end of the term"
    END_OF_SESSION = "end_of_session", "Until the end of the session"
    AT_DATE = "at_date", "Until a date"
    UNTIL_REMOVED = "until_removed", "Until it is removed"


class BatchStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_APPROVAL = "pending_approval", "Waiting for approval"
    REJECTED = "rejected", "Rejected"
    APPROVED = "approved", "Approved"
    PROCESSING = "processing", "Generating"
    PARTIALLY_SUCCESSFUL = "partially_successful", "Generated with errors"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


#: A maker may still change the batch.
EDITABLE_STATUSES = (BatchStatus.DRAFT, BatchStatus.REJECTED)
#: Provider calls are, or may still be, in flight for these.
IN_FLIGHT_STATUSES = (BatchStatus.PROCESSING,)
#: The batch is finished with (though a failed item can still be retried).
SETTLED_STATUSES = (BatchStatus.COMPLETED, BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED)
OPEN_STATUSES = (
    BatchStatus.DRAFT, BatchStatus.PENDING_APPROVAL, BatchStatus.REJECTED, BatchStatus.APPROVED, BatchStatus.PROCESSING,
    BatchStatus.PARTIALLY_SUCCESSFUL, BatchStatus.FAILED,
)


class GenerationStatus(models.TextChoices):
    PENDING = "pending", "Waiting"
    SKIPPED = "skipped", "Not selected"
    GENERATING = "generating", "Being generated"
    SUCCESS = "success", "Generated"
    FAILED = "failed", "Failed"
    RETRYING = "retrying", "Trying again"


class Eligibility(models.TextChoices):
    #: Ready to be given an account.
    ELIGIBLE = "eligible", "Eligible"
    #: Owes for an earlier term and the school's policy says a person must override to include them.
    NEEDS_OVERRIDE = "needs_override", "Needs an override"
    #: Owes for an earlier term and the school's policy says each must be approved by a person.
    MANUAL_APPROVAL = "manual_approval", "Needs approval"
    #: Owes for an earlier term and the school's policy leaves them out.
    EXCLUDED = "excluded", "Excluded by policy"
    #: Already has an account that covers this period.
    HAS_ACCOUNT = "has_account", "Already has an account"
    #: Has an account with another provider that must be retired first.
    PROVIDER_CONFLICT = "provider_conflict", "Account with another provider"
    #: The provider needs details about the payer that the school has not recorded.
    MISSING_DETAILS = "missing_details", "Payer details missing"
    #: The provider cannot make this kind of account (for example a static one, where a provider only makes dynamic accounts).
    UNSUPPORTED_MODE = "unsupported_mode", "Not supported by the provider"
    #: Owes nothing for this period.
    NOTHING_DUE = "nothing_due", "Nothing due"


#: These can be selected, with the extra step each names.
SELECTABLE = (Eligibility.ELIGIBLE, Eligibility.NOTHING_DUE, Eligibility.MANUAL_APPROVAL)
OVERRIDABLE_STATUSES = (Eligibility.NEEDS_OVERRIDE, Eligibility.EXCLUDED)


class SwitchStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    #: The date has come and nothing stands in the way. Nothing has changed: only a person applies it.
    READY_TO_SWITCH = "ready_to_switch", "Ready to switch"
    APPLIED = "applied", "Applied"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


OPEN_SWITCH = (SwitchStatus.SCHEDULED, SwitchStatus.READY_TO_SWITCH)


class JobKind(models.TextChoices):
    PROVISION = "provision", "Make a family's account"
    RETIRE = "retire", "Retire a family's account"


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    RETRY = "retry", "Waiting to retry"
    SUCCEEDED = "succeeded", "Done"
    FAILED = "failed", "Failed"


#: A provider that does not answer is tried again this many times before the item is marked failed for a person to retry.
MAX_ATTEMPTS = 5
#: Seconds to wait before attempt n+1 (attempt 1 is immediate).
BACKOFF_SECONDS = (30, 120, 600, 1800)
#: A job that says it is running but has not finished after this long is taken to have died with its worker.
LEASE_SECONDS = 180

"""A stand-in direct-debit provider that makes the whole mandate and debit path testable without any real provider.

It plays the part of Remita / Lendsqr for development and tests: it verifies a key, makes a mandate (deterministically, so a retry gives the same
one), activates it with a one-time password or on request, cancels, suspends and reactivates it, and executes debit instructions idempotently by
their reference. Its data is synthetic and flagged so, and it is never offered to a school in production (`MANDATES_ENABLE_SANDBOX`).

For tests it can also be told to fail: `inject_fault` makes a matching call raise, once or several times. The important one is the timeout: a debit
that reached the provider and was carried out but whose answer never came back (`timeout_after`), and one that never reached it (`timeout_before`).
"""

import hashlib
from datetime import timedelta

from django.utils import timezone as dj_timezone

from ..constants import DebitOutcome, MandateStatus
from ..models import SandboxDebit, SandboxMandate
from .base import (
    ENV_TEST,
    STATUS_SANDBOX,
    ActivationChallenge,
    BadCredentials,
    BankInfo,
    CreateMandateRequest,
    CredentialField,
    DebitRequest,
    DebitResult,
    MandateCapabilities,
    MandateConnector,
    MandateProviderInfo,
    MerchantProfile,
    ProviderMandate,
    ProviderRejected,
    ProviderUnavailable,
    ValidatedConnection,
    WebhookSetup,
)

CODE = "sandbox"
KEY_PREFIX = "sandbox-"
#: The one-time password the sandbox accepts.
GOOD_OTP = "1234"

_faults: list[dict] = []


def inject_fault(operation: str, kind: str, *, times: int = 1) -> None:
    """The next `times` calls of `operation` ("create_mandate", "get_mandate_status", "create_debit", "get_debit_status", ...) misbehave:
    `kind` is "unavailable" (raises before anything happens), "timeout_before" (the same), or "timeout_after" (the operation is carried out
    and THEN the answer is lost)."""
    _faults.append({"operation": operation, "kind": kind, "times": times})


def clear_faults() -> None:
    _faults.clear()
    _next_debit_statuses.clear()


_next_debit_statuses: list[str] = []


def set_next_debit_status(status: str) -> None:
    """The next debit the sandbox carries out ends as `status` ("success", "failed", "pending", "reversed", "refunded")."""
    _next_debit_statuses.append(status)


def _next_status() -> str:
    return _next_debit_statuses.pop(0) if _next_debit_statuses else "success"


def _take_fault(operation: str) -> str | None:
    for fault in _faults:
        if fault["operation"] == operation and fault["times"] > 0:
            fault["times"] -= 1
            return fault["kind"]
    return None


def _ref(request_ref: str) -> str:
    return "SBM" + hashlib.sha256(request_ref.encode()).hexdigest()[:10].upper()


def _mandate(secret: dict, ref: str) -> SandboxMandate:
    found = SandboxMandate.objects.filter(connection_id=secret["connection_id"], provider_ref=ref).first()
    if found is None:
        raise ProviderRejected("mandate_not_found", "The provider has no such mandate.")
    return found


def _view(row: SandboxMandate) -> ProviderMandate:
    status, ready = row.status, row.debit_ready_at
    if status == MandateStatus.PENDING_PROVIDER_SETUP and ready and ready <= dj_timezone.now():
        status = MandateStatus.ACTIVE
    return ProviderMandate(
        provider_ref=row.provider_ref, status=status, provider_status=f"sandbox:{row.status}", provider_status_code=row.status,
        mandate_code=f"SBM/{row.provider_ref}", is_active=status == MandateStatus.ACTIVE, debit_ready_at=ready,
        activated_at=ready, activation={"method": "sandbox", "otp": "Use the sandbox one-time password."},
        activation_deadline=row.created_at + timedelta(hours=72),
    )


def _outcome_of(row: SandboxDebit) -> DebitResult:
    outcome = {
        "success": DebitOutcome.SUCCESS, "failed": DebitOutcome.FAILED, "pending": DebitOutcome.PENDING,
        "reversed": DebitOutcome.REVERSED, "refunded": DebitOutcome.REFUNDED,
    }.get(row.status, DebitOutcome.UNKNOWN)
    return DebitResult(
        outcome=outcome, provider_status=f"sandbox:{row.status}", provider_status_code=row.status, provider_reference=row.provider_reference,
        amount_minor=row.amount_minor, failure_code="insufficient_funds" if row.status == "failed" else "", at=row.created_at,
    )


class SandboxMandateConnector(MandateConnector):
    info = MandateProviderInfo(
        code=CODE,
        display_name="Test mandate provider",
        icon="science",
        environments=(ENV_TEST,),
        credential_fields=(CredentialField("sandbox_key", "Sandbox key", help="Any key that starts with sandbox-"),),
        capabilities=MandateCapabilities(
            supports_provider_hosted_consent=True, supports_remote_cancellation=True, supports_suspension=True, supports_reactivation=True,
            supports_fixed_amount_mandate=True, supports_variable_amount_mandate=True, supports_manual_debit=True, supports_status_requery=True,
            supports_debit_status=True, supports_otp_activation=True, supports_form_activation=True,
        ),
        onboarding="Development and tests only.",
        webhook=WebhookSetup(mode="dashboard", where="Not needed.", verification="requery"),
        production_status=STATUS_SANDBOX,
        description="A test provider. No real bank, no real money.",
        activation_note="Use the sandbox one-time password.",
        maximum_note="The most that can be debited at one time.",
        payer_requirements=(),
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings) -> ValidatedConnection:
        key = str(credentials.get("sandbox_key") or "")
        if not key.startswith(KEY_PREFIX):
            raise BadCredentials()
        return ValidatedConnection(merchant=MerchantProfile(display_name="Test mandate merchant", reference="sandbox"), secret={"sandbox_key": key})

    def get_merchant_profile(self, secret, *, environment, settings) -> MerchantProfile:
        if not str(secret.get("sandbox_key") or "").startswith(KEY_PREFIX):
            raise BadCredentials()
        return MerchantProfile(display_name="Test mandate merchant", reference="sandbox")

    def list_supported_banks(self, secret, *, environment, settings) -> list[BankInfo]:
        return [BankInfo("058", "Test Bank", self_activation=True), BankInfo("044", "Test Bank Two")]

    # -- a mandate ------------------------------------------------------------------------------

    def create_mandate(self, secret, request: CreateMandateRequest, *, environment, settings) -> ProviderMandate:
        fault = _take_fault("create_mandate")
        if fault in ("unavailable", "timeout_before"):
            raise ProviderUnavailable()
        row, _ = SandboxMandate.objects.get_or_create(
            connection_id=secret["connection_id"], request_ref=request.request_ref,
            defaults={
                "provider_ref": _ref(request.request_ref + secret["connection_id"]), "bank_code": request.bank_code,
                "account_mask": "****" + request.account_number[-4:], "maximum_minor": request.maximum_amount_minor,
            },
        )
        if fault == "timeout_after":
            raise ProviderUnavailable()
        return _view(row)

    def get_mandate_status(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        if _take_fault("get_mandate_status") in ("unavailable", "timeout_before"):
            raise ProviderUnavailable()
        return _view(_mandate(secret, mandate_ref))

    def request_activation(self, secret, *, mandate_ref, environment, settings, **hints) -> ActivationChallenge:
        row = _mandate(secret, mandate_ref)
        row.challenge_ref = "CH" + row.provider_ref[-8:]
        row.save(update_fields=["challenge_ref"])
        return ActivationChallenge(
            challenge_ref=row.challenge_ref,
            fields=({"name": "OTP", "label": "One Time Password", "description": "Enter the one-time password the bank sent you."},),
        )

    def confirm_activation(self, secret, *, mandate_ref, challenge_ref, answers, environment, settings, **hints) -> ProviderMandate:
        row = _mandate(secret, mandate_ref)
        if row.challenge_ref != challenge_ref or str(answers.get("OTP") or "") != GOOD_OTP:
            raise ProviderRejected("activation_refused", "The bank did not accept that one-time password.")
        row.status = MandateStatus.ACTIVE
        row.debit_ready_at = dj_timezone.now()
        row.save(update_fields=["status", "debit_ready_at"])
        return _view(row)

    def cancel_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        row = _mandate(secret, mandate_ref)
        row.status = MandateStatus.CANCELLED
        row.save(update_fields=["status"])
        return _view(row)

    def suspend_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        row = _mandate(secret, mandate_ref)
        row.status = MandateStatus.SUSPENDED
        row.save(update_fields=["status"])
        return _view(row)

    def reactivate_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        row = _mandate(secret, mandate_ref)
        row.status = MandateStatus.ACTIVE
        row.save(update_fields=["status"])
        return _view(row)

    # -- a debit --------------------------------------------------------------------------------

    def create_debit(self, secret, request: DebitRequest, *, environment, settings) -> DebitResult:
        fault = _take_fault("create_debit")
        if fault in ("unavailable", "timeout_before"):
            raise ProviderUnavailable()
        mandate = _mandate(secret, request.mandate_ref)
        existing = SandboxDebit.objects.filter(connection_id=secret["connection_id"], request_ref=request.request_ref).first()
        if existing is not None:
            row = existing
        else:
            if _view(mandate).status != MandateStatus.ACTIVE:
                return DebitResult(outcome=DebitOutcome.FAILED, provider_status="not active", failure_code="mandate_not_activated")
            if request.amount_minor > mandate.maximum_minor:
                return DebitResult(outcome=DebitOutcome.FAILED, provider_status="limit exceeded", failure_code="payment_limit_exceeded")
            row = SandboxDebit.objects.create(
                connection_id=secret["connection_id"], mandate_ref=request.mandate_ref, request_ref=request.request_ref,
                provider_reference="SBD" + hashlib.sha256((secret["connection_id"] + request.request_ref).encode()).hexdigest()[:12].upper(),
                amount_minor=request.amount_minor, status=_next_status(),
            )
        if fault == "timeout_after":
            raise ProviderUnavailable()
        return _outcome_of(row)

    def get_debit_status(self, secret, *, mandate_ref, request_ref, environment, settings, **hints) -> DebitResult:
        if _take_fault("get_debit_status") in ("unavailable", "timeout_before"):
            raise ProviderUnavailable()
        row = SandboxDebit.objects.filter(connection_id=secret["connection_id"], request_ref=request_ref).first()
        if row is None:
            return DebitResult(outcome=DebitOutcome.NOT_FOUND, provider_status="no such request")
        return _outcome_of(row)


"""A stand-in provider that makes the whole Smart Money Collection path testable without any real provider.

It plays the part of Paystack / Monnify for development and tests: it verifies a key, makes a family collection account
(deterministically, so a retry gives the same account), deactivates, reactivates and closes it, signs its own webhooks and can
confirm a payment when asked, so every code path a real connector uses is exercised. Its data is synthetic and flagged so: it is
never counted as the school's money, and it is never offered to a school in production (`BANKCONNECT_ENABLE_SANDBOX`).

For tests it can also be told to fail: `inject_fault` makes a matching provisioning call raise, once or several times, which is how
partial failure, retry and timeout-then-success are exercised.
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone

from django.utils.dateparse import parse_datetime

from .base import (
    ENV_TEST,
    STATUS_SANDBOX,
    AccountEvent,
    BadCredentials,
    CollectionCapabilities,
    CollectionConnector,
    ConnectorError,
    CredentialField,
    InvalidSignature,
    MerchantProfile,
    NormalizedTransaction,
    ProviderAccountState,
    ProviderInfo,
    ProviderRejected,
    ProvisionedAccount,
    ProvisionRequest,
    ValidatedConnection,
    VerifiedPayment,
    WebhookOutcome,
    WebhookSetup,
)

SIGNATURE_HEADER = "x-sandbox-signature"
_faults: list[dict] = []


def inject_fault(error: ConnectorError, *, match: str = "", times: int = 1) -> None:
    """Make the next `times` provisioning calls whose family code or reference contains `match` raise `error`."""
    _faults.append({"match": match, "error": error, "left": times})


def clear_faults() -> None:
    _faults.clear()


def _maybe_fail(request: ProvisionRequest) -> None:
    for fault in _faults:
        if fault["left"] > 0 and fault["match"] in (request.family_code + "|" + request.account_reference):
            fault["left"] -= 1
            raise fault["error"]


def _parse(item: dict) -> NormalizedTransaction:
    when = parse_datetime(item.get("transaction_date", "")) or datetime.now(timezone.utc)
    return NormalizedTransaction(
        external_transaction_id=str(item["external_transaction_id"]), direction=item.get("direction", "credit"),
        amount_minor=int(item["amount_minor"]), transaction_date=when, currency=item.get("currency", "NGN"),
        transaction_reference=item.get("transaction_reference", ""), provider_session_id=item.get("provider_session_id", ""),
        transaction_type=item.get("transaction_type", "transfer"), sender_name=item.get("sender_name", ""),
        sender_account_number=item.get("sender_account_number", ""), sender_bank=item.get("sender_bank", ""),
        narration=item.get("narration", ""), raw_provider_reference=item.get("raw_provider_reference", ""),
        receiving_account_reference=item.get("receiving_account_reference", ""),
    )


class SandboxConnector(CollectionConnector):
    info = ProviderInfo(
        code="sandbox",
        display_name="Sandbox (test data)",
        icon="science",
        environments=(ENV_TEST,),
        credential_fields=(CredentialField("sandbox_key", "Sandbox key", help="Any key that starts with sandbox-"),),
        capabilities=CollectionCapabilities(
            supports_family_collection_accounts=True, supports_static_accounts=True, supports_dynamic_accounts=True,
            supports_account_deactivation=True, supports_account_reactivation=True, supports_account_closure=True,
            supports_webhooks=True, supports_transaction_requery=True, requires_customer_kyc=False,
        ),
        onboarding="Development and tests only.",
        webhook=WebhookSetup(mode="dashboard", where="Not needed: the sandbox signs its own test events.", verification="hmac_sha512", events=("payment",)),
        production_status=STATUS_SANDBOX,
        description="Synthetic accounts and payments for testing. Never counted as the school's money.",
        account_label="Test account number",
        payer_note="This is a test account. No real money can be paid into it.",
        connection_type="sandbox",
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings, **_) -> ValidatedConnection:
        key = str(credentials.get("sandbox_key") or "")
        if not key.startswith("sandbox-"):
            raise BadCredentials()
        return ValidatedConnection(
            merchant=MerchantProfile(display_name="Sandbox school", reference="****TEST"),
            secret={"sandbox_key": key, "webhook_secret": secrets.token_urlsafe(24)}, settings={},
        )

    def get_merchant_profile(self, secret, *, environment, settings, **_) -> MerchantProfile:
        self._require_live(secret)
        return MerchantProfile(display_name="Sandbox school", reference="****TEST")

    # -- family collection accounts -------------------------------------------------------------

    def provision_family_collection_account(self, secret, request: ProvisionRequest, *, environment, settings, **_) -> ProvisionedAccount:
        from ..models import SandboxProviderAccount

        self._require_live(secret)
        _maybe_fail(request)
        digest = hashlib.sha256(f"sandbox:{request.idempotency_key}".encode()).hexdigest()
        account, _ = SandboxProviderAccount.objects.get_or_create(
            connection_id=secret["connection_id"], reference=request.account_reference,
            defaults={"account_number": "9" + str(int(digest, 16) % 10**9).zfill(9)},
        )
        return ProvisionedAccount(
            account_number=account.account_number, provider_account_ref=request.account_reference, lookup_ref=account.account_number,
            account_name=f"TEST {request.family_name}"[:200], bank_name="SchoolOS Test Bank", number_label=self.info.account_label,
            public_details=[{"label": "Test only", "value": "Not a real bank account"}], provider_meta={"test": True}, ready=True,
        )

    def _account(self, secret: dict, account_ref: str):
        from ..models import SandboxProviderAccount

        return SandboxProviderAccount.objects.filter(connection_id=secret["connection_id"], reference=account_ref).first()

    def get_collection_account(self, secret, *, account_ref, environment, settings, **_) -> ProviderAccountState:
        account = self._account(secret, account_ref)
        return ProviderAccountState(account.status if account else "closed", account.account_number if account else "")

    def _set(self, secret, account_ref, status) -> None:
        account = self._account(secret, account_ref)
        if account is None:
            raise ProviderRejected("provider_rejected", "The sandbox has no such account.")
        account.status = status
        account.save(update_fields=["status"])

    def deactivate_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        self._set(secret, account_ref, "inactive")

    def reactivate_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        self._set(secret, account_ref, "active")

    def close_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        self._set(secret, account_ref, "closed")

    # -- payments -------------------------------------------------------------------------------

    def verify_transaction(self, secret, *, reference, environment, settings, **_) -> VerifiedPayment:
        from ..models import SandboxFeedItem

        for row in SandboxFeedItem.objects.filter(connection_id=secret["connection_id"]):
            if str(row.payload.get("external_transaction_id")) == reference:
                item = _parse(row.payload)
                return VerifiedPayment(
                    found=True, paid=True, amount_minor=item.amount_minor, currency=item.currency, reference=reference,
                    receiving_reference=item.receiving_account_reference, provider_status="success",
                )
        return VerifiedPayment(found=False)

    def handle_webhook(self, *, raw_body, headers, secret, environment=ENV_TEST, settings=None, **_) -> WebhookOutcome:
        given = headers.get(SIGNATURE_HEADER, "")
        expected = self.sign(raw_body, secret["webhook_secret"])
        if not (given and hmac.compare_digest(given, expected)):
            raise InvalidSignature()
        try:
            body = json.loads(raw_body)
        except ValueError:
            return WebhookOutcome(ignored=True)
        if "account_event" in body:
            event = body["account_event"]
            return WebhookOutcome(account_events=[AccountEvent(event.get("kind", "ready"), event.get("reference", ""), event.get("account_number", ""))])
        try:
            return WebhookOutcome(transactions=[_parse(body["transaction"])])
        except (KeyError, TypeError, ValueError):
            return WebhookOutcome(ignored=True)

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def sign(raw_body: bytes, webhook_secret: str) -> str:
        return hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    @staticmethod
    def _require_live(secret: dict) -> None:
        if not secret.get("sandbox_key"):
            raise BadCredentials()

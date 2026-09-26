"""Paystack, through the SCHOOL'S OWN Paystack account and secret key (never SchoolOS's own Paystack, which is only for what
schools pay SchoolOS).

Written only against Paystack's published API (its official OpenAPI, github.com/PaystackOSS/openapi, and the Dedicated Virtual
Account and Webhooks guides at docs-v2.paystack.com):

* authentication: `Authorization: Bearer <secret key>`, the key in the format `sk_<domain>_...`;
* a family's account is a Dedicated Virtual Account (DVA): `POST /customer` (email, first_name, last_name, phone, metadata) then
  `POST /dedicated_account` (customer code, preferred_bank); the response carries account_number, account_name, bank, id,
  active and assigned. Test keys use the bank slug `test-bank`; the documented live banks are `wema-bank` and `titan-paystack`;
* `GET /dedicated_account/{id}` reads it and `DELETE /dedicated_account/{id}` deactivates it. Paystack documents no way to
  reactivate a deactivated account, so this connector does not claim one;
* a payment is a `charge.success` webhook whose `data.authorization.channel` is `dedicated_nuban` and whose
  `data.authorization.receiver_bank_account_number` is the DVA that was paid; `data.amount` is in kobo;
* the webhook is authenticated by the `x-paystack-signature` header, an HMAC SHA512 of the raw body with the secret key;
  Paystack acknowledges nothing but a 200, and retries for 72 hours, so a delivery is processed idempotently;
* `GET /transaction/verify/{reference}` confirms a payment.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from django.conf import settings
from django.utils.dateparse import parse_datetime

from .base import (
    ENV_LIVE,
    ENV_TEST,
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
    ProviderUnavailable,
    ProvisionedAccount,
    ProvisionRequest,
    SettingField,
    ValidatedConnection,
    VerifiedPayment,
    WebhookOutcome,
    WebhookSetup,
)
from .transport import HttpResult, get_transport

SIGNATURE_HEADER = "x-paystack-signature"
LIVE_BANKS = ("wema-bank", "titan-paystack")
TEST_BANK = "test-bank"
DEFAULT_BASE_URL = "https://api.paystack.co"
CHANNEL_DVA = "dedicated_nuban"
#: How far back to look for a customer whose creation may have succeeded although the answer never arrived.
RECOVERY_PAGES = 10


def _base() -> str:
    return str(getattr(settings, "COLLECTION_PAYSTACK_BASE_URL", "") or DEFAULT_BASE_URL).rstrip("/")


def key_environment(secret_key: str) -> str | None:
    """`sk_live_...` is a live key and `sk_test_...` a test key (Paystack's documented `sk_<domain>_` format)."""
    if secret_key.startswith("sk_live_"):
        return ENV_LIVE
    if secret_key.startswith("sk_test_"):
        return ENV_TEST
    return None


def _call(secret: dict, method: str, path: str, *, body=None, params=None) -> dict:
    """One authenticated call. A refused key is `BadCredentials`; a failure to get an answer is `ProviderUnavailable`
    (outcome unknown); any other refusal is `ProviderRejected`. The provider's own words are never passed on."""
    key = str(secret.get("secret_key") or "")
    if not key:
        raise BadCredentials()
    result: HttpResult = get_transport().request(
        method, _base() + path, headers={"Authorization": f"Bearer {key}"}, json_body=body, params=params
    )
    if result.status in (401, 403):
        raise BadCredentials()
    if result.status >= 500 or (result.status == 429):
        raise ProviderUnavailable()
    if not isinstance(result.data, dict):
        raise ProviderUnavailable("Paystack's answer could not be read. Try again in a moment.")
    if not result.ok or result.data.get("status") is False:
        raise ProviderRejected("provider_rejected", "Paystack did not accept the request. Check the school's Paystack account and the details sent.")
    return result.data


def _minor(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderRejected("provider_unreadable", "Paystack sent an amount that could not be read.")
    return int(value)


def _when(data: dict) -> datetime:
    for key in ("paid_at", "paidAt", "created_at", "createdAt"):
        parsed = parse_datetime(str(data.get(key) or ""))
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


class PaystackConnector(CollectionConnector):
    info = ProviderInfo(
        code="paystack",
        display_name="Paystack",
        icon="payments",
        environments=(ENV_LIVE, ENV_TEST),
        credential_fields=(
            CredentialField(
                "secret_key", "Secret key", help="The secret key from your own Paystack dashboard. It starts with sk_live_ (live) or sk_test_ (test)."
            ),
        ),
        setting_fields=(
            SettingField(
                "preferred_bank", "Bank the family accounts are issued from", choices=LIVE_BANKS + (TEST_BANK,), required=False,
                default="",
            ),
        ),
        capabilities=CollectionCapabilities(
            supports_family_collection_accounts=True,
            supports_static_accounts=True,
            supports_dynamic_accounts=True,
            supports_account_deactivation=True,
            supports_account_reactivation=False,
            supports_account_closure=True,
            supports_webhooks=True,
            supports_transaction_requery=True,
            requires_customer_kyc=False,
        ),
        onboarding="Create and verify your school's own account at Paystack first, then enter the secret key Paystack issued to your school.",
        webhook=WebhookSetup(
            mode="dashboard", where="Your Paystack dashboard's API and webhook settings", verification="hmac_sha512",
            events=("charge.success", "dedicatedaccount.assign.success", "dedicatedaccount.assign.failed"),
            note="Paystack signs every event with your secret key. Use your live or test webhook address to match the key you entered.",
        ),
        description="Family payment accounts issued as Paystack Dedicated Virtual Accounts, on the school's own Paystack account.",
        account_label="Account number",
        payer_note="Pay by bank transfer to this account number.",
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings, **_) -> ValidatedConnection:
        key = str(credentials.get("secret_key") or "").strip()
        detected = key_environment(key)
        if detected is None:
            raise ProviderRejected("credentials_invalid", "A Paystack secret key starts with sk_live_ or sk_test_.")
        if detected != environment:
            raise ProviderRejected(
                "environment_mismatch", f"This is a {detected} key, but the connection was set up as {environment}. Choose the matching environment."
            )
        bank = str(settings.get("preferred_bank") or "").strip() or (TEST_BANK if environment == ENV_TEST else LIVE_BANKS[0])
        allowed = (TEST_BANK,) if environment == ENV_TEST else LIVE_BANKS
        if bank not in allowed:
            raise ProviderRejected("invalid_setting", f"Choose one of: {', '.join(allowed)}.")
        secret = {"secret_key": key}
        # A read that needs a working key and the Dedicated Virtual Account feature; a refused key is BadCredentials.
        _call(secret, "GET", "/dedicated_account/available_providers")
        return ValidatedConnection(
            merchant=MerchantProfile(display_name="Paystack account", reference="", meta={"environment": environment}),
            secret=secret, settings={"preferred_bank": bank},
        )

    def get_merchant_profile(self, secret, *, environment, settings, **_) -> MerchantProfile:
        _call(secret, "GET", "/dedicated_account/available_providers")
        return MerchantProfile(display_name="Paystack account", meta={"environment": environment})

    # -- family collection accounts -------------------------------------------------------------

    def _customer(self, secret: dict, request: ProvisionRequest) -> dict:
        """The Paystack customer for this family's payer, made once. Its code is kept on the request's checkpoint so a retry
        carries on from it rather than making another."""
        checkpoint = request.checkpoint
        if checkpoint.get("customer_code"):
            return checkpoint
        c = request.customer
        body = {
            "email": c.email, "first_name": c.first_name or c.name, "last_name": c.last_name or request.family_name, "phone": c.phone,
            "metadata": json.dumps({"schoolos_family": request.family_code, "schoolos_reference": request.account_reference}),
        }
        try:
            data = _call(secret, "POST", "/customer", body=body)["data"]
        except ProviderUnavailable:
            found = self._find_customer(secret, c.email)  # the customer may have been made although the answer never arrived
            if found is None:
                raise
            data = found
        except ProviderRejected:
            found = self._find_customer(secret, c.email)  # an existing customer with this email is fine to use
            if found is None:
                raise
            data = found
        checkpoint["customer_code"] = data["customer_code"]
        checkpoint["customer_id"] = data.get("id")
        return checkpoint

    def _find_customer(self, secret: dict, email: str):
        for page in range(1, RECOVERY_PAGES + 1):
            listed = _call(secret, "GET", "/customer", params={"perPage": "50", "page": str(page)}).get("data") or []
            for row in listed:
                if str(row.get("email", "")).lower() == email.lower() and row.get("customer_code"):
                    return row
            if len(listed) < 50:
                return None
        return None

    def provision_family_collection_account(self, secret, request, *, environment, settings, **_) -> ProvisionedAccount:
        if not request.customer.email:
            raise ProviderRejected("customer_details_missing", "Paystack needs an email address for the family's payer.")
        checkpoint = self._customer(secret, request)
        bank = str(settings.get("preferred_bank") or (TEST_BANK if environment == ENV_TEST else LIVE_BANKS[0]))
        if checkpoint.get("dva_id"):
            return self._as_account(self._fetch(secret, checkpoint["dva_id"]))
        try:
            data = _call(secret, "POST", "/dedicated_account", body={"customer": checkpoint["customer_code"], "preferred_bank": bank})["data"]
        except ProviderUnavailable:
            existing = self._existing_for_customer(secret, checkpoint.get("customer_id"))
            if existing is None:
                raise
            data = existing
        checkpoint["dva_id"] = data.get("id")
        return self._as_account(data)

    def _existing_for_customer(self, secret: dict, customer_id):
        if not customer_id:
            return None
        rows = _call(secret, "GET", "/dedicated_account", params={"customer": str(customer_id), "active": "true"}).get("data") or []
        return rows[0] if rows else None

    def _fetch(self, secret: dict, dva_id) -> dict:
        return _call(secret, "GET", f"/dedicated_account/{dva_id}")["data"]

    @staticmethod
    def _as_account(data: dict) -> ProvisionedAccount:
        bank = data.get("bank") or {}
        number = str(data.get("account_number") or "")
        if not number:
            raise ProviderRejected("provider_unreadable", "Paystack did not return an account number.")
        return ProvisionedAccount(
            account_number=number, provider_account_ref=str(data.get("id") or ""), lookup_ref=number,
            account_name=str(data.get("account_name") or ""), bank_name=str(bank.get("name") or ""),
            number_label="Account number", provider_meta={"bank_slug": bank.get("slug", ""), "currency": data.get("currency", "NGN")},
            ready=bool(data.get("active", True)) and bool(data.get("assigned", True)),
        )

    def get_collection_account(self, secret, *, account_ref, environment, settings, **_) -> ProviderAccountState:
        data = self._fetch(secret, account_ref)
        number = str(data.get("account_number") or "")
        if not data.get("active", True):
            return ProviderAccountState("inactive", number)
        return ProviderAccountState("active" if data.get("assigned", True) else "pending", number)

    def deactivate_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        _call(secret, "DELETE", f"/dedicated_account/{account_ref}")

    def close_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        self.deactivate_collection_account(secret, account_ref=account_ref, environment=environment, settings=settings)

    # -- payments -------------------------------------------------------------------------------

    def verify_transaction(self, secret, *, reference, environment, settings, **_) -> VerifiedPayment:
        try:
            data = _call(secret, "GET", f"/transaction/verify/{reference}")["data"]
        except ProviderRejected:
            return VerifiedPayment(found=False)
        authorization = data.get("authorization") or {}
        return VerifiedPayment(
            found=True, paid=data.get("status") == "success", amount_minor=_minor(data.get("amount")),
            currency=str(data.get("currency") or "NGN"), reference=str(data.get("reference") or reference),
            receiving_reference=str(authorization.get("receiver_bank_account_number") or ""), provider_status=str(data.get("status") or ""),
        )

    def handle_webhook(self, *, raw_body, headers, secret, environment=ENV_LIVE, settings=None, **_) -> WebhookOutcome:
        key = str(secret.get("secret_key") or "")
        given = str(headers.get(SIGNATURE_HEADER, ""))
        expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
        if not key or not given or not hmac.compare_digest(given.lower(), expected):
            raise InvalidSignature()
        try:
            body = json.loads(raw_body)
            event, data = body["event"], body["data"]
        except (ValueError, KeyError, TypeError):
            raise ConnectorError("unreadable", "The callback could not be read.") from None
        if event == "charge.success":
            authorization = data.get("authorization") or {}
            if authorization.get("channel") != CHANNEL_DVA:
                return WebhookOutcome(ignored=True)  # a card or other charge: not a payment into a family account
            tx = NormalizedTransaction(
                external_transaction_id=str(data["id"]), direction="credit", amount_minor=_minor(data["amount"]),
                transaction_date=_when(data), currency=str(data.get("currency") or "NGN"),
                transaction_reference=str(data.get("reference") or ""), provider_session_id="", transaction_type=CHANNEL_DVA,
                sender_name=str(authorization.get("sender_name") or ""),
                sender_account_number=str(authorization.get("sender_bank_account_number") or ""),
                sender_bank=str(authorization.get("sender_bank") or authorization.get("bank") or ""),
                narration=str(authorization.get("narration") or ""), raw_provider_reference=str(data.get("reference") or ""),
                receiving_account_reference=str(authorization.get("receiver_bank_account_number") or ""),
            )
            return WebhookOutcome(transactions=[tx])
        if event in ("dedicatedaccount.assign.success", "dedicatedaccount.assign.failed"):
            customer = data.get("customer") or {}
            account = data.get("dedicated_account") or {}
            return WebhookOutcome(
                account_events=[
                    AccountEvent(
                        "ready" if event.endswith("success") else "failed",
                        reference=str(customer.get("customer_code") or data.get("customer_code") or ""),
                        account_number=str(account.get("account_number") or ""),
                    )
                ]
            )
        return WebhookOutcome(ignored=True)

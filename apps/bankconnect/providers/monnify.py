"""Monnify, through the SCHOOL'S OWN Monnify merchant account (API key, secret key and contract code).

Written only against Monnify's published API (its official OpenAPI at developers.monnify.com/collection/monnify-collection.yml
and its Reserved Accounts, Webhooks and Verify Transactions guides):

* base URLs: `https://sandbox.monnify.com` (sandbox) and `https://api.monnify.com` (live);
* authentication: `POST /api/v1/auth/login` with `Authorization: Basic base64(apiKey:secretKey)` returns
  `responseBody.accessToken` (valid for `expiresIn` seconds); every other call sends `Authorization: Bearer <token>`;
* a family's account is a Customer Reserved Account: `POST /api/v2/bank-transfer/reserved-accounts` with accountReference (unique
  per merchant), accountName, currencyCode, contractCode, customerEmail, customerName, **bvn** (Monnify requires the customer's
  BVN or NIN), getAllAvailableBanks and preferredBanks (default 50515, Moniepoint Microfinance Bank). The response carries
  `accounts[]` (bankCode, bankName, accountNumber, accountName), `reservationReference` and `status`. One account per
  customerEmail;
* `GET /api/v2/bank-transfer/reserved-accounts/{accountReference}` reads it and
  `DELETE /api/v1/bank-transfer/reserved-accounts/reference/{accountReference}` deallocates it. Monnify documents no separate
  deactivate or reactivate, so this connector claims only closure;
* a payment is a `SUCCESSFUL_TRANSACTION` webhook; for a reserved account `eventData.product.type` is `RESERVED_ACCOUNT` and
  `eventData.product.reference` is the accountReference, `destinationAccountInformation.accountNumber` the account paid;
* the webhook carries `monnify-signature`, an HMAC-SHA512 of the request body with the client secret - **in production only**:
  sandbox notifications are unsigned, so a sandbox event is never trusted from its body: every one is confirmed by asking Monnify
  (`GET /api/v2/merchant/transactions/query`), and the identity of the account comes from that answer;
* Monnify documents webhook addresses as set in its dashboard (Developers > Webhook URLs), not by API.
"""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from .base import (
    ENV_LIVE,
    ENV_TEST,
    AlreadyExists,
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

SIGNATURE_HEADER = "monnify-signature"
BASE_URLS = {ENV_TEST: "https://sandbox.monnify.com", ENV_LIVE: "https://api.monnify.com"}
DEFAULT_PREFERRED_BANK = "50515"  # Monnify's documented default: Moniepoint Microfinance Bank
EVENT_COLLECTION = "SUCCESSFUL_TRANSACTION"
PRODUCT_RESERVED_ACCOUNT = "RESERVED_ACCOUNT"
#: A token is renewed this long before it expires.
TOKEN_MARGIN_SECONDS = 60
_tokens: dict = {}


def clear_token_cache() -> None:
    _tokens.clear()


def to_minor(value) -> int:
    """Monnify amounts are naira, as a number or a decimal string. Whole kobo only: anything finer is refused, never rounded."""
    try:
        kobo = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        raise ProviderRejected("provider_unreadable", "Monnify sent an amount that could not be read.") from None
    if kobo != kobo.to_integral_value() or kobo < 0:
        raise ProviderRejected("provider_unreadable", "Monnify sent an amount that could not be read.")
    return int(kobo)


def _paid_on(value) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _mask(value: str) -> str:
    return f"****{value[-4:]}" if len(value) >= 4 else "****"


def _token(secret: dict, environment: str, *, force: bool = False) -> str:
    api_key, secret_key = str(secret.get("api_key") or ""), str(secret.get("secret_key") or "")
    if not api_key or not secret_key:
        raise BadCredentials()
    cache_key = (environment, hashlib.sha256(f"{api_key}:{secret_key}".encode()).hexdigest())
    cached = _tokens.get(cache_key)
    if cached and not force and cached[1] - time.time() > TOKEN_MARGIN_SECONDS:
        return cached[0]
    basic = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    result = get_transport().request(
        "POST", BASE_URLS[environment] + "/api/v1/auth/login", headers={"Authorization": f"Basic {basic}"}
    )
    if result.status in (400, 401, 403):
        raise BadCredentials()
    if result.status >= 500 or result.status == 429 or not isinstance(result.data, dict):
        raise ProviderUnavailable()
    body = (result.data.get("responseBody") or {}) if result.data.get("requestSuccessful") else {}
    token = body.get("accessToken")
    if not token:
        raise BadCredentials()
    _tokens[cache_key] = (token, time.time() + float(body.get("expiresIn") or 0))
    return token


def _call(secret: dict, environment: str, method: str, path: str, *, body=None, params=None, allow=()) -> HttpResult:
    """One authenticated call. A stale token is renewed once. `allow` lists HTTP statuses the caller wants to look at itself."""
    for attempt in (0, 1):
        token = _token(secret, environment, force=bool(attempt))
        result = get_transport().request(
            method, BASE_URLS[environment] + path, headers={"Authorization": f"Bearer {token}"}, json_body=body, params=params
        )
        if result.status == 401 and not attempt:
            continue
        break
    if result.status == 401:
        raise BadCredentials()
    if result.status >= 500 or result.status == 429:
        raise ProviderUnavailable()
    if result.status in allow:
        return result
    if not isinstance(result.data, dict):
        raise ProviderUnavailable("Monnify's answer could not be read. Try again in a moment.")
    if not result.ok or result.data.get("requestSuccessful") is False:
        raise ProviderRejected("provider_rejected", "Monnify did not accept the request. Check the school's Monnify account and the details sent.")
    return result


class MonnifyConnector(CollectionConnector):
    info = ProviderInfo(
        code="monnify",
        display_name="Monnify",
        icon="payments",
        environments=(ENV_LIVE, ENV_TEST),
        credential_fields=(
            CredentialField("api_key", "API key", help="From Developers > API Keys & Contracts in your own Monnify dashboard."),
            CredentialField("secret_key", "Secret key", help="From the same page. Keep it private."),
            CredentialField("contract_code", "Contract code", secret=False, help="Your Monnify contract code, from the same page."),
        ),
        setting_fields=(
            SettingField("preferred_bank_code", "Bank code the family accounts are issued from", default=DEFAULT_PREFERRED_BANK),
        ),
        capabilities=CollectionCapabilities(
            supports_family_collection_accounts=True,
            supports_static_accounts=True,
            supports_dynamic_accounts=True,
            supports_account_deactivation=False,
            supports_account_reactivation=False,
            supports_account_closure=True,
            supports_webhooks=True,
            supports_transaction_requery=True,
            supports_direct_debit_mandates=True,
            requires_customer_kyc=True,
        ),
        onboarding="Create and verify your school's own Monnify merchant account first, then enter the API key, secret key and contract code Monnify issued to your school.",
        webhook=WebhookSetup(
            mode="dashboard", where="Developers > Webhook URLs in your Monnify dashboard (Transaction Completion)", verification="hmac_sha512",
            events=("SUCCESSFUL_TRANSACTION",),
            note="Monnify signs live notifications; sandbox notifications are not signed, so SchoolOS confirms each one with Monnify directly.",
        ),
        description="Family payment accounts issued as Monnify Customer Reserved Accounts, on the school's own Monnify account. Monnify requires the payer's BVN or NIN.",
        account_label="Account number",
        payer_note="Pay by bank transfer to this account number.",
        customer_requirements=("email", "identity"),
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings, **_) -> ValidatedConnection:
        secret = {k: str(credentials.get(k) or "").strip() for k in ("api_key", "secret_key", "contract_code")}
        _token(secret, environment, force=True)  # the key pair is proved by logging in
        bank = str(settings.get("preferred_bank_code") or DEFAULT_PREFERRED_BANK).strip()
        if not bank.isdigit():
            raise ProviderRejected("invalid_setting", "A bank code is made of digits.")
        return ValidatedConnection(
            merchant=MerchantProfile(display_name="Monnify merchant", reference=_mask(secret["contract_code"]), meta={"environment": environment}),
            secret=secret, settings={"preferred_bank_code": bank},
            # Monnify only reports a wrong contract code when the first account is made, so it is not claimed as verified.
            meta={"contract_code_verified": False},
        )

    def get_merchant_profile(self, secret, *, environment, settings, **_) -> MerchantProfile:
        _token(secret, environment, force=True)
        return MerchantProfile(display_name="Monnify merchant", reference=_mask(str(secret.get("contract_code") or "")), meta={"environment": environment})

    # -- family collection accounts -------------------------------------------------------------

    def provision_family_collection_account(self, secret, request: ProvisionRequest, *, environment, settings, **_) -> ProvisionedAccount:
        c = request.customer
        if not c.email:
            raise ProviderRejected("customer_details_missing", "Monnify needs an email address for the family's payer.")
        if not (c.bvn or c.nin):
            raise ProviderRejected("customer_kyc_required", "Monnify needs the family payer's BVN or NIN before it will make an account.")
        body = {
            "accountReference": request.account_reference, "accountName": request.account_name[:200], "currencyCode": request.currency,
            "contractCode": str(secret.get("contract_code") or ""), "customerEmail": c.email, "customerName": c.name or request.family_name,
            "getAllAvailableBanks": False, "preferredBanks": [str(settings.get("preferred_bank_code") or DEFAULT_PREFERRED_BANK)],
        }
        if c.bvn:
            body["bvn"] = c.bvn
        if c.nin:
            body["nin"] = c.nin
        try:
            result = _call(secret, environment, "POST", "/api/v2/bank-transfer/reserved-accounts", body=body, allow=(400, 422))
            if not result.ok or (isinstance(result.data, dict) and result.data.get("requestSuccessful") is False):
                raise self._refusal(result)
            details = result.data["responseBody"]
        except (AlreadyExists, ProviderUnavailable):
            # This reference may already have been made (an earlier attempt that never got its answer through): look before giving up.
            details = self._details(secret, environment, request.account_reference)
        return self._as_account(details)

    @staticmethod
    def _refusal(result: HttpResult) -> ConnectorError:
        message = str((result.data or {}).get("responseMessage") or "") if isinstance(result.data, dict) else ""
        if "same reference" in message.lower():  # Monnify's documented "You can not reserve two accounts with the same reference."
            return AlreadyExists()
        return ProviderRejected("provider_rejected", "Monnify did not accept the request. Check the family's details and the school's Monnify account.")

    def _details(self, secret: dict, environment: str, reference: str) -> dict:
        result = _call(secret, environment, "GET", f"/api/v2/bank-transfer/reserved-accounts/{quote(reference, safe='')}")
        return result.data["responseBody"]

    @staticmethod
    def _as_account(details: dict) -> ProvisionedAccount:
        accounts = details.get("accounts") or []
        first = accounts[0] if accounts else {}
        number = str(first.get("accountNumber") or "")
        if not number:
            raise ProviderRejected("provider_unreadable", "Monnify did not return an account number.")
        return ProvisionedAccount(
            account_number=number, provider_account_ref=str(details.get("accountReference") or ""),
            lookup_ref=str(details.get("accountReference") or ""),
            account_name=str(first.get("accountName") or details.get("accountName") or ""), bank_name=str(first.get("bankName") or ""),
            number_label="Account number", provider_meta={"reservation_reference": details.get("reservationReference", ""), "bank_code": first.get("bankCode", "")},
            ready=str(details.get("status") or "ACTIVE").upper() == "ACTIVE",
        )

    def get_collection_account(self, secret, *, account_ref, environment, settings, **_) -> ProviderAccountState:
        result = _call(secret, environment, "GET", f"/api/v2/bank-transfer/reserved-accounts/{quote(account_ref, safe='')}", allow=(404,))
        if result.status == 404:
            return ProviderAccountState("closed")
        body = (result.data or {}).get("responseBody") or {}
        accounts = body.get("accounts") or [{}]
        status = str(body.get("status") or "").upper()
        return ProviderAccountState("active" if status == "ACTIVE" else "inactive", str(accounts[0].get("accountNumber") or ""))

    def close_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        _call(secret, environment, "DELETE", f"/api/v1/bank-transfer/reserved-accounts/reference/{quote(account_ref, safe='')}")

    # -- payments -------------------------------------------------------------------------------

    def verify_transaction(self, secret, *, reference, environment, settings, **_) -> VerifiedPayment:
        result = _call(
            secret, environment, "GET", "/api/v2/merchant/transactions/query", params={"transactionReference": reference}, allow=(404,)
        )
        if result.status == 404:
            return VerifiedPayment(found=False)
        body = (result.data or {}).get("responseBody") or {}
        product = body.get("product") or {}
        return VerifiedPayment(
            found=True, paid=str(body.get("paymentStatus") or "").upper() == "PAID", amount_minor=to_minor(body.get("amountPaid") or 0),
            currency=str(body.get("currency") or "NGN"), reference=str(body.get("transactionReference") or reference),
            receiving_reference=str(product.get("reference") or "") if product.get("type") == PRODUCT_RESERVED_ACCOUNT else "",
            provider_status=str(body.get("paymentStatus") or ""),
        )

    def handle_webhook(self, *, raw_body, headers, secret, environment=ENV_LIVE, settings=None, **_) -> WebhookOutcome:
        given = str(headers.get(SIGNATURE_HEADER, ""))
        secret_key = str(secret.get("secret_key") or "")
        signed = bool(given)
        if signed:
            expected = hmac.new(secret_key.encode(), raw_body, hashlib.sha512).hexdigest()
            if not secret_key or not hmac.compare_digest(given.lower(), expected):
                raise InvalidSignature()
        elif environment != ENV_TEST:
            raise InvalidSignature()  # live notifications are always signed: an unsigned one is not Monnify's
        try:
            body = json.loads(raw_body)
            event, data = body["eventType"], body["eventData"]
        except (ValueError, KeyError, TypeError):
            raise ConnectorError("unreadable", "The callback could not be read.") from None
        if event != EVENT_COLLECTION:
            return WebhookOutcome(ignored=True)
        product = data.get("product") or {}
        if product.get("type") != PRODUCT_RESERVED_ACCOUNT:
            return WebhookOutcome(ignored=True)  # not a payment into a family's reserved account
        reference = str(data.get("transactionReference") or "")
        if not reference:
            raise ConnectorError("unreadable", "The callback could not be read.")
        if signed:
            paid = str(data.get("paymentStatus") or "").upper() == "PAID"
            amount = to_minor(data.get("amountPaid") or 0)
            receiving = str(product.get("reference") or "") or str((data.get("destinationAccountInformation") or {}).get("accountNumber") or "")
        else:
            # Unsigned (sandbox): nothing in the body is trusted. Monnify itself is asked, and its answer decides.
            verified = self.verify_transaction(secret, reference=reference, environment=environment, settings=settings or {})
            if not verified.found:
                raise InvalidSignature()
            paid, amount, receiving = verified.paid, verified.amount_minor, verified.receiving_reference
        if not paid or amount <= 0:
            return WebhookOutcome(ignored=True)
        source = (data.get("paymentSourceInformation") or [{}])[0] if isinstance(data.get("paymentSourceInformation"), list) else {}
        tx = NormalizedTransaction(
            external_transaction_id=reference, direction="credit", amount_minor=amount, transaction_date=_paid_on(data.get("paidOn")),
            currency=str(data.get("currency") or "NGN"), transaction_reference=str(data.get("paymentReference") or ""),
            provider_session_id=str(source.get("sessionId") or ""), transaction_type=str(data.get("paymentMethod") or ""),
            sender_name=str(source.get("accountName") or ""), sender_account_number=str(source.get("accountNumber") or ""),
            sender_bank=str(source.get("bankCode") or ""), narration=str(data.get("paymentDescription") or ""),
            raw_provider_reference=reference, receiving_account_reference=receiving,
        )
        return WebhookOutcome(transactions=[tx])

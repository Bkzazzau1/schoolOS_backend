"""Remita, through the SCHOOL'S OWN Remita merchant account (merchant id, API key and service type id).

Written only against Remita's published API documentation (the "Remita APIs" documentation at api.remita.net: Invoice Generation,
Check Transaction Status, Cancel Invoice and the Payment Notification webhook). Remita is different from the other two providers
and this connector does not pretend otherwise:

* Remita documents **no virtual or dedicated bank accounts**. Its collection primitive is the invoice: a payment reference, the
  Remita Retrieval Reference (RRR), created for a payer and an amount. So a family's "collection account" here is an RRR for the
  family's collection target. It is fixed-amount and made for one collection, so it is a DYNAMIC account only; there is no static
  reusable account;
* invoice: `POST .../echannelsvc/merchant/api/paymentinit` with serviceTypeId, amount, orderId (our unique reference), payerName,
  payerEmail, payerPhone, description and an optional expiryDate (DD/MM/YYYY). Authentication is the header
  `Authorization: remitaConsumerKey=<merchantId>,remitaConsumerToken=<apiHash>` where, for an invoice, apiHash is the SHA-512 of
  merchantId + serviceTypeId + orderId + totalAmount + apiKey. The answer is `statuscode` `025` and the `RRR`;
* status: `GET .../echannelsvc/<merchantId>/<rrr>/<apiHash>/status.reg` with apiHash = SHA-512(rrr + apiKey + merchantId), or by order
  id (`.../orderstatus.reg`, SHA-512(orderId + apiKey + merchantId)). Status `00` or `01` means paid;
* cancel: `POST .../echannelsvc/v2/api/deactivate.json` with rrr, merchantId and hash = SHA-512(rrr + apiKey + merchantId);
* payment notification: a JSON ARRAY posted to the address the school gives Remita; the reply is `Ok` (or `Not Ok`). **Remita
  documents no signature on it**, so a notification body is never trusted: every RRR in it is confirmed by an authenticated status
  check, and the amount and identity come from that answer;
* mandates / direct debit are a Remita product too (`supports_direct_debit_mandates`); they are not part of this connector and
  are never mixed into the family receivables model;
* Remita's documentation gives only its demo host. The live host is not in it, so it is taken from the server's configuration
  (`COLLECTION_REMITA_LIVE_BASE_URL`) and a live connection is refused until an operator sets it.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from django.conf import settings as django_settings

from .base import (
    ENV_LIVE,
    ENV_TEST,
    AlreadyExists,
    BadCredentials,
    CollectionCapabilities,
    CollectionConnector,
    ConnectorError,
    CredentialField,
    MerchantProfile,
    NormalizedTransaction,
    ProviderAccountState,
    ProviderInfo,
    ProviderRejected,
    ProviderUnavailable,
    ProvisionedAccount,
    ProvisionRequest,
    ValidatedConnection,
    VerifiedPayment,
    WebhookOutcome,
    WebhookSetup,
)
from .monnify import to_minor
from .transport import get_transport

DEMO_BASE = "https://demo.remita.net/remita/exapp/api/v1/send/api"
DEMO_CANCEL_BASE = "https://remitademo.net/remita/exapp/api/v1/send/api"
DUMMY_RRR = "000000000000"
#: Remita's documented status codes.
CODE_GENERATED = "025"
CODES_PAID = ("00", "01")
CODES_AUTH = ("013", "020", "033")
CODES_DUPLICATE = ("028", "055")
CODE_CANCELLED_OK = "00"
_JSONP = re.compile(r"^\s*jsonp\s*\((.*)\)\s*;?\s*$", re.S)


def _base(environment: str) -> str:
    if environment == ENV_TEST:
        return DEMO_BASE
    live = str(getattr(django_settings, "COLLECTION_REMITA_LIVE_BASE_URL", "") or "").rstrip("/")
    if not live:
        raise ProviderRejected(
            "live_not_configured", "Remita's live address has not been configured on this SchoolOS server yet, so a live Remita connection cannot be made."
        )
    return live


def _cancel_base(environment: str) -> str:
    return DEMO_CANCEL_BASE if environment == ENV_TEST else _base(environment)


def sha512(*parts: str) -> str:
    return hashlib.sha512("".join(parts).encode()).hexdigest()


def _auth(merchant_id: str, api_hash: str) -> dict:
    return {"Authorization": f"remitaConsumerKey={merchant_id},remitaConsumerToken={api_hash}", "Content-Type": "application/json"}


def _json(result):
    """Remita answers some calls as plain JSON and others as `jsonp ({...})`."""
    if isinstance(result.data, (dict, list)):
        return result.data
    match = _JSONP.match(result.text or "")
    if match:
        try:
            return json.loads(match.group(1))
        except ValueError:
            return None
    return None


def _amount_text(amount_minor: int) -> str:
    naira = Decimal(amount_minor) / 100
    return str(int(naira)) if naira == naira.to_integral_value() else f"{naira:.2f}"


def _creds(secret: dict) -> tuple[str, str, str]:
    merchant, key, service = (str(secret.get(k) or "") for k in ("merchant_id", "api_key", "service_type_id"))
    if not (merchant and key and service):
        raise BadCredentials()
    return merchant, key, service


def _status_by_rrr(secret: dict, environment: str, rrr: str) -> dict:
    merchant, key, _ = _creds(secret)
    api_hash = sha512(rrr, key, merchant)
    result = get_transport().request("GET", f"{_base(environment)}/echannelsvc/{merchant}/{rrr}/{api_hash}/status.reg", headers=_auth(merchant, api_hash))
    return _read_status(result)


def _status_by_order(secret: dict, environment: str, order_id: str) -> dict:
    merchant, key, _ = _creds(secret)
    api_hash = sha512(order_id, key, merchant)
    result = get_transport().request("GET", f"{_base(environment)}/echannelsvc/{merchant}/{order_id}/{api_hash}/orderstatus.reg", headers=_auth(merchant, api_hash))
    return _read_status(result)


def _read_status(result) -> dict:
    if result.status >= 500 or result.status == 429:
        raise ProviderUnavailable()
    body = _json(result)
    if not isinstance(body, dict):
        raise ProviderUnavailable("Remita's answer could not be read. Try again in a moment.")
    code = str(body.get("status") or body.get("statuscode") or "")
    if code in CODES_AUTH or result.status in (401, 403):
        raise BadCredentials()
    return body


class RemitaConnector(CollectionConnector):
    info = ProviderInfo(
        code="remita",
        display_name="Remita",
        icon="payments",
        environments=(ENV_LIVE, ENV_TEST),
        credential_fields=(
            CredentialField("merchant_id", "Merchant ID", secret=False, help="From the API Keys and Webhooks page of your own Remita account."),
            CredentialField("api_key", "API key", help="From the same page. Keep it private."),
            CredentialField("service_type_id", "Service type ID", secret=False, help="The service type you created on Remita for school fees."),
        ),
        capabilities=CollectionCapabilities(
            supports_family_collection_accounts=True,
            supports_static_accounts=False,
            supports_dynamic_accounts=True,
            supports_account_deactivation=False,
            supports_account_reactivation=False,
            supports_account_closure=True,
            supports_webhooks=True,
            supports_transaction_requery=True,
            supports_direct_debit_mandates=True,
            requires_customer_kyc=False,
        ),
        onboarding="Register your school's own Remita account and complete its KYC first, then enter the merchant id, API key and service type id Remita issued to your school.",
        webhook=WebhookSetup(
            mode="dashboard", where="The API Keys and Webhooks page of your Remita account (the listening URL for payment notifications)",
            verification="requery", events=("payment notification",),
            note="Remita does not sign its notifications, so SchoolOS confirms every payment with Remita directly before it counts.",
        ),
        description="Family payment references made as Remita invoices (an RRR for the family's collection target), on the school's own Remita account. Remita has no reusable family bank account.",
        account_label="Remita Retrieval Reference (RRR)",
        payer_note="Pay this Remita Retrieval Reference through Remita.",
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings, **_) -> ValidatedConnection:
        secret = {k: str(credentials.get(k) or "").strip() for k in ("merchant_id", "api_key", "service_type_id")}
        _creds(secret)
        _base(environment)  # a live connection needs the server's live address
        # Remita documents no read that only proves the keys, so a status check for an RRR that cannot exist is used: a wrong
        # merchant id or key is answered with an authentication code (013, 020 or 033), anything else means the keys were accepted.
        _status_by_rrr(secret, environment, DUMMY_RRR)
        return ValidatedConnection(
            merchant=MerchantProfile(display_name="Remita merchant", reference=f"****{secret['merchant_id'][-4:]}", meta={"environment": environment}),
            secret=secret, settings={},
            # The service type is only checked when the first invoice is made.
            meta={"service_type_verified": False, "credentials_check": "status_check"},
        )

    def get_merchant_profile(self, secret, *, environment, settings, **_) -> MerchantProfile:
        _status_by_rrr(secret, environment, DUMMY_RRR)
        return MerchantProfile(display_name="Remita merchant", reference=f"****{str(secret.get('merchant_id') or '')[-4:]}", meta={"environment": environment})

    # -- family collection accounts (an invoice, hence an RRR) ---------------------------------

    def provision_family_collection_account(self, secret, request: ProvisionRequest, *, environment, settings, **_) -> ProvisionedAccount:
        c = request.customer
        if not (c.email and c.phone and (c.name or request.family_name)):
            raise ProviderRejected("customer_details_missing", "Remita needs the family payer's name, email address and phone number.")
        if not request.amount_minor or request.amount_minor <= 0:
            raise ProviderRejected("amount_required", "A Remita payment reference is made for an amount: there is nothing to collect.")
        merchant, key, service = _creds(secret)
        amount = _amount_text(request.amount_minor)
        order_id = request.account_reference
        body = {
            "serviceTypeId": service, "amount": amount, "orderId": order_id, "payerName": c.name or request.family_name,
            "payerEmail": c.email, "payerPhone": c.phone, "description": (request.description or f"School fees - {request.family_name}")[:250],
        }
        if request.valid_until:
            body["expiryDate"] = request.valid_until.strftime("%d/%m/%Y")
        api_hash = sha512(merchant, service, order_id, amount, key)
        try:
            result = get_transport().request("POST", f"{_base(environment)}/echannelsvc/merchant/api/paymentinit", headers=_auth(merchant, api_hash), json_body=body)
            answer = self._read_generated(result)
        except (AlreadyExists, ProviderUnavailable):
            # The order id may already have an RRR (an earlier attempt whose answer never arrived): ask Remita by order id.
            answer = _status_by_order(secret, environment, order_id)
            if not answer.get("RRR"):
                raise
        rrr = str(answer.get("RRR") or "")
        if not rrr:
            raise ProviderRejected("provider_unreadable", "Remita did not return a payment reference.")
        return ProvisionedAccount(
            account_number=rrr, provider_account_ref=rrr, lookup_ref=rrr, account_name=c.name or request.family_name, bank_name="Remita",
            number_label=self.info.account_label,
            public_details=[{"label": "Amount to pay", "value": f"NGN {request.amount_minor // 100:,}.{request.amount_minor % 100:02d}"}],
            provider_meta={"order_id": order_id, "service_type_id": service}, ready=True,
        )

    @staticmethod
    def _read_generated(result) -> dict:
        if result.status >= 500 or result.status == 429:
            raise ProviderUnavailable()
        body = _json(result)
        if not isinstance(body, dict):
            raise ProviderUnavailable("Remita's answer could not be read. Try again in a moment.")
        code = str(body.get("statuscode") or body.get("status") or "")
        if code in CODES_AUTH or result.status in (401, 403):
            raise BadCredentials()
        if code in CODES_DUPLICATE:
            raise AlreadyExists()
        if code != CODE_GENERATED:
            raise ProviderRejected("provider_rejected", "Remita did not make the payment reference. Check the school's Remita account and the family's details.")
        return body

    def get_collection_account(self, secret, *, account_ref, environment, settings, **_) -> ProviderAccountState:
        body = _status_by_rrr(secret, environment, account_ref)
        code = str(body.get("status") or "")
        if code in CODES_PAID:
            return ProviderAccountState("closed", account_ref, "paid")
        return ProviderAccountState("active" if code in ("021", "025", "045") else "unknown", account_ref)

    def close_collection_account(self, secret, *, account_ref, environment, settings, **_) -> None:
        merchant, key, _ = _creds(secret)
        result = get_transport().request(
            "POST", f"{_cancel_base(environment)}/echannelsvc/v2/api/deactivate.json", headers={"Content-Type": "application/json"},
            json_body={"rrr": account_ref, "merchantId": merchant, "hash": sha512(account_ref, key, merchant)},
        )
        if result.status >= 500 or result.status == 429:
            raise ProviderUnavailable()
        body = _json(result)
        if not isinstance(body, dict):
            raise ProviderUnavailable("Remita's answer could not be read. Try again in a moment.")
        if str(body.get("statuscode") or "") != CODE_CANCELLED_OK:
            raise ProviderRejected("provider_rejected", "Remita did not cancel the payment reference. It may already have been paid.")

    # -- payments -------------------------------------------------------------------------------

    def verify_transaction(self, secret, *, reference, environment, settings, **_) -> VerifiedPayment:
        body = _status_by_rrr(secret, environment, reference)
        code = str(body.get("status") or "")
        if not body.get("RRR"):
            return VerifiedPayment(found=False)
        return VerifiedPayment(
            found=True, paid=code in CODES_PAID, amount_minor=to_minor(body.get("amount") or 0), currency="NGN",
            reference=str(body.get("RRR")), receiving_reference=str(body.get("RRR")), provider_status=code,
        )

    def handle_webhook(self, *, raw_body, headers, secret, environment=ENV_LIVE, settings=None, **_) -> WebhookOutcome:
        try:
            items = json.loads(raw_body)
        except ValueError:
            raise ConnectorError("unreadable", "The callback could not be read.") from None
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise ConnectorError("unreadable", "The callback could not be read.")
        transactions = []
        for item in items:
            rrr = str(item.get("rrr") or item.get("RRR") or "") if isinstance(item, dict) else ""
            if not rrr:
                continue
            # No signature is documented, so nothing in the body is trusted: Remita is asked, and its answer decides.
            verified = self.verify_transaction(secret, reference=rrr, environment=environment, settings=settings or {})
            if not (verified.found and verified.paid and verified.amount_minor > 0):
                continue
            transactions.append(
                NormalizedTransaction(
                    external_transaction_id=rrr, direction="credit", amount_minor=verified.amount_minor,
                    transaction_date=self._when(item), currency="NGN", transaction_reference=str(item.get("orderRef") or item.get("orderId") or ""),
                    transaction_type=str(item.get("channel") or ""), sender_name=str(item.get("payerName") or ""),
                    sender_bank=str(item.get("bank") or ""), narration=str(item.get("paymentDescription") or ""),
                    raw_provider_reference=rrr, receiving_account_reference=verified.receiving_reference,
                )
            )
        return WebhookOutcome(transactions=transactions, ignored=not transactions)

    @staticmethod
    def _when(item: dict) -> datetime:
        for key in ("debitdate", "transactiondate"):
            try:
                return datetime.strptime(str(item.get(key)), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return datetime.now(timezone.utc)

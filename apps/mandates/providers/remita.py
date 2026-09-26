"""Remita Direct Debit, through the SCHOOL'S OWN Remita merchant credentials (merchant id, service type id, API key and API token).

Written only against Remita's published API documentation (the "Remita APIs" collection at api.remita.net, section "Direct Debit (For
Non-Financial Institutions)": Generate Mandates, Activate Mandate, Mandate Operations, Check Status and Webhook Notifications, with its Fields and
Status Codes). Nothing here is guessed. What the documentation says, and what SchoolOS therefore does:

* a mandate is set up with `mandate/setup`; SchoolOS makes a VARIABLE mandate (`mandateType` "DD": a maximum amount and a maximum number of
  debits), the kind a debit can be sent on when the school chooses. The hash is SHA-512 of merchantId + serviceTypeId + requestId + amount + apiKey;
* the payer activates it in one of two ways: with a bank one-time password entered in SchoolOS (`requestAuthorization` and
  `validateAuthorization`, only for the banks Remita lists, personal accounts only; the headers carry MERCHANT_ID, API_KEY, REQUEST_ID, REQUEST_TS
  and API_DETAILS_HASH = SHA-512 of apiKey + requestId + apiToken), or by printing the mandate form Remita gives and signing it for their bank;
* `mandate/status` says whether the mandate is active; `mandate/stop` ends it (there is no documented pause, so SchoolOS offers cancel only);
* a debit is `mandate/payment/send` (hash: merchantId + serviceTypeId + requestId + amount + apiKey) with the payer's funding account and bank
  code, and `mandate/payment/status` asks about it by the same request id. Remita's own status code says whether it is paid: only `01` is;
* Remita posts `ACTIVATION` and `DEBIT` notifications to a listening URL. **No signature is documented on them**, so nothing in a notification body
  is trusted: SchoolOS asks Remita (mandate status, debit status) and its answer decides.

Remita's documentation gives only its demo host. The live host is not in it, so it is taken from the server's configuration
(`MANDATES_REMITA_LIVE_BASE_URL`) and a live connection is refused until an operator sets it. Remita also requires a UAT with it before go-live.
Where the documentation is silent or ambiguous, SchoolOS refuses rather than guesses: credentials are checked with a status query for a mandate that
cannot exist (documented codes: `013` invalid hash / `020` authentication error mean bad credentials, `074` no record means the hash was accepted).
"""

import hashlib
import json
import time
from datetime import date, datetime, timezone

from django.conf import settings as django_settings

from ..constants import DebitOutcome, MandateStatus
from .base import (
    ENV_LIVE,
    ENV_TEST,
    ActivationChallenge,
    BadCredentials,
    BankInfo,
    ConnectorError,
    CreateMandateRequest,
    CredentialField,
    DebitRequest,
    DebitResult,
    MandateCapabilities,
    MandateConnector,
    MandateProviderInfo,
    MerchantProfile,
    ProviderEvent,
    ProviderMandate,
    ProviderRejected,
    ProviderUnavailable,
    ValidatedConnection,
    WebhookOutcome,
    WebhookSetup,
)
from apps.bankconnect.providers.transport import get_transport

DEMO_BASE = "https://demo.remita.net/remita/exapp/api/v1/send/api"
DEMO_FORM_BASE = "https://demo.remita.net/remita/ecomm"
MANDATE = "/echannelsvc/echannel/mandate"
DUMMY_MANDATE = "000000000000"
MAX_NOTIFICATION_ITEMS = 200

#: Banks whose payers can activate in SchoolOS with a one-time password (Remita's list, by name). Others use the printed, signed form.
SELF_ACTIVATION = ("214", "057", "070", "301", "101", "030", "215")
#: The commercial banks in Remita's published Bank Codes table (General Reference > Bank Codes). The table also lists many microfinance banks.
BANKS = (
    ("011", "First Bank of Nigeria"), ("023", "Citibank Nigeria"), ("030", "Heritage Bank"), ("032", "Union Bank of Nigeria"),
    ("033", "United Bank for Africa"), ("035", "Wema Bank"), ("039", "Stanbic IBTC Bank"), ("044", "Access Bank"), ("050", "Ecobank Nigeria"),
    ("057", "Zenith Bank"), ("058", "Guaranty Trust Bank"), ("068", "Standard Chartered Bank Nigeria"), ("070", "Fidelity Bank"),
    ("076", "Polaris Bank"), ("101", "Providus Bank"), ("214", "First City Monument Bank"), ("215", "Unity Bank"), ("232", "Sterling Bank"),
    ("301", "Jaiz Bank"), ("459", "Coronation Merchant Bank"), ("480", "Jubilee Bank"),
)

#: Remita's documented status codes for a debit, in SchoolOS's words: (outcome, failure code).
DEBIT_CODES = {
    "01": (DebitOutcome.SUCCESS, ""),
    "069": (DebitOutcome.PENDING, ""), "070": (DebitOutcome.PENDING, ""), "071": (DebitOutcome.PENDING, ""), "072": (DebitOutcome.PENDING, ""),
    "051": (DebitOutcome.FAILED, "insufficient_funds"), "107": (DebitOutcome.FAILED, "insufficient_funds"),
    "034": (DebitOutcome.FAILED, "invalid_funding_source"), "035": (DebitOutcome.FAILED, "payment_limit_exceeded"),
    "061": (DebitOutcome.FAILED, "mandate_not_activated"), "062": (DebitOutcome.FAILED, "mandate_not_due"),
    "063": (DebitOutcome.FAILED, "mandate_expired"), "066": (DebitOutcome.FAILED, "mandate_deactivated"),
    "067": (DebitOutcome.FAILED, "invalid_mandate_type"), "073": (DebitOutcome.FAILED, "invalid_mandate_type"),
    "074": (DebitOutcome.NOT_FOUND, ""),
}


def _sha512(*parts: str) -> str:
    return hashlib.sha512("".join(str(p) for p in parts).encode()).hexdigest()


def _naira(minor: int) -> str:
    """Minor units as the plain naira string Remita's examples use ("10000", or "10000.50" when there are kobo)."""
    whole, kobo = divmod(int(minor), 100)
    return f"{whole}" if kobo == 0 else f"{whole}.{kobo:02d}"


def _to_minor(value) -> int | None:
    try:
        whole, _, frac = str(value).strip().partition(".")
        return int(whole) * 100 + int((frac + "00")[:2])
    except (ValueError, TypeError):
        return None


def _base(environment: str) -> str:
    if environment == ENV_TEST:
        return DEMO_BASE
    live = str(getattr(django_settings, "MANDATES_REMITA_LIVE_BASE_URL", "") or "").rstrip("/")
    if not live:
        raise ProviderRejected(
            "live_not_configured",
            "Remita's live address has not been configured on this SchoolOS server yet, so a live Remita connection cannot be made.",
        )
    return live


def _form_base(environment: str) -> str:
    if environment == ENV_TEST:
        return DEMO_FORM_BASE
    return str(getattr(django_settings, "MANDATES_REMITA_LIVE_FORM_BASE_URL", "") or "").rstrip("/")


def _now_ref() -> str:
    return str(time.time_ns() // 1_000_000)


def _parse(result):
    """Remita answers some calls as plain JSON and others as `jsonp ({...})`."""
    data = result.data
    if data is None and result.text:
        text = result.text.strip()
        if text.lower().startswith("jsonp"):
            text = text[text.index("(") + 1: text.rindex(")")] if "(" in text and ")" in text else ""
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return data if isinstance(data, dict) else None


def _post(environment: str, path: str, body: dict, *, headers: dict | None = None) -> dict:
    result = get_transport().request("POST", _base(environment) + path, json_body=body, headers={"Content-Type": "application/json", **(headers or {})})
    answer = _parse(result)
    if result.status >= 500:
        raise ProviderUnavailable()
    if answer is None:
        # A reply that cannot be read after the request went out: the outcome is not known.
        raise ProviderUnavailable("Remita's answer could not be read. Try again in a moment.")
    return answer


def _code(answer: dict) -> str:
    return str(answer.get("statuscode") or answer.get("statusCode") or "")


def _check_auth(answer: dict) -> None:
    code = _code(answer)
    if code in ("013", "020"):
        raise BadCredentials()


def _headers(secret: dict) -> dict:
    """The headers the OTP activation calls carry: a fresh request id and a hash of the API key, that id and the API token."""
    request_id = _now_ref()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+000000")
    return {
        "MERCHANT_ID": secret["merchant_id"], "API_KEY": secret["api_key"], "REQUEST_ID": request_id, "REQUEST_TS": stamp,
        "API_DETAILS_HASH": _sha512(secret["api_key"], request_id, secret["api_token"]),
    }


def _date_out(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _date_in(value) -> date | None:
    try:
        return datetime.strptime(str(value), "%d/%m/%Y").date()
    except ValueError:
        return None


def _mandate_from(ref: str, answer: dict) -> ProviderMandate:
    code = _code(answer)
    active = answer.get("isActive") is True
    end = _date_in(answer.get("endDate"))
    if code == "063" or (end is not None and end < date.today()):
        status = MandateStatus.EXPIRED
    elif code == "066":
        status = MandateStatus.CANCELLED
    elif code == "00" and active:
        status = MandateStatus.ACTIVE
    elif code in ("00", "061", "040"):
        status = MandateStatus.PENDING_ACTIVATION
    else:
        raise ProviderRejected("provider_unreadable", "Remita's answer about the mandate could not be understood.")
    return ProviderMandate(
        provider_ref=str(answer.get("mandateId") or ref), status=status, provider_status=str(answer.get("status") or "")[:120], provider_status_code=code,
        mandate_code=str(answer.get("mandateId") or ref), is_active=status == MandateStatus.ACTIVE, start_date=_date_in(answer.get("startDate")),
        end_date=end,
    )


def _debit_from(answer: dict, *, default_amount: int | None = None) -> DebitResult:
    code = _code(answer)
    outcome, failure = DEBIT_CODES.get(code, (DebitOutcome.UNKNOWN, ""))
    amount = _to_minor(answer.get("amount")) if answer.get("amount") not in (None, "") else default_amount
    return DebitResult(
        outcome=outcome, provider_status=str(answer.get("status") or "")[:120], provider_status_code=code,
        provider_reference=str(answer.get("RRR") or answer.get("rrr") or ""), amount_minor=amount, failure_code=failure,
        meta={"transactionRef": str(answer.get("transactionRef") or "")},
    )


class RemitaMandateConnector(MandateConnector):
    info = MandateProviderInfo(
        code="remita",
        display_name="Remita",
        icon="payments",
        environments=(ENV_TEST, ENV_LIVE),
        credential_fields=(
            CredentialField("merchant_id", "Merchant ID", secret=False, help="Your school's own Remita merchant id."),
            CredentialField("service_type_id", "Service type ID", secret=False, help="The service type your school created on Remita for school fees."),
            CredentialField("api_key", "API key", help="The API key Remita issued to your school for Direct Debit."),
            CredentialField("api_token", "API token", help="The API token Remita issued to your school (used when a payer activates with a bank one-time password)."),
        ),
        capabilities=MandateCapabilities(
            supports_provider_hosted_consent=True, supports_remote_cancellation=True, supports_variable_amount_mandate=True,
            supports_manual_debit=True, supports_webhooks=True, supports_status_requery=True, supports_debit_status=True,
            supports_otp_activation=True, supports_form_activation=True, requires_account_number_for_debit=True,
        ),
        onboarding=(
            "Register your school's own Remita account and complete its KYC, then enter the merchant id, service type id, API key and API token "
            "Remita issued to your school for Direct Debit. Remita requires a UAT with it before you go live."
        ),
        webhook=WebhookSetup(
            mode="dashboard", where="Give Remita the address below as the listening URL for your Direct Debit notifications", verification="requery",
            events=("ACTIVATION", "DEBIT"),
            note="Remita does not sign its notifications, so SchoolOS confirms every one with Remita before it counts.",
        ),
        description="Direct debit mandates on the payer's bank account, activated with a bank one-time password or a signed form.",
        live_note="Live use needs Remita's live address (set by whoever runs this server) and a UAT with Remita.",
        activation_note=(
            "The payer activates the mandate with a one-time password from their bank (for the banks Remita lists) or by signing the mandate form "
            "and taking it to their bank."
        ),
        maximum_note="The most that can be debited in a calendar month.",
        maximum_scope="calendar_month",
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings) -> ValidatedConnection:
        secret = {k: str(credentials.get(k) or "") for k in ("merchant_id", "service_type_id", "api_key", "api_token")}
        self._probe(secret, environment)
        return ValidatedConnection(
            merchant=MerchantProfile(display_name="Remita merchant", reference=f"****{secret['merchant_id'][-4:]}", meta={"environment": environment}),
            secret=secret,
        )

    def get_merchant_profile(self, secret, *, environment, settings) -> MerchantProfile:
        self._probe(secret, environment)
        return MerchantProfile(display_name="Remita merchant", reference=f"****{str(secret.get('merchant_id') or '')[-4:]}", meta={"environment": environment})

    def _probe(self, secret: dict, environment: str) -> None:
        """Remita documents no read that only proves the keys. A status query for a mandate that cannot exist is answered `074` (no record) when
        the hash was accepted, and `013` / `020` when it was not."""
        request_id = _now_ref()
        answer = _post(environment, MANDATE + "/status", {
            "merchantId": secret["merchant_id"], "mandateId": DUMMY_MANDATE, "requestId": request_id,
            "hash": _sha512(DUMMY_MANDATE, secret["merchant_id"], request_id, secret["api_key"]),
        })
        _check_auth(answer)
        if _code(answer) != "074":
            raise ProviderRejected("provider_unreadable", "Remita's answer could not be understood. Check the credentials and try again.")

    def list_supported_banks(self, secret, *, environment, settings) -> list[BankInfo]:
        return [BankInfo(code, name, self_activation=code in SELF_ACTIVATION) for code, name in BANKS]

    # -- a mandate ------------------------------------------------------------------------------

    def create_mandate(self, secret, request: CreateMandateRequest, *, environment, settings) -> ProviderMandate:
        if request.existing_ref:
            # An earlier attempt reached Remita: ask about it rather than make a second mandate.
            return self.get_mandate_status(secret, mandate_ref=request.existing_ref, environment=environment, settings=settings, request_ref=request.request_ref)
        amount = _naira(request.maximum_amount_minor)
        answer = _post(environment, MANDATE + "/setup", {
            "merchantId": secret["merchant_id"], "serviceTypeId": secret["service_type_id"], "requestId": request.request_ref,
            "hash": _sha512(secret["merchant_id"], secret["service_type_id"], request.request_ref, amount, secret["api_key"]),
            "payerName": request.payer.name, "payerEmail": request.payer.email, "payerPhone": request.payer.phone,
            "payerBankCode": request.bank_code, "payerAccount": request.account_number, "amount": amount,
            "startDate": _date_out(request.start_date), "endDate": _date_out(request.end_date), "mandateType": "DD",
            "maxNoOfDebits": str(request.max_debits or 1),
        })
        _check_auth(answer)
        if _code(answer) != "040" or not answer.get("mandateId"):
            raise ProviderRejected("provider_rejected", "Remita did not set up the mandate. Check the payer's details and the school's Remita account.")
        ref = str(answer["mandateId"])
        return ProviderMandate(
            provider_ref=ref, status=MandateStatus.PENDING_ACTIVATION, provider_status=str(answer.get("status") or "")[:120], provider_status_code="040",
            mandate_code=ref, start_date=request.start_date, end_date=request.end_date,
            activation={"methods": ["otp", "form"], "form": self._form_url(secret, environment, ref, request.request_ref)},
        )

    def _form_url(self, secret: dict, environment: str, mandate_ref: str, request_ref: str) -> str:
        """Where the payer prints the mandate form (Remita's PrintMandate address), when SchoolOS knows the host for this environment."""
        base = _form_base(environment)
        if not base:
            return ""
        return f"{base}/mandate/form/{secret['merchant_id']}/{_sha512(secret['merchant_id'], secret['api_key'], request_ref)}/{mandate_ref}/{request_ref}/rest.reg"

    def get_mandate_status(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        request_ref = hints.get("request_ref") or _now_ref()
        answer = _post(environment, MANDATE + "/status", {
            "merchantId": secret["merchant_id"], "mandateId": mandate_ref, "requestId": request_ref,
            "hash": _sha512(mandate_ref, secret["merchant_id"], request_ref, secret["api_key"]),
        })
        _check_auth(answer)
        if _code(answer) == "074":
            raise ProviderRejected("mandate_not_found", "Remita has no record of this mandate.")
        return _mandate_from(mandate_ref, answer)

    def request_activation(self, secret, *, mandate_ref, environment, settings, **hints) -> ActivationChallenge:
        request_ref = hints.get("request_ref") or _now_ref()
        answer = _post(environment, MANDATE + "/requestAuthorization", {"mandateId": mandate_ref, "requestId": request_ref}, headers=_headers(secret))
        _check_auth(answer)
        params = answer.get("authParams") or []
        if _code(answer) != "00" or not answer.get("remitaTransRef") or not params or not isinstance(params[0], dict):
            raise ProviderRejected("activation_unavailable", "The payer's bank could not start the activation. They can use the mandate form instead.")
        first, fields, i = params[0], [], 1
        while first.get(f"param{i}"):
            fields.append({"name": str(first[f"param{i}"]), "label": str(first.get(f"label{i}") or ""), "description": str(first.get(f"description{i}") or "")})
            i += 1
        return ActivationChallenge(challenge_ref=str(answer["remitaTransRef"]), fields=tuple(fields))

    def confirm_activation(self, secret, *, mandate_ref, challenge_ref, answers, environment, settings, **hints) -> ProviderMandate:
        pairs = []
        for i, (name, value) in enumerate(answers.items(), start=1):
            pairs.append({f"param{i}": str(name), "value": str(value)})
        answer = _post(environment, MANDATE + "/validateAuthorization", {"remitaTransRef": challenge_ref, "authParams": pairs}, headers=_headers(secret))
        _check_auth(answer)
        if _code(answer) != "00":
            raise ProviderRejected("activation_refused", "The bank did not accept what was entered. Check the one-time password and try again.")
        # Activated: whether it can now be debited is what Remita says next, so ask.
        return self.get_mandate_status(secret, mandate_ref=mandate_ref, environment=environment, settings=settings, **hints)

    def cancel_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        request_ref = hints.get("request_ref") or _now_ref()
        answer = _post(environment, MANDATE + "/stop", {
            "merchantId": secret["merchant_id"], "mandateId": mandate_ref, "requestId": request_ref,
            "hash": _sha512(mandate_ref, secret["merchant_id"], request_ref, secret["api_key"]),
        })
        _check_auth(answer)
        if _code(answer) != "00":
            raise ProviderRejected("provider_rejected", "Remita did not stop the mandate.")
        return ProviderMandate(provider_ref=mandate_ref, status=MandateStatus.CANCELLED, provider_status=str(answer.get("status") or "")[:120], provider_status_code="00")

    # -- a debit --------------------------------------------------------------------------------

    def create_debit(self, secret, request: DebitRequest, *, environment, settings) -> DebitResult:
        amount = _naira(request.amount_minor)
        answer = _post(environment, MANDATE + "/payment/send", {
            "merchantId": secret["merchant_id"], "serviceTypeId": secret["service_type_id"], "requestId": request.request_ref,
            "hash": _sha512(secret["merchant_id"], secret["service_type_id"], request.request_ref, amount, secret["api_key"]),
            "totalAmount": amount, "mandateId": request.mandate_ref, "fundingAccount": request.funding_account,
            "fundingBankCode": request.funding_bank_code,
        })
        if _code(answer) == "020":
            raise BadCredentials()
        if _code(answer) == "013":
            raise ProviderRejected("invalid_request", "Remita did not accept the debit request.")
        return _debit_from(answer, default_amount=request.amount_minor)

    def get_debit_status(self, secret, *, mandate_ref, request_ref, environment, settings, **hints) -> DebitResult:
        answer = _post(environment, MANDATE + "/payment/status", {
            "merchantId": secret["merchant_id"], "mandateId": mandate_ref, "requestId": request_ref,
            "hash": _sha512(mandate_ref, secret["merchant_id"], request_ref, secret["api_key"]),
        })
        _check_auth(answer)
        return _debit_from(answer)

    # -- callbacks ------------------------------------------------------------------------------

    def handle_webhook(self, *, raw_body, headers, secret, environment, settings) -> WebhookOutcome:
        """Remita documents no signature on its notifications, so this returns only what to look up; the caller asks Remita and its answer decides."""
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ConnectorError("unreadable", "The notification could not be read.") from None
        if not isinstance(body, dict) or str(body.get("notificationType") or "").upper() not in ("ACTIVATION", "DEBIT"):
            return WebhookOutcome(ignored=True)
        kind = "mandate" if str(body["notificationType"]).upper() == "ACTIVATION" else "debit"
        events = []
        for item in list(body.get("lineItems") or [])[:MAX_NOTIFICATION_ITEMS]:
            if isinstance(item, dict) and item.get("mandateId"):
                events.append(ProviderEvent(kind=kind, mandate_ref=str(item["mandateId"]), request_ref=str(item.get("requestId") or "")))
        return WebhookOutcome(events=events, ignored=not events)

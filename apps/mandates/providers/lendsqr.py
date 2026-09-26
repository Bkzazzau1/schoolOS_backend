"""Lendsqr Direct Debit, through the Adjutor Developer API, with the SCHOOL'S OWN Lendsqr API key.

Written only against Lendsqr's published documentation: the Developer APIs pages at docs.lendsqr.com (Getting started, Authentication, Test and Live
mode, Direct Debit APIs) and the Adjutor API reference they point to (api.adjutor.io, the published "Adjutor API Service" collection). What it says,
and what SchoolOS therefore does:

* every call is `https://adjutor.lendsqr.com/v2/...` with `Authorization: Bearer <the app's API key>`. Test and Live mode use the SAME key and
  base URL: the mode is a toggle on the app in the Lendsqr admin console, so SchoolOS cannot see which one a key is in;
* `GET /direct-debit/banks` lists the banks a mandate can be made on, each with its activation amount and the NIBSS account the payer transfers
  it to (in the response's `meta`);
* `POST /customers/direct-debit-mandates` makes a mandate for a Lendsqr CUSTOMER (`user_id`), a bank account and a maximum amount that can be
  debited at one time. **The payer therefore has to exist as a customer in the school's own Lendsqr organisation first**: creating one needs
  BVN, date of birth, address and documents (lender KYC), which SchoolOS neither collects nor sends. The payer's Lendsqr customer id is
  recorded against the payer, and without it no mandate is made;
* the payer activates it by transferring the activation amount (documented: 168 hours, after which it is cancelled) to the bank's NIBSS account;
  the mandate is then activated but "isn't yet available for debit": NIBSS's own setup follows and takes "up to 2 hours". SchoolOS therefore keeps
  ACTIVATED and DEBIT-READY apart: the window and the setup time are Lendsqr's rules and live in configuration, never in SchoolOS's business code,
  and the provider's own dates are used whenever it gives them;
* `GET /customers/:id/direct-debit-mandates` is how a mandate's status is asked for; `PATCH .../activate`, `.../deactivate` and `.../cancel`
  change it (deactivate is a temporary stop and only an active mandate can be deactivated).

**What Lendsqr's published API does NOT document: sending a debit instruction on a mandate, or asking about one.** Lendsqr's help pages show a
"Trigger debit" action in the admin console, and its API reference has "Repay customer loan" (which debits against a loan booked in Lendsqr's own
lending ledger). Neither is a way for SchoolOS to send an approved school-fee debit without creating a second ledger. So `create_debit` and
`get_debit_status` are refused with `pending_documentation` and nothing is guessed: a Lendsqr mandate can be made, activated and watched, but SchoolOS
does not debit it until Lendsqr documents how. Lendsqr also documents no callback events, so none are accepted.

Lendsqr states that live use needs the organisation to be licensed as a lender (or otherwise legally entitled) and to have completed its KYC. SchoolOS
never claims a school is eligible: a live connection is refused until an operator has switched `MANDATES_LENDSQR_LIVE_ENABLED` on.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from django.conf import settings as django_settings

from apps.bankconnect.providers.transport import get_transport

from ..constants import MandateStatus
from .base import (
    ENV_LIVE,
    ENV_TEST,
    BadCredentials,
    BankInfo,
    CreateMandateRequest,
    CredentialField,
    MandateCapabilities,
    MandateConnector,
    MandateProviderInfo,
    MerchantProfile,
    PendingDocumentation,
    ProviderMandate,
    ProviderRejected,
    ProviderUnavailable,
    ValidatedConnection,
)

BASE = "https://adjutor.lendsqr.com/v2"
ACTIVATION_TRANSFER_NOTE = "Transfer the activation amount from the account the mandate is on to the account below. It is activated as soon as it is received."


def _setting(name: str, default: int) -> int:
    return int(getattr(django_settings, name, default) or default)


def _call(secret: dict, environment: str, method: str, path: str, *, body: dict | None = None) -> dict:
    if environment == ENV_LIVE and not getattr(django_settings, "MANDATES_LENDSQR_LIVE_ENABLED", False):
        raise ProviderRejected(
            "live_not_enabled",
            "Live Lendsqr mandates are not switched on for this SchoolOS server. Lendsqr needs to confirm that your organisation is licensed as a lender "
            "or otherwise entitled to use it. Use test mode until then.",
        )
    result = get_transport().request(method, BASE + path, headers={"Authorization": f"Bearer {secret['api_key']}"}, json_body=body)
    if result.status in (401, 403):
        raise BadCredentials()
    if result.status >= 500:
        raise ProviderUnavailable()
    data = result.data if isinstance(result.data, dict) else None
    if not result.ok or data is None or str(data.get("status") or "").lower() not in ("success", ""):
        if data is None:
            raise ProviderUnavailable("Lendsqr's answer could not be read. Try again in a moment.")
        raise ProviderRejected("provider_rejected", "Lendsqr did not accept the request. Check the payer's details and the school's Lendsqr account.")
    return data


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _minor(value) -> int | None:
    try:
        whole, _, frac = str(value).strip().partition(".")
        return int(whole) * 100 + int((frac + "00")[:2])
    except (ValueError, TypeError):
        return None


def _naira(minor: int) -> float:
    return round(int(minor) / 100, 2)


def _mandate_from(m: dict, *, activation: dict | None = None, now: datetime | None = None) -> ProviderMandate:
    now = now or datetime.now(timezone.utc)
    raw = str(m.get("status") or "").strip().lower()
    active_flag = m.get("is_active") in (1, True, "1")
    end = _parse_when(m.get("end_date"))
    activated_at = _parse_when(m.get("activation_date"))
    ready = None
    if raw == "cancelled":
        status = MandateStatus.CANCELLED
    elif end is not None and end.date() < now.date() and raw != "pending_mandate_activation":
        status = MandateStatus.EXPIRED
    elif raw == "pending_mandate_activation":
        status = MandateStatus.PENDING_ACTIVATION
    elif raw == "pending":
        status = MandateStatus.ACTIVATING
    elif raw in ("active", "successful"):
        if not active_flag:
            status = MandateStatus.SUSPENDED
        else:
            basis = activated_at or _parse_when(m.get("last_activity_date"))
            ready = basis + timedelta(minutes=_setting("MANDATES_LENDSQR_DEBIT_SETUP_MINUTES", 120)) if basis else None
            status = MandateStatus.ACTIVE if ready is not None and now >= ready else MandateStatus.PENDING_PROVIDER_SETUP
    elif raw in ("failed", "declined", "rejected"):
        status = MandateStatus.FAILED
    else:
        raise ProviderRejected("provider_unreadable", "Lendsqr's answer about the mandate could not be understood.")
    registered = _parse_when(m.get("registration_date")) or _parse_when(m.get("created_on"))
    deadline = registered + timedelta(hours=_setting("MANDATES_LENDSQR_ACTIVATION_WINDOW_HOURS", 168)) if registered and status == MandateStatus.PENDING_ACTIVATION else None
    return ProviderMandate(
        provider_ref=str(m.get("id")), status=status, provider_status=str(m.get("provider_status") or m.get("status") or "")[:120],
        provider_status_code=raw[:20], mandate_code=str(m.get("mandate_id") or ""), activated_at=activated_at, debit_ready_at=ready,
        activation_deadline=deadline, start_date=_parse_when(m.get("start_date")).date() if _parse_when(m.get("start_date")) else None,
        end_date=end.date() if end else None, is_active=status == MandateStatus.ACTIVE, activation=activation or {},
        meta={"reference": str(m.get("reference") or ""), "mandateType": str(m.get("mandate_type") or ""), "activationType": str(m.get("activation_type") or "")},
    )


class LendsqrMandateConnector(MandateConnector):
    info = MandateProviderInfo(
        code="lendsqr",
        display_name="Lendsqr",
        icon="account_balance",
        environments=(ENV_TEST, ENV_LIVE),
        credential_fields=(CredentialField("api_key", "API key", help="The API key of the app you created under Developer > Apps in your Lendsqr admin console."),),
        capabilities=MandateCapabilities(
            supports_provider_hosted_consent=True, supports_remote_cancellation=True, supports_suspension=True, supports_reactivation=True,
            supports_variable_amount_mandate=True, supports_status_requery=True, supports_transfer_activation=True, requires_provider_customer=True,
        ),
        onboarding=(
            "Create an app under Developer > Apps in your school's own Lendsqr admin console, choose the direct debit scope, and enter its API key. "
            "Each payer must already be a customer in your Lendsqr organisation: enter their Lendsqr customer id when you start their mandate."
        ),
        description="Direct debit mandates activated by the payer's transfer to a NIBSS account. SchoolOS can make, activate and watch them.",
        live_note=(
            "Lendsqr requires the organisation to be licensed as a lender (or otherwise legally entitled) and to have passed its KYC before live use. "
            "Live is switched on by whoever runs this server, after that is confirmed. Test mode is always available."
        ),
        activation_note=(
            "The payer activates the mandate by transferring a small amount (usually N50) from the account to a NIBSS account, within Lendsqr's "
            "activation window. Debits only become possible after the bank has finished setting it up."
        ),
        payer_requirements=(),
        maximum_note="The most that can be debited at one time.",
    )

    # -- connecting -----------------------------------------------------------------------------

    def validate_credentials(self, *, environment, credentials, settings) -> ValidatedConnection:
        secret = {"api_key": str(credentials.get("api_key") or "")}
        self._banks_payload(secret, environment)
        tag = hashlib.sha256(secret["api_key"].encode()).hexdigest()[:4]
        return ValidatedConnection(merchant=MerchantProfile(display_name="Lendsqr app", reference=f"app-{tag}", meta={"environment": environment}), secret=secret)

    def get_merchant_profile(self, secret, *, environment, settings) -> MerchantProfile:
        self._banks_payload(secret, environment)
        tag = hashlib.sha256(secret["api_key"].encode()).hexdigest()[:4]
        return MerchantProfile(display_name="Lendsqr app", reference=f"app-{tag}", meta={"environment": environment})

    def _banks_payload(self, secret: dict, environment: str) -> list:
        body = _call(secret, environment, "GET", "/direct-debit/banks")
        rows = (body.get("data") or {}).get("data") if isinstance(body.get("data"), dict) else body.get("data")
        return rows if isinstance(rows, list) else []

    def list_supported_banks(self, secret, *, environment, settings) -> list[BankInfo]:
        banks = []
        for row in self._banks_payload(secret, environment):
            if not isinstance(row, dict) or not row.get("bank_code"):
                continue
            meta = {}
            try:
                meta = json.loads(row.get("meta") or "{}") if isinstance(row.get("meta"), str) else dict(row.get("meta") or {})
            except ValueError:
                meta = {}
            banks.append(BankInfo(
                code=str(row["bank_code"]), name=str(row.get("name") or ""), activation_amount_minor=_minor(row.get("activation_amount")),
                activation_note=ACTIVATION_TRANSFER_NOTE,
                meta={"activationBank": str(meta.get("mandate-activation-bank") or ""), "activationAccount": str(meta.get("mandate-activation-account-number") or "")},
            ))
        return banks

    # -- a mandate ------------------------------------------------------------------------------

    def _mandates_of(self, secret, environment, customer_ref: str) -> list[dict]:
        body = _call(secret, environment, "GET", f"/customers/{customer_ref}/direct-debit-mandates")
        data = body.get("data")
        rows = data.get("mandates") if isinstance(data, dict) else data
        return [m for m in (rows or []) if isinstance(m, dict)]

    def _activation_for(self, secret, environment, bank_code: str) -> dict:
        try:
            bank = next((b for b in self.list_supported_banks(secret, environment=environment, settings={}) if b.code == bank_code), None)
        except ProviderUnavailable:
            bank = None
        if bank is None:
            return {"method": "transfer", "note": ACTIVATION_TRANSFER_NOTE}
        return {
            "method": "transfer", "note": bank.activation_note, "amountMinor": bank.activation_amount_minor,
            "toBank": bank.meta.get("activationBank", ""), "toAccount": bank.meta.get("activationAccount", ""),
            "windowHours": _setting("MANDATES_LENDSQR_ACTIVATION_WINDOW_HOURS", 168),
        }

    def create_mandate(self, secret, request: CreateMandateRequest, *, environment, settings) -> ProviderMandate:
        customer = request.provider_customer_ref
        if not customer:
            raise ProviderRejected(
                "provider_customer_required",
                "Lendsqr needs the payer's Lendsqr customer id before it can make a mandate. Add it to the payer, then start the mandate.",
            )
        # Lendsqr documents no idempotency key. A mandate an earlier attempt already made on this account is adopted, never duplicated.
        for existing in self._mandates_of(secret, environment, customer):
            if str(existing.get("payer_account") or "") == request.account_number and str(existing.get("payer_bank_code") or "") == request.bank_code:
                if str(existing.get("status") or "").lower() not in ("cancelled", "failed", "declined", "rejected"):
                    return _mandate_from(existing, activation=self._activation_for(secret, environment, request.bank_code))
        body = _call(secret, environment, "POST", "/customers/direct-debit-mandates", body={
            "account_number": request.account_number, "bank_code": request.bank_code, "amount": _naira(request.maximum_amount_minor),
            "start_date": request.start_date.isoformat(), "user_id": customer,
        })
        data = body.get("data")
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderRejected("provider_unreadable", "Lendsqr's answer about the new mandate could not be understood.")
        return _mandate_from(data, activation=self._activation_for(secret, environment, request.bank_code))

    def get_mandate_status(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        customer = hints.get("provider_customer_ref") or ""
        if not customer:
            raise ProviderRejected("provider_customer_required", "Lendsqr is asked about a mandate through the payer's Lendsqr customer id.")
        for m in self._mandates_of(secret, environment, customer):
            if str(m.get("id")) == str(mandate_ref):
                return _mandate_from(m, activation=self._activation_for(secret, environment, hints["bank_code"]) if hints.get("bank_code") else None)
        raise ProviderRejected("mandate_not_found", "Lendsqr has no record of this mandate.")

    def _patch(self, secret, environment, mandate_ref: str, action: str) -> ProviderMandate:
        body = _call(secret, environment, "PATCH", f"/customers/direct-debit-mandates/{mandate_ref}/{action}")
        data = body.get("data")
        mandate = data.get("mandate") if isinstance(data, dict) else None
        if not isinstance(mandate, dict):
            raise ProviderRejected("provider_unreadable", "Lendsqr's answer could not be understood.")
        mandate = {"id": mandate_ref, **mandate}
        return _mandate_from(mandate)

    def cancel_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        return self._patch(secret, environment, mandate_ref, "cancel")

    def suspend_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        return self._patch(secret, environment, mandate_ref, "deactivate")

    def reactivate_mandate(self, secret, *, mandate_ref, environment, settings, **hints) -> ProviderMandate:
        return self._patch(secret, environment, mandate_ref, "activate")

    # -- a debit: not documented as an API call ------------------------------------------------

    def create_debit(self, secret, request, *, environment, settings):
        raise PendingDocumentation("sending a debit instruction on a mandate", "Lendsqr")

    def get_debit_status(self, secret, *, mandate_ref, request_ref, environment, settings, **hints):
        raise PendingDocumentation("asking about a debit on a mandate", "Lendsqr")


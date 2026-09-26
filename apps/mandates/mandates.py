"""A payer's direct-debit mandate: starting one, finding them, whether one is DEBIT-READY, and a family's primary one.

A mandate authorises a payment RAIL. It never says what a family owes: that is the receivables ledger's alone. A staff member starts a mandate
(`finance.mandate_manage`) but can never consent for the payer: the payer authorises it themselves (see consent.py). The payer's bank account
number is sealed the moment it arrives and shown only as a bank and a masked number; it is removed altogether once the provider no longer needs it.

    DRAFT -> PENDING_CONSENT -> PENDING_ACTIVATION -> ACTIVATING -> PENDING_PROVIDER_SETUP -> ACTIVE
       (the provider is asked)      (the payer activates)   (bank/provider set-up)   (debit-ready)
    ... and SUSPENDED (stopped for now), CANCELLED, EXPIRED, FAILED.

Provider calls are never made while a transaction is open (see mandate_provider.py).
"""

from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound

from apps.receivables import periods
from apps.receivables.models import Family, FamilyGuardian

from . import audit, identifiers, permissions
from .connections import open_for_provider
from .constants import (
    ENDED_MANDATE,
    LIVE_MANDATE,
    ConnectionStatus,
    ConsentRoute,
    MandateStatus,
)
from .errors import MandateRefused
from .models import DirectDebitMandate, MandateEvent, MandateProviderConnection
from .providers import registry
from .providers.base import ConnectorError
from .vault import account_context_for, get_vault

ACCOUNT_DIGITS = 10
MAX_MAXIMUM_MINOR = 10**12


def _need_manage(membership) -> None:
    if not permissions.can_manage(membership):
        raise MandateRefused("Only the owner, or someone the owner has authorised, can manage mandates.", "not_mandate_manager")


def record_event(mandate, kind: str, *, actor=None, from_status: str = "", to_status: str = "", **detail) -> MandateEvent:
    from apps.core.scrub import scrub

    return MandateEvent.objects.create(mandate=mandate, kind=kind, actor=actor, from_status=from_status, to_status=to_status, detail=scrub(detail))


# -- finding ------------------------------------------------------------------------------------------


def get_mandate(membership, mandate_id, *, lock: bool = False) -> DirectDebitMandate:
    """Only ever this school's own: another school's mandate answers 404, as if it did not exist."""
    query = DirectDebitMandate.objects.select_related("family", "payer__guardian", "provider_connection", "school").filter(school=membership.school, id=mandate_id)
    mandate = (query.select_for_update(of=("self",)) if lock else query).first()
    if mandate is None:
        raise NotFound("That mandate was not found.")
    return mandate


def list_mandates(membership, *, family=None, status: str = "", provider: str = "", debit_ready: bool | None = None, search: str = "", limit: int = 200):
    rows = DirectDebitMandate.objects.select_related("family", "payer__guardian", "provider_connection").filter(school=membership.school)
    if family is not None:
        rows = rows.filter(family=family)
    if status:
        rows = rows.filter(status=status)
    if provider:
        rows = rows.filter(provider=provider)
    if search:
        rows = rows.filter(family__display_name__icontains=search) | rows.filter(family__code__icontains=search)
    found = list(rows.order_by("-created_at")[:limit])
    if debit_ready is not None:
        found = [m for m in found if is_debit_ready(m)[0] == debit_ready]
    return found


# -- readiness ----------------------------------------------------------------------------------------


def is_debit_ready(mandate, *, today: date | None = None, connection=None) -> tuple[bool, str]:
    """Whether a debit could be sent on this mandate now, and if not, why in words a person can act on. It reads only.

    Debit-ready is stricter than "the payer activated it": the provider must say it can be debited, the payer must have consented, the mandate
    must be inside its dates, and the school's connection to the provider must be working."""
    today = today or periods.school_today()
    if mandate.status != MandateStatus.ACTIVE:
        return False, {
            MandateStatus.DRAFT: "The provider has not been asked to make it yet.",
            MandateStatus.PENDING_CONSENT: "The payer has not authorised it yet.",
            MandateStatus.PENDING_ACTIVATION: "The payer has not activated it yet.",
            MandateStatus.ACTIVATING: "The provider is confirming the activation.",
            MandateStatus.PENDING_PROVIDER_SETUP: "The provider is still setting it up. It can be debited once that is done.",
            MandateStatus.SUSPENDED: "It is suspended.",
            MandateStatus.CANCELLED: "It was cancelled.", MandateStatus.EXPIRED: "It has expired.", MandateStatus.FAILED: "It failed.",
        }.get(mandate.status, "It is not active.")
    if not mandate.consent_at:
        return False, "The payer's consent is not recorded."
    if mandate.start_date and today < mandate.start_date:
        return False, f"It starts on {mandate.start_date.strftime('%d %B %Y')}."
    if mandate.end_date and today > mandate.end_date:
        return False, "It has passed its end date."
    connection = connection or mandate.provider_connection
    if connection.status != ConnectionStatus.CONNECTED:
        return False, "The school's connection to the provider is not working."
    connector = registry.get_connector(mandate.provider)
    if connector is None or not connector.info.capabilities.supports_manual_debit:
        return False, f"{connector.info.display_name if connector else mandate.provider} cannot send a debit through SchoolOS yet."
    return True, ""


# -- starting one -------------------------------------------------------------------------------------


def _clean_account(bank_code, account_number) -> tuple[str, str]:
    bank = identifiers.clean_digits(bank_code)
    if not 3 <= len(bank) <= 12:
        raise MandateRefused("Choose the payer's bank.", "invalid_bank")
    account = identifiers.clean_digits(account_number)
    if len(account) != ACCOUNT_DIGITS or account != str(account_number).strip():
        raise MandateRefused(f"An account number is {ACCOUNT_DIGITS} digits.", "invalid_account_number")
    return bank, account


def _bank_name(connector, secret, connection, bank_code: str) -> str:
    """The bank's name as the provider gives it, and a refusal if the provider does not list the bank at all (where it has a list)."""
    try:
        banks = connector.list_supported_banks(secret, environment=connection.environment, settings=dict(connection.provider_settings or {}))
    except ConnectorError:
        return ""
    if not banks:
        return ""
    match = next((b for b in banks if b.code == bank_code), None)
    if match is None:
        raise MandateRefused(f"{connector.info.display_name} cannot make a mandate on that bank.", "bank_not_supported")
    return match.name[:120]


def _clean_dates(start, end) -> tuple[date, date]:
    today = periods.school_today()
    start = start or today
    end = end or start + timedelta(days=365)
    if not isinstance(start, date) or not isinstance(end, date) or end <= start:
        raise MandateRefused("A mandate needs an end date after its start date.", "invalid_dates")
    return start, end


@sensitive_variables("account_number", "sealed")
def start(
    membership, *, family_id, payer_id, connection_id, bank_code, account_number, maximum_amount_minor, start_date=None, end_date=None,
    max_debits=None, consent_route: str = "", provider_customer_ref: str = "",
) -> DirectDebitMandate:
    """Start a mandate for a payer. Nothing reaches the provider until the payer has authorised it (the app route) - or straight away for a
    payer who will authorise with the provider (the provider route). Staff can never authorise for the payer."""
    _need_manage(membership)
    school = membership.school
    family = Family.objects.filter(school=school, id=family_id).first()
    if family is None:
        raise MandateRefused("That family was not found.", "family_not_found")
    payer = FamilyGuardian.objects.select_related("guardian").filter(school=school, family=family, id=payer_id, is_active=True).first()
    if payer is None:
        raise MandateRefused("Choose one of the family's payers.", "payer_not_found")
    connection = MandateProviderConnection.objects.filter(school=school, id=connection_id).first()
    connector = registry.get_connector(connection.provider) if connection else None
    if connection is None or connector is None:
        raise MandateRefused("That provider is not connected to this school.", "connection_not_found")
    if connection.status != ConnectionStatus.CONNECTED:
        raise MandateRefused("That provider is not working. Test it or replace its credentials first.", "provider_not_connected")
    info = connector.info
    if not (info.capabilities.supports_variable_amount_mandate or info.capabilities.supports_fixed_amount_mandate):
        raise MandateRefused(f"{info.display_name} cannot make a mandate here yet.", "provider_cannot_mandate")

    bank, account = _clean_account(bank_code, account_number)
    if isinstance(maximum_amount_minor, bool) or not isinstance(maximum_amount_minor, int) or not 0 < maximum_amount_minor <= MAX_MAXIMUM_MINOR:
        raise MandateRefused("Say the most that can be debited, in whole kobo.", "invalid_maximum")
    if max_debits is not None and (isinstance(max_debits, bool) or not isinstance(max_debits, int) or not 1 <= max_debits <= 31):
        raise MandateRefused("The most debits allowed is a whole number from 1 to 31.", "invalid_max_debits")
    start_date, end_date = _clean_dates(start_date, end_date)

    missing = [need for need in info.payer_requirements if not _payer_fact(payer, need)]
    if missing:
        raise MandateRefused(f"{info.display_name} needs the payer's {', '.join(missing)} on record first.", "payer_details_missing", missing=missing)
    customer_ref = " ".join(str(provider_customer_ref or "").split())[:60]
    if info.capabilities.requires_provider_customer and not customer_ref:
        customer_ref = _earlier_customer_ref(payer, connection)
    if info.capabilities.requires_provider_customer and not customer_ref:
        raise MandateRefused(f"{info.display_name} needs the payer's {info.display_name} customer id before a mandate can be made.", "provider_customer_required")

    route = consent_route or (ConsentRoute.PAYER_APP if payer.guardian.account_user_id else ConsentRoute.PROVIDER)
    if route not in ConsentRoute.values:
        raise MandateRefused("Choose whether the payer authorises in the app or with the provider.", "invalid_consent_route")
    if route == ConsentRoute.PAYER_APP and not payer.guardian.account_user_id:
        raise MandateRefused("This payer has no signed-in account in the app, so they cannot authorise there. Choose to authorise with the provider.", "payer_has_no_app_account")

    fingerprint = identifiers.account_fingerprint(school.id, bank, account)
    vault = get_vault()  # no key, no storage: refuse before doing anything
    try:
        connector, secret = open_for_provider(connection)
        bank_name = _bank_name(connector, secret, connection, bank)
    except ConnectorError as error:
        raise MandateRefused(error.message, error.code)

    mandate = DirectDebitMandate(
        school=school, family=family, payer=payer, provider_connection=connection, provider=connection.provider, environment=connection.environment,
        provider_customer_ref=customer_ref, bank_code=bank, bank_name=bank_name, account_mask=identifiers.mask_account(account),
        account_fingerprint=fingerprint, maximum_amount_minor=maximum_amount_minor, max_debits=max_debits, start_date=start_date, end_date=end_date,
        consent_route=route, created_by=membership,
        status=MandateStatus.PENDING_CONSENT if route == ConsentRoute.PAYER_APP else MandateStatus.DRAFT, request_ref="pending",
    )
    mandate.request_ref = identifiers.numeric_reference("mandate", mandate.id)
    sealed = vault.seal(account_context_for(mandate), {"account_number": account, "bank_code": bank})
    mandate.sealed_account_details = sealed
    try:
        with transaction.atomic():
            if DirectDebitMandate.objects.filter(school=school, account_fingerprint=fingerprint, status__in=LIVE_MANDATE).exists():
                raise MandateRefused("A mandate already exists on this account. Cancel it before making another.", "mandate_exists")
            if not DirectDebitMandate.objects.filter(family=family, is_primary=True, status__in=LIVE_MANDATE).exists():
                mandate.is_primary = True
            mandate.save()
            record_event(mandate, "started", actor=membership, to_status=mandate.status, provider=mandate.provider, route=route)
            audit.record(school, "mandate_initiated", actor=membership, obj=mandate, family=str(family.id), provider=mandate.provider, route=route)
    except IntegrityError:
        raise MandateRefused("A mandate for this payer could not be started. Try again.", "mandate_exists")
    if route == ConsentRoute.PAYER_APP:
        from . import notifications

        notifications.needs_authorisation(mandate)
    else:
        from . import mandate_provider

        mandate_provider.send_to_provider(mandate.id, actor=membership)
        mandate.refresh_from_db()
    return mandate


def _payer_fact(payer, need: str) -> str:
    guardian = payer.guardian
    return {"name": guardian.name, "email": guardian.email, "phone": guardian.phone}.get(need, "")


def _earlier_customer_ref(payer, connection) -> str:
    previous = DirectDebitMandate.objects.filter(payer=payer, provider_connection=connection).exclude(provider_customer_ref="").order_by("-created_at").first()
    return previous.provider_customer_ref if previous else ""


# -- the primary mandate ------------------------------------------------------------------------------


@transaction.atomic
def set_primary(membership, mandate_id) -> DirectDebitMandate:
    """Choose which of a family's live mandates is its primary one. A family may have several; only one is primary."""
    _need_manage(membership)
    mandate = get_mandate(membership, mandate_id, lock=True)
    if mandate.status in ENDED_MANDATE:
        raise MandateRefused("A cancelled, expired or failed mandate cannot be the primary one.", "mandate_ended")
    if mandate.is_primary:
        return mandate
    DirectDebitMandate.objects.filter(family=mandate.family, is_primary=True).update(is_primary=False)
    mandate.is_primary = True
    mandate.save(update_fields=["is_primary", "updated_at"])
    record_event(mandate, "primary_changed", actor=membership)
    audit.record(mandate.school, "primary_mandate_changed", actor=membership, obj=mandate, family=str(mandate.family_id))
    return mandate


def usable_mandate_for(family) -> DirectDebitMandate | None:
    """The mandate a debit for this family would use: the primary one if it is debit-ready, otherwise another that is (newest first)."""
    candidates = list(DirectDebitMandate.objects.select_related("provider_connection", "payer__guardian").filter(family=family, status=MandateStatus.ACTIVE).order_by("-is_primary", "-created_at"))
    for mandate in candidates:
        if is_debit_ready(mandate)[0]:
            return mandate
    return None

"""The life of a school's mandate-provider connection: connect, test, replace credentials, disable, enable and disconnect. Every change is audited,
and none of them ever returns or logs a credential.

The school onboards with Remita or Lendsqr DIRECTLY and enters the credentials that provider issued to it. SchoolOS verifies them with the provider,
seals them (see vault.py) and keeps only safe facts: the provider, the environment, the merchant's name and a masked identifier, the callback's state
and when it was last verified.

A school may connect BOTH providers. There is no active provider: each mandate records the connection it was made under, so different families can
use different providers. A connection with history is never deleted: disconnecting wipes its credential and revokes it.
"""

from dataclasses import dataclass

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound, PermissionDenied

from . import audit, identifiers
from .constants import LIVE_MANDATE, LIVE_CONNECTION, NOT_CLOSED_CONNECTION, ConnectionStatus, WebhookStatus
from .errors import MandateRefused
from .models import DirectDebitMandate, MandateProviderConnection
from .permissions import can_manage_providers
from .providers import registry
from .providers.base import ConnectorError
from .vault import VaultError, context_for, get_vault

MAX_LABEL = 80
MAX_CREDENTIAL = 512
#: The key, inside the sealed credential, under which the callback address's token is kept so an authorised person can be shown it again.
WEBHOOK_KEY = "webhook_token"


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a test case, whatever its name suggests

    ok: bool
    code: str = ""
    message: str = ""


def _require_manager(membership) -> None:
    """Defence in depth: the views check this too. Only the owner, or someone the owner gave the duty, and only at their own school."""
    if not can_manage_providers(membership):
        raise PermissionDenied("Only the owner, or someone the owner has authorised, can manage the school's mandate providers.")


def get_connection(membership, connection_id, *, lock: bool = False) -> MandateProviderConnection:
    """Only ever this school's own: another school's connection answers 404, as if it did not exist."""
    query = MandateProviderConnection.objects.filter(school=membership.school, id=connection_id)
    connection = (query.select_for_update() if lock else query).first()
    if connection is None:
        raise NotFound("That provider connection was not found.")
    return connection


def list_connections(membership):
    return MandateProviderConnection.objects.filter(school=membership.school).order_by("created_at")


# -- checking what came in --------------------------------------------------------------------------


def _connector(code):
    connector = registry.get_connector(str(code or ""))
    if connector is None or not connector.info.implemented:
        raise MandateRefused("That provider is not available.", "unknown_provider")
    return connector


def _environment(info, value) -> str:
    environment = str(value or "").strip().lower() or info.environments[0]
    if environment not in info.environments:
        raise MandateRefused(f"Choose {' or '.join(info.environments)} for {info.display_name}.", "invalid_environment")
    return environment


def _label(value) -> str:
    label = " ".join(str(value or "").split())
    if len(label) > MAX_LABEL:
        raise MandateRefused(f"A name can be at most {MAX_LABEL} characters.", "invalid_label")
    return label


@sensitive_variables("credentials")
def _clean_credentials(info, credentials) -> dict:
    if not isinstance(credentials, dict):
        raise MandateRefused(f"Enter the credentials {info.display_name} gave your school.", "credentials_required")
    known = {f.name for f in info.credential_fields}
    for name in credentials:
        if name not in known:
            raise MandateRefused(f"'{str(name)[:40]}' is not something {info.display_name} asks for.", "unexpected_field")
    cleaned = {}
    for field in info.credential_fields:
        value = credentials.get(field.name)
        value = value.strip() if isinstance(value, str) else ""
        if field.required and not value:
            raise MandateRefused(f"Enter {field.label}.", "credentials_required")
        if len(value) > MAX_CREDENTIAL:
            raise MandateRefused(f"{field.label} is too long.", "credentials_invalid")
        if value:
            cleaned[field.name] = value
    return cleaned


def _validate_with_provider(membership, connector, environment, credentials, *, action: str):
    try:
        return connector.validate_credentials(environment=environment, credentials=credentials, settings={})
    except ConnectorError as error:
        audit.record(membership.school, f"{action}_failed", actor=membership, object_type="MandateProviderConnection", provider=connector.info.code, code=error.code)
        raise MandateRefused(error.message, error.code)


# -- talking to a provider --------------------------------------------------------------------------


@sensitive_variables("secret")
def open_secret(connection) -> dict:
    """The stored credential, opened, with the connection's id added for connectors that need it. It exists only in the caller's memory."""
    secret = get_vault().open(context_for(connection), connection.sealed_credentials)
    return {**secret, "connection_id": str(connection.id)}


@sensitive_variables("secret")
def open_for_provider(connection):
    """The connector and the connection's opened credential, ready for a provider call. Raises `ConnectorError` or `VaultError`."""
    connector = registry.get_connector(connection.provider)
    if connector is None:
        raise ConnectorError("provider_unavailable", "This provider is not available on this server.")
    return connector, open_secret(connection)


def provider_call_args(connection) -> dict:
    """The environment and non-secret settings every connector call needs."""
    return {"environment": connection.environment, "settings": dict(connection.provider_settings or {})}


def record_failure(connection, code: str) -> None:
    """A provider call failed. A refused credential means the school must replace it; anything else is an error that a later successful call
    clears. A disabled connection stays disabled."""
    if connection.status in LIVE_CONNECTION:
        connection.status = ConnectionStatus.NEEDS_REAUTH if code == "bad_credentials" else ConnectionStatus.ERROR
    connection.last_error_code = code
    connection.save(update_fields=["status", "last_error_code", "updated_at"])


def record_success(connection) -> None:
    if connection.status in (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
        connection.status = ConnectionStatus.CONNECTED
    connection.last_error_code = ""
    connection.last_verified_at = timezone.now()
    connection.save(update_fields=["status", "last_error_code", "last_verified_at", "updated_at"])


# -- lifecycle --------------------------------------------------------------------------------------


@sensitive_variables("credentials", "validated")
def connect(membership, *, provider, environment=None, label=None, credentials=None) -> MandateProviderConnection:
    """Verify the school's own credentials with the provider and keep the connection. Connecting one provider never touches another: there is
    no "active" one to displace."""
    _require_manager(membership)
    connector = _connector(provider)
    info = connector.info
    environment = _environment(info, environment)
    label = _label(label)
    vault = get_vault()  # no key, no storage: fail before contacting the provider
    credentials = _clean_credentials(info, credentials)
    school = membership.school
    if MandateProviderConnection.objects.filter(school=school, provider=info.code).exclude(status=ConnectionStatus.REVOKED).exists():
        raise MandateRefused(
            f"{info.display_name} is already connected to your school. Replace its credentials instead of connecting it again.", "already_connected"
        )
    validated = _validate_with_provider(membership, connector, environment, credentials, action="connect")
    token = identifiers.new_webhook_token()
    connection = MandateProviderConnection(
        school=school, provider=info.code, environment=environment, merchant_name=validated.merchant.display_name,
        merchant_reference=validated.merchant.reference, label=label, status=ConnectionStatus.CONNECTED, provider_settings=validated.settings,
        provider_meta={**validated.meta, **validated.merchant.meta}, is_sandbox=info.is_sandbox, created_by=membership, last_verified_at=timezone.now(),
        webhook_token_hash=identifiers.hash_token(token),
        webhook_status=WebhookStatus.AWAITING_EVENT if info.capabilities.supports_webhooks else WebhookStatus.NOT_CONFIGURED,
    )
    connection.sealed_credentials = vault.seal(context_for(connection), {**validated.secret, WEBHOOK_KEY: token})
    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        raise MandateRefused(f"{info.display_name} is already connected to your school.", "already_connected")
    audit.record(
        school, "provider_connected", actor=membership, obj=connection, provider=info.code, environment=environment, merchant=connection.merchant_reference,
    )
    return connection


def test(membership, connection_id) -> tuple[MandateProviderConnection, TestResult]:
    """Check the stored credential still works with the provider."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED_CONNECTION:
        raise MandateRefused("Only a connected provider can be tested.", "not_live")
    try:
        connector, secret = open_for_provider(connection)
        connector.get_merchant_profile(secret, **provider_call_args(connection))
    except ConnectorError as error:
        record_failure(connection, error.code)
        result = TestResult(False, error.code, error.message)
    except VaultError:
        record_failure(connection, "vault_error")
        result = TestResult(False, "vault_error", "The stored credential could not be opened. Replace the credentials.")
    else:
        record_success(connection)
        result = TestResult(True)
    audit.record(membership.school, "provider_tested", actor=membership, obj=connection, ok=result.ok, code=result.code)
    return connection, result


def rename(membership, connection_id, *, label) -> MandateProviderConnection:
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status == ConnectionStatus.REVOKED:
        raise MandateRefused("A disconnected provider cannot be changed.", "closed")
    connection.label = _label(label)
    connection.save(update_fields=["label", "updated_at"])
    return connection


def live_mandate_count(connection) -> int:
    return DirectDebitMandate.objects.filter(provider_connection=connection, status__in=LIVE_MANDATE).count()


def disable(membership, connection_id) -> MandateProviderConnection:
    """Stop using this provider for now. Only where that is safe: not one whose mandates are still live (SchoolOS would stop hearing about them
    and could not stop or debit them)."""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in LIVE_CONNECTION:
            raise MandateRefused("Only a connected provider can be disabled.", "not_live")
        live = live_mandate_count(connection)
        if live:
            raise MandateRefused(f"{live} mandate{'s are' if live != 1 else ' is'} still live on this provider. Cancel them first.", "has_live_mandates")
        was = connection.status
        connection.status = ConnectionStatus.DISABLED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "provider_disabled", actor=membership, obj=connection, was=was)
    return connection


def enable(membership, connection_id) -> tuple[MandateProviderConnection, TestResult]:
    """Use it again - but only after proving the credential still works."""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status != ConnectionStatus.DISABLED:
            raise MandateRefused("This provider is not disabled.", "not_disabled")
        connection.status = ConnectionStatus.CONNECTED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "provider_enabled", actor=membership, obj=connection)
    return test(membership, connection_id)


@sensitive_variables("credentials", "validated", "secret", "previous")
def replace_credentials(membership, connection_id, *, credentials=None) -> MandateProviderConnection:
    """Swap in new credentials for the same provider connection (after a key was rotated, or one stopped working). Credentials for a different
    merchant are refused: the mandates already made belong to the first one."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED_CONNECTION:
        raise MandateRefused("A disconnected provider cannot be changed. Connect it again instead.", "wrong_status")
    connector = _connector(connection.provider)
    vault = get_vault()
    new = _clean_credentials(connector.info, credentials)
    validated = _validate_with_provider(membership, connector, connection.environment, new, action="credentials_replace")
    if connection.merchant_reference and validated.merchant.reference and connection.merchant_reference != validated.merchant.reference:
        audit.record(membership.school, "credentials_replace_failed", actor=membership, obj=connection, code="different_merchant")
        raise MandateRefused(
            "These credentials are for a different merchant account. To use another account, connect it as a new connection.", "different_merchant"
        )
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in NOT_CLOSED_CONNECTION:
            raise MandateRefused("This provider was changed by someone else. Check it and try again.", "wrong_status")
        previous = vault.open(context_for(connection), connection.sealed_credentials) if connection.sealed_credentials else {}
        token = previous.get(WEBHOOK_KEY) or identifiers.new_webhook_token()
        connection.sealed_credentials = vault.seal(context_for(connection), {**validated.secret, WEBHOOK_KEY: token})
        connection.webhook_token_hash = identifiers.hash_token(token)
        if connection.status in (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
            connection.status = ConnectionStatus.CONNECTED
        connection.last_error_code = ""
        connection.last_verified_at = timezone.now()
        connection.save()
        audit.record(membership.school, "credentials_replaced", actor=membership, obj=connection, provider=connection.provider)
    return connection


def disconnect(membership, connection_id) -> MandateProviderConnection:
    """Wipe the credential and revoke the connection. Refused while any mandate on it is still live. The history stays. (The school revokes its
    own keys in the provider's dashboard: SchoolOS holds none of the provider's side.)"""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status == ConnectionStatus.REVOKED:
            return connection
        live = live_mandate_count(connection)
        if live:
            raise MandateRefused(f"{live} mandate{'s are' if live != 1 else ' is'} still live on this provider. Cancel them first.", "has_live_mandates")
        connection.sealed_credentials = b""
        connection.webhook_token_hash = ""
        connection.webhook_status = WebhookStatus.NOT_CONFIGURED
        connection.last_error_code = ""
        connection.status = ConnectionStatus.REVOKED
        connection.disconnected_at = timezone.now()
        connection.save()
        audit.record(membership.school, "provider_disconnected", actor=membership, obj=connection, provider=connection.provider)
    return connection


# -- the callback ------------------------------------------------------------------------------------


def public_path(connection, token: str) -> str:
    return f"mandate-webhooks/{connection.provider}/{token}/"


def public_url(path: str) -> str:
    base = str(getattr(django_settings, "SCHOOLOS_PUBLIC_API_URL", "") or "").rstrip("/")
    return f"{base}/{path}" if base else ""


def webhook_setup(membership, connection_id) -> dict:
    """What the school must do at the provider, and the address to give it. Shown to whoever manages providers; never with a credential."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    info = _connector(connection.provider).info
    if connection.status not in NOT_CLOSED_CONNECTION:
        raise MandateRefused("A disconnected provider has no callback.", "closed")
    if not info.capabilities.supports_webhooks:
        raise MandateRefused(f"{info.display_name} does not send callbacks. SchoolOS asks it instead.", "no_webhooks")
    try:
        token = open_secret(connection).get(WEBHOOK_KEY, "")
    except VaultError:
        raise MandateRefused("The stored credential could not be opened. Replace the credentials.", "vault_error")
    path = public_path(connection, token)
    return {
        "path": path, "url": public_url(path), "status": connection.webhook_status,
        "confirmedAt": connection.webhook_confirmed_at.isoformat() if connection.webhook_confirmed_at else None,
        "mode": info.webhook.mode, "where": info.webhook.where, "verification": info.webhook.verification,
        "events": list(info.webhook.events), "note": info.webhook.note,
    }


def issue_webhook_token(membership, connection_id) -> str:
    """A new address for the provider to call. The old one stops working; the callback is awaiting its first event again."""
    _require_manager(membership)
    vault = get_vault()
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        connector = registry.get_connector(connection.provider)
        if connection.status not in NOT_CLOSED_CONNECTION or connector is None or not connector.info.capabilities.supports_webhooks:
            raise MandateRefused("This provider cannot send callbacks.", "no_webhooks")
        secret = vault.open(context_for(connection), connection.sealed_credentials)
        token = identifiers.new_webhook_token()
        connection.sealed_credentials = vault.seal(context_for(connection), {**secret, WEBHOOK_KEY: token})
        connection.webhook_token_hash = identifiers.hash_token(token)
        connection.webhook_status = WebhookStatus.AWAITING_EVENT
        connection.webhook_confirmed_at = None
        connection.save(update_fields=["sealed_credentials", "webhook_token_hash", "webhook_status", "webhook_confirmed_at", "updated_at"])
        audit.record(membership.school, "webhook_address_renewed", actor=membership, obj=connection)
    return token


def confirm_webhook(connection) -> None:
    """A verified event reached the address: only now is the callback active."""
    if connection.webhook_status != WebhookStatus.ACTIVE:
        connection.webhook_status = WebhookStatus.ACTIVE
        connection.webhook_confirmed_at = timezone.now()
        connection.save(update_fields=["webhook_status", "webhook_confirmed_at", "updated_at"])
        audit.record(connection.school, "webhook_confirmed", obj=connection)

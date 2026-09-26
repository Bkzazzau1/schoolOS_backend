"""The life of a school's collection-provider connection: connect, test, replace credentials, disable, enable, disconnect and choose
the active provider. Every change is audited, and none of them ever returns or logs a credential.

The school onboards with Paystack, Monnify or Remita DIRECTLY and enters the credentials that provider issued to it. SchoolOS verifies
them with the provider, seals them (see vault.py) and keeps only safe facts: the provider, the environment, the merchant's name and a
masked identifier, the webhook's state and when it was last verified. It never asks for, or needs, the school's settlement bank account.

A school may connect several providers but has exactly ONE active collection provider at a time. It is chosen once here
(`activate`), and changed only through a provider switch (see apps.smartcollect.switching), which is reviewed and approved.
A connection with history is never deleted: disconnecting wipes its credential and revokes it.
"""

from dataclasses import dataclass

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound, PermissionDenied

from apps.core.errors import Rejected

from . import audit, identifiers
from .constants import ConnectionStatus, WebhookStatus
from .models import CollectionProviderConnection
from .permissions import can_manage_providers
from .providers import registry
from .providers.base import ConnectorError
from .vault import VaultError, context_for, get_vault

MAX_LABEL = 80
MAX_CREDENTIAL = 512

#: A connection still in use (its credential may be asked to work).
LIVE = (ConnectionStatus.CONNECTED, ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR)
NOT_CLOSED = LIVE + (ConnectionStatus.DISABLED,)
#: The key, inside the sealed credential, under which the webhook address's token is kept so an authorised person can be shown it again.
WEBHOOK_KEY = "webhook_token"


class CollectionRejected(Rejected):
    """A refusal the app can show, with a short stable `code`."""

    def __init__(self, message: str, code: str = "collection_error"):
        super().__init__(message)
        self.code = code
        self.message = message


#: The name it went by before Smart Money Collection.
BankRejected = CollectionRejected


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a test case, whatever its name suggests

    ok: bool
    code: str = ""
    message: str = ""


# -- who may ----------------------------------------------------------------------------------------


def _require_manager(membership) -> None:
    """Defence in depth: the views check this too. Only the owner, or someone the owner gave the duty, and only at their own school."""
    if not can_manage_providers(membership):
        raise PermissionDenied("Only the owner, or someone the owner has authorised, can manage the school's collection providers.")


# -- finding ----------------------------------------------------------------------------------------


def get_connection(membership, connection_id, *, lock: bool = False) -> CollectionProviderConnection:
    """Only ever this school's own: another school's connection answers 404, as if it did not exist."""
    query = CollectionProviderConnection.objects.filter(school=membership.school, id=connection_id)
    connection = (query.select_for_update() if lock else query).first()
    if connection is None:
        raise NotFound("That provider connection was not found.")
    return connection


def list_connections(membership):
    return CollectionProviderConnection.objects.filter(school=membership.school).order_by("created_at")


def active_provider(school) -> CollectionProviderConnection | None:
    return CollectionProviderConnection.objects.filter(school=school, is_active_provider=True).first()


# -- checking what came in --------------------------------------------------------------------------


def _connector(code):
    connector = registry.get_connector(str(code or ""))
    if connector is None or not connector.info.implemented:
        raise CollectionRejected("That provider is not available.", "unknown_provider")
    return connector


def _environment(info, value) -> str:
    environment = str(value or "").strip().lower() or info.environments[0]
    if environment not in info.environments:
        raise CollectionRejected(f"Choose {' or '.join(info.environments)} for {info.display_name}.", "invalid_environment")
    return environment


def _label(value) -> str:
    label = " ".join(str(value or "").split())
    if len(label) > MAX_LABEL:
        raise CollectionRejected(f"A name can be at most {MAX_LABEL} characters.", "invalid_label")
    return label


@sensitive_variables("credentials")
def _clean_credentials(info, credentials) -> dict:
    if not isinstance(credentials, dict):
        raise CollectionRejected(f"Enter the credentials {info.display_name} gave your school.", "credentials_required")
    known = {f.name for f in info.credential_fields}
    for name in credentials:
        if name not in known:
            raise CollectionRejected(f"'{str(name)[:40]}' is not something {info.display_name} asks for.", "unexpected_field")
    cleaned = {}
    for field in info.credential_fields:
        value = credentials.get(field.name)
        value = value.strip() if isinstance(value, str) else ""
        if field.required and not value:
            raise CollectionRejected(f"Enter {field.label}.", "credentials_required")
        if len(value) > MAX_CREDENTIAL:
            raise CollectionRejected(f"{field.label} is too long.", "credentials_invalid")
        if value:
            cleaned[field.name] = value
    return cleaned


def _clean_settings(info, values) -> dict:
    values = values if isinstance(values, dict) else {}
    known = {f.name: f for f in info.setting_fields}
    for name in values:
        if name not in known:
            raise CollectionRejected(f"'{str(name)[:40]}' is not a setting {info.display_name} has.", "unexpected_field")
    cleaned = {}
    for name, field in known.items():
        value = str(values.get(name) or "").strip()
        if value and field.choices and value not in field.choices:
            raise CollectionRejected(f"Choose one of: {', '.join(field.choices)}.", "invalid_setting")
        if value:
            cleaned[name] = value
    return cleaned


def _validate_with_provider(membership, connector, environment, credentials, settings_in, *, action: str):
    try:
        return connector.validate_credentials(environment=environment, credentials=credentials, settings=settings_in)
    except ConnectorError as error:
        audit.record(membership.school, f"{action}_failed", actor=membership, provider=connector.info.code, code=error.code)
        raise CollectionRejected(error.message, error.code)


# -- talking to a provider --------------------------------------------------------------------------


@sensitive_variables("secret")
def open_secret(connection) -> dict:
    """The stored credential, opened, with the connection's id added for connectors that need it. It exists only in the caller's
    memory. Raises `VaultError`."""
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
    """A provider call failed. A refused credential means the school must replace it; anything else is an error that a later successful
    call clears. A disabled connection stays disabled."""
    if connection.status in LIVE:
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
def connect(membership, *, provider, environment=None, label=None, credentials=None, settings=None) -> CollectionProviderConnection:
    """Verify the school's own credentials with the provider and keep the connection. It is not yet the active provider."""
    _require_manager(membership)
    connector = _connector(provider)
    info = connector.info
    environment = _environment(info, environment)
    label = _label(label)
    vault = get_vault()  # no key, no storage: fail before contacting the provider
    credentials = _clean_credentials(info, credentials)
    settings_in = _clean_settings(info, settings)
    school = membership.school
    if CollectionProviderConnection.objects.filter(school=school, provider=info.code).exclude(status=ConnectionStatus.REVOKED).exists():
        raise CollectionRejected(
            f"{info.display_name} is already connected to your school. Replace its credentials instead of connecting it again.", "already_connected"
        )

    validated = _validate_with_provider(membership, connector, environment, credentials, settings_in, action="connect")
    token = identifiers.new_webhook_token()
    connection = CollectionProviderConnection(
        school=school, provider=info.code, connection_type=info.connection_type, environment=environment,
        merchant_name=validated.merchant.display_name, merchant_reference=validated.merchant.reference, label=label,
        status=ConnectionStatus.CONNECTED, provider_settings=validated.settings, provider_meta={**validated.meta, **validated.merchant.meta},
        token_expires_at=validated.token_expires_at, is_sandbox=info.is_sandbox, created_by=membership, last_verified_at=timezone.now(),
        webhook_token_hash=identifiers.hash_token(token), webhook_status=WebhookStatus.AWAITING_EVENT,
    )
    connection.sealed_credentials = vault.seal(context_for(connection), {**validated.secret, WEBHOOK_KEY: token})
    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        raise CollectionRejected(f"{info.display_name} is already connected to your school.", "already_connected")
    audit.record(
        school, "provider_connected", actor=membership, connection=connection, provider=info.code, environment=environment,
        merchant=connection.merchant_reference,
    )
    return connection


def test(membership, connection_id) -> tuple[CollectionProviderConnection, TestResult]:
    """Check the stored credential still works with the provider."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED:
        raise CollectionRejected("Only a connected provider can be tested.", "not_live")
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
    audit.record(
        membership.school, "provider_test_passed" if result.ok else "provider_test_failed", actor=membership, connection=connection, code=result.code
    )
    return connection, result


def rename(membership, connection_id, *, label) -> CollectionProviderConnection:
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status == ConnectionStatus.REVOKED:
        raise CollectionRejected("A disconnected provider cannot be changed.", "closed")
    before = connection.label
    connection.label = _label(label)
    connection.save(update_fields=["label", "updated_at"])
    audit.record(membership.school, "provider_renamed", actor=membership, connection=connection, before=before, after=connection.label)
    return connection


def _live_accounts(connection) -> int:
    from apps.receivables.models import AccountStatus, FamilyCollectionAccount

    return FamilyCollectionAccount.objects.filter(connection=connection).exclude(status__in=(AccountStatus.CLOSED, AccountStatus.FAILED)).count()


def disable(membership, connection_id) -> CollectionProviderConnection:
    """Stop using this provider for now. Only where that is safe: not the active provider, and not one whose family accounts are still live
    (SchoolOS would stop hearing about the payments made into them)."""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in LIVE:
            raise CollectionRejected("Only a connected provider can be disabled.", "not_live")
        if connection.is_active_provider:
            raise CollectionRejected("This is the active collection provider. Switch to another provider first.", "is_active_provider")
        live = _live_accounts(connection)
        if live:
            raise CollectionRejected(
                f"{live} family collection account{'s are' if live != 1 else ' is'} still live on this provider. Retire them first, so no payment is missed.",
                "has_live_accounts",
            )
        was = connection.status
        connection.status = ConnectionStatus.DISABLED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "provider_disabled", actor=membership, connection=connection, was=was)
    return connection


def enable(membership, connection_id) -> tuple[CollectionProviderConnection, TestResult]:
    """Use it again - but only after proving the credential still works."""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status != ConnectionStatus.DISABLED:
            raise CollectionRejected("This provider is not disabled.", "not_disabled")
        connection.status = ConnectionStatus.CONNECTED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "provider_enabled", actor=membership, connection=connection)
    return test(membership, connection_id)


@sensitive_variables("credentials", "validated", "secret", "previous")
def replace_credentials(membership, connection_id, *, credentials=None, settings=None) -> CollectionProviderConnection:
    """Swap in new credentials for the same provider connection (after a key was rotated, or one stopped working). Credentials for a
    different merchant are refused: family accounts already made belong to the first one."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED:
        raise CollectionRejected("A disconnected provider cannot be changed. Connect it again instead.", "wrong_status")
    connector = _connector(connection.provider)
    info = connector.info
    vault = get_vault()
    new = _clean_credentials(info, credentials)
    settings_in = _clean_settings(info, settings) if settings is not None else dict(connection.provider_settings or {})
    validated = _validate_with_provider(membership, connector, connection.environment, new, settings_in, action="credentials_replace")
    if connection.merchant_reference and validated.merchant.reference and connection.merchant_reference != validated.merchant.reference:
        audit.record(membership.school, "credentials_replace_failed", actor=membership, connection=connection, code="different_merchant")
        raise CollectionRejected(
            "These credentials are for a different merchant account. To use another account, connect it as a new connection.", "different_merchant"
        )
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in NOT_CLOSED:
            raise CollectionRejected("This provider was changed by someone else. Check it and try again.", "wrong_status")
        previous = vault.open(context_for(connection), connection.sealed_credentials) if connection.sealed_credentials else {}
        token = previous.get(WEBHOOK_KEY) or identifiers.new_webhook_token()
        connection.sealed_credentials = vault.seal(context_for(connection), {**validated.secret, WEBHOOK_KEY: token})
        connection.webhook_token_hash = identifiers.hash_token(token)
        connection.provider_settings = validated.settings
        connection.token_expires_at = validated.token_expires_at
        if connection.status in (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
            connection.status = ConnectionStatus.CONNECTED
        connection.last_error_code = ""
        connection.last_verified_at = timezone.now()
        connection.save()
        audit.record(membership.school, "credentials_replaced", actor=membership, connection=connection, provider=connection.provider)
    return connection


def disconnect(membership, connection_id) -> CollectionProviderConnection:
    """Wipe the credential and revoke the connection. Refused while it is the active provider or its family accounts are still live. The
    history stays. (The school revokes its own keys in the provider's dashboard: SchoolOS holds none of the provider's side.)"""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status == ConnectionStatus.REVOKED:
            return connection
        if connection.is_active_provider:
            raise CollectionRejected("This is the active collection provider. Switch to another provider first.", "is_active_provider")
        live = _live_accounts(connection)
        if live:
            raise CollectionRejected(f"{live} family collection account{'s are' if live != 1 else ' is'} still live on this provider. Retire them first.", "has_live_accounts")
        connection.sealed_credentials = b""
        connection.webhook_token_hash = ""
        connection.webhook_status = WebhookStatus.NOT_CONFIGURED
        connection.token_expires_at = None
        connection.last_error_code = ""
        connection.status = ConnectionStatus.REVOKED
        connection.disconnected_at = timezone.now()
        connection.save()
        audit.record(membership.school, "provider_disconnected", actor=membership, connection=connection, provider=connection.provider)
    return connection


# -- the webhook -------------------------------------------------------------------------------------


def public_path(connection, token: str) -> str:
    return f"bank-webhooks/{connection.provider}/{token}/"


def public_url(path: str) -> str:
    base = str(getattr(django_settings, "SCHOOLOS_PUBLIC_API_URL", "") or "").rstrip("/")
    return f"{base}/{path}" if base else ""


def webhook_setup(membership, connection_id) -> dict:
    """What the school must do at the provider, and the address to give it. Shown to whoever manages providers; never with a credential.
    The webhook is not called active until a verified event has actually arrived."""
    _require_manager(membership)
    connection = get_connection(membership, connection_id)
    connector = _connector(connection.provider)
    info = connector.info
    if connection.status not in NOT_CLOSED:
        raise CollectionRejected("A disconnected provider has no webhook.", "closed")
    try:
        token = open_secret(connection).get(WEBHOOK_KEY, "")
    except VaultError:
        raise CollectionRejected("The stored credential could not be opened. Replace the credentials.", "vault_error")
    path = public_path(connection, token)
    return {
        "path": path, "url": public_url(path), "status": connection.webhook_status,
        "confirmedAt": connection.webhook_confirmed_at.isoformat() if connection.webhook_confirmed_at else None,
        "mode": info.webhook.mode, "where": info.webhook.where, "verification": info.webhook.verification,
        "events": list(info.webhook.events), "note": info.webhook.note,
    }


def issue_webhook_token(membership, connection_id) -> str:
    """A new address for the provider to call. The old one stops working; the webhook is awaiting its first event again."""
    _require_manager(membership)
    vault = get_vault()
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        connector = registry.get_connector(connection.provider)
        if connection.status not in NOT_CLOSED or connector is None or not connector.info.capabilities.supports_webhooks:
            raise CollectionRejected("This provider cannot send callbacks.", "no_webhooks")
        secret = vault.open(context_for(connection), connection.sealed_credentials)
        token = identifiers.new_webhook_token()
        connection.sealed_credentials = vault.seal(context_for(connection), {**secret, WEBHOOK_KEY: token})
        connection.webhook_token_hash = identifiers.hash_token(token)
        connection.webhook_status = WebhookStatus.AWAITING_EVENT
        connection.webhook_confirmed_at = None
        connection.save(update_fields=["sealed_credentials", "webhook_token_hash", "webhook_status", "webhook_confirmed_at", "updated_at"])
        audit.record(membership.school, "webhook_address_renewed", actor=membership, connection=connection)
    return token


def confirm_webhook(connection) -> None:
    """A verified event reached the address: only now is the webhook active."""
    if connection.webhook_status != WebhookStatus.ACTIVE:
        connection.webhook_status = WebhookStatus.ACTIVE
        connection.webhook_confirmed_at = timezone.now()
        connection.save(update_fields=["webhook_status", "webhook_confirmed_at", "updated_at"])
        audit.record(connection.school, "webhook_confirmed", connection=connection)


# -- the active provider ------------------------------------------------------------------------------


def set_active(connection: CollectionProviderConnection, *, actor=None, kind: str = "active_provider_set") -> CollectionProviderConnection:
    """Make this the school's ONE active collection provider, atomically: the old one is stood down first, so the database's rule (one
    active per school) is never broken even for an instant. The caller holds the transaction and has checked the right to do this."""
    with transaction.atomic():
        CollectionProviderConnection.objects.filter(school=connection.school, is_active_provider=True).exclude(pk=connection.pk).update(is_active_provider=False)
        connection.is_active_provider = True
        connection.save(update_fields=["is_active_provider", "updated_at"])
        audit.record(connection.school, kind, actor=actor, connection=connection, provider=connection.provider)
    return connection


def activate(membership, connection_id) -> CollectionProviderConnection:
    """Choose the school's first active collection provider. Once one is active, a change is a provider switch (scheduled, reviewed, approved)."""
    _require_manager(membership)
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        current = CollectionProviderConnection.objects.select_for_update().filter(school=membership.school, is_active_provider=True).first()
        if connection.is_active_provider:
            return connection
        if current is not None:
            raise CollectionRejected(
                f"{current.merchant_name or current.provider} is the active collection provider. Change it with a provider switch.", "use_switch"
            )
        if connection.status != ConnectionStatus.CONNECTED:
            raise CollectionRejected("Only a connected provider can be made the active provider.", "not_connected")
        return set_active(connection, actor=membership)

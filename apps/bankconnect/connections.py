"""The life of a connection: connect, confirm, test, rename, disable, enable, rotate, reconnect,
disconnect. Every change is audited, and none of them ever returns or logs a credential.

A new connection is created `pending` once the provider has verified the account and told us who
owns it; it only becomes `connected` when a person confirms that is the account they meant. A
connection with history is never deleted: disconnecting wipes its credential and revokes it.
"""

from dataclasses import dataclass
from urllib.parse import urlparse

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import NotFound

from apps.core.errors import Rejected

from . import audit, identifiers
from .constants import ConnectionStatus, Purpose
from .models import BankConnection
from .providers import registry
from .providers.base import METHOD_AUTHORIZATION, METHOD_CREDENTIALS, ConnectorError
from .vault import VaultError, context_for, get_vault

STATE_SALT = "bankconnect.authorization"
STATE_MAX_AGE_SECONDS = 15 * 60
#: A token this close to expiry is renewed before it is used.
REFRESH_WINDOW_SECONDS = 5 * 60
MAX_LABEL = 80
MAX_CREDENTIAL = 512

LIVE = (ConnectionStatus.CONNECTED, ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR)
NOT_CLOSED = LIVE + (ConnectionStatus.DISABLED,)


class BankRejected(Rejected):
    """A refusal the app can show, with a short stable `code`."""

    def __init__(self, message: str, code: str = "bankconnect_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a test case, whatever its name suggests

    ok: bool
    code: str = ""
    message: str = ""


# -- finding ------------------------------------------------------------------------------------


def get_connection(membership, connection_id, *, lock: bool = False) -> BankConnection:
    """Only ever this school's own: another school's connection answers 404, as if it did not exist."""
    query = BankConnection.objects.filter(school=membership.school, id=connection_id)
    connection = (query.select_for_update() if lock else query).first()
    if connection is None:
        raise NotFound("That bank account was not found.")
    return connection


def list_connections(membership):
    return BankConnection.objects.filter(school=membership.school).order_by("created_at")


# -- checking what came in ----------------------------------------------------------------------


def _implemented(code: str):
    connector = registry.get_connector(str(code or ""))
    if connector is None:
        raise BankRejected("That provider is not available.", "unknown_provider")
    if not connector.info.implemented:
        raise BankRejected(
            f"{connector.info.display_name} cannot be connected yet: SchoolOS has not been given its "
            "verified API documentation.",
            "pending_documentation",
        )
    return connector


def _purpose(value, default=Purpose.GENERAL) -> str:
    if value in (None, ""):
        return default
    if value not in Purpose.values:
        raise BankRejected("Choose what this account collects money for.", "invalid_purpose")
    return value


def _label(value) -> str:
    label = " ".join(str(value or "").split())
    if len(label) > MAX_LABEL:
        raise BankRejected(f"A name can be at most {MAX_LABEL} characters.", "invalid_label")
    return label


def _redirect_uri(value) -> str:
    uri = str(value or "")
    parsed = urlparse(uri)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    allowed = (parsed.scheme == "https" and parsed.netloc) or local or parsed.scheme == "schoolos"
    if not allowed or len(uri) > 500:
        raise BankRejected("The return address for the bank's approval page is not valid.", "invalid_redirect")
    return uri


@sensitive_variables("credentials")
def _clean_credentials(info, credentials) -> dict:
    if not isinstance(credentials, dict):
        raise BankRejected("Enter the credentials the bank gave you.", "credentials_required")
    known = {f.name for f in info.credential_fields}
    for name in credentials:
        if name not in known:
            raise BankRejected(f"'{str(name)[:40]}' is not something {info.display_name} asks for.", "unexpected_field")
    cleaned = {}
    for field in info.credential_fields:
        value = credentials.get(field.name)
        value = value.strip() if isinstance(value, str) else ""
        if field.required and not value:
            raise BankRejected(f"Enter {field.label}.", "credentials_required")
        if len(value) > MAX_CREDENTIAL:
            raise BankRejected(f"{field.label} is too long.", "credentials_invalid")
        if value:
            cleaned[field.name] = value
    return cleaned


def _state_for(membership, provider: str) -> str:
    return signing.dumps({"s": str(membership.school_id), "m": str(membership.id), "p": provider}, salt=STATE_SALT)


def _check_state(state, membership, provider: str) -> None:
    try:
        body = signing.loads(str(state or ""), salt=STATE_SALT, max_age=STATE_MAX_AGE_SECONDS)
    except signing.BadSignature:
        raise BankRejected("The bank's approval has expired or is not valid. Start again.", "bad_state")
    if body != {"s": str(membership.school_id), "m": str(membership.id), "p": provider}:
        raise BankRejected("The bank's approval was started by someone else. Start again.", "bad_state")


# -- talking to a provider ----------------------------------------------------------------------


@sensitive_variables("credentials", "authorization_code", "grant")
def _obtain_grant(membership, connector, *, credentials, authorization_code, state):
    """Ask the provider to verify what the person gave us; the grant is its answer."""
    info = connector.info
    if authorization_code:
        if METHOD_AUTHORIZATION not in info.connect_methods:
            raise BankRejected(f"{info.display_name} connects with credentials, not through its own page.", "wrong_method")
        _check_state(state, membership, info.code)
        attempt = {"authorization_code": str(authorization_code)}
    else:
        if METHOD_CREDENTIALS not in info.connect_methods:
            raise BankRejected(f"{info.display_name} connects by approving access on its own page.", "wrong_method")
        attempt = {"credentials": _clean_credentials(info, credentials)}
    try:
        return connector.connect(**attempt)
    except ConnectorError as error:
        audit.record(membership.school, "connect_failed", actor=membership, provider=info.code, code=error.code)
        raise BankRejected(error.message, error.code)


@sensitive_variables("secret")
def open_secret(connection: BankConnection) -> dict:
    """The stored credential, opened, with the connection's id added for connectors that need it.
    It exists only in the caller's memory. Raises `VaultError`."""
    secret = get_vault().open(context_for(connection), connection.sealed_credentials)
    return {**secret, "connection_id": str(connection.id)}


@sensitive_variables("secret", "renewed")
def open_for_provider(connection: BankConnection):
    """The connector and the connection's opened credential, ready for a provider call, renewing an
    expiring token first. Raises `ConnectorError` or `VaultError`; the credential is never stored
    back unsealed."""
    connector = registry.get_connector(connection.provider)
    if connector is None:
        raise ConnectorError("provider_unavailable", "This provider is not available on this server.")
    vault = get_vault()
    secret = vault.open(context_for(connection), connection.sealed_credentials)
    soon = connection.token_expires_at and (
        (connection.token_expires_at - timezone.now()).total_seconds() < REFRESH_WINDOW_SECONDS
    )
    if soon and connector.info.capabilities.supports_token_refresh:
        renewed, expires = connector.refresh_access_token(secret)
        connection.sealed_credentials = vault.seal(context_for(connection), renewed)
        connection.token_expires_at = expires
        connection.save(update_fields=["sealed_credentials", "token_expires_at", "updated_at"])
        secret = renewed
    return connector, {**secret, "connection_id": str(connection.id)}


def record_failure(connection: BankConnection, code: str) -> None:
    """A provider call failed. A refused credential means the school must reconnect; anything else
    is an error that a later successful call clears. A disabled connection stays disabled."""
    if connection.status in LIVE:
        connection.status = ConnectionStatus.NEEDS_REAUTH if code == "bad_credentials" else ConnectionStatus.ERROR
    connection.last_error_code = code
    connection.save(update_fields=["status", "last_error_code", "updated_at"])


def record_success(connection: BankConnection, *, synced: bool = False) -> None:
    if connection.status in (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
        connection.status = ConnectionStatus.CONNECTED
    connection.last_error_code = ""
    fields = ["status", "last_error_code", "updated_at"]
    if synced:
        connection.last_synced_at = timezone.now()
        fields.append("last_synced_at")
    connection.save(update_fields=fields)


# -- lifecycle ----------------------------------------------------------------------------------


def begin_authorization(membership, *, provider, redirect_uri) -> dict:
    connector = _implemented(provider)
    if METHOD_AUTHORIZATION not in connector.info.connect_methods:
        raise BankRejected(f"{connector.info.display_name} connects with credentials, not through its own page.", "wrong_method")
    state = _state_for(membership, connector.info.code)
    try:
        start = connector.begin_authorization(redirect_uri=_redirect_uri(redirect_uri), state=state)
    except ConnectorError as error:
        raise BankRejected(error.message, error.code)
    return {"authorizationUrl": start.authorization_url, "state": start.state}


@sensitive_variables("credentials", "authorization_code", "grant")
def connect(membership, *, provider, purpose=None, label=None, credentials=None, authorization_code=None, state=None):
    """Verify the account with the provider and keep it `pending` until a person confirms it."""
    connector = _implemented(provider)
    info = connector.info
    purpose, label = _purpose(purpose), _label(label)
    vault = get_vault()  # no key, no storage: fail before contacting the provider
    grant = _obtain_grant(membership, connector, credentials=credentials, authorization_code=authorization_code, state=state)

    school = membership.school
    identity = grant.identity
    fingerprint = identifiers.fingerprint(school.id, info.code, identity.account_number)
    duplicate = BankConnection.objects.filter(school=school, provider=info.code, account_fingerprint=fingerprint)
    if duplicate.exclude(status=ConnectionStatus.REVOKED).exists():
        raise BankRejected("This account is already connected to your school.", "already_connected")

    connection = BankConnection(
        school=school, provider=info.code, connection_type=info.connection_type,
        bank_name=identity.bank_name, account_name=identity.account_name,
        account_mask=identifiers.mask_account(identity.account_number), account_fingerprint=fingerprint,
        purpose=purpose, label=label, status=ConnectionStatus.PENDING,
        token_expires_at=grant.token_expires_at, is_sandbox=info.is_sandbox, created_by=membership,
    )
    connection.sealed_credentials = vault.seal(context_for(connection), grant.secret)
    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        raise BankRejected("This account is already connected to your school.", "already_connected")
    audit.record(
        school, "connection_created", actor=membership, connection=connection,
        provider=info.code, account=connection.account_mask, purpose=purpose,
    )
    return connection


def confirm(membership, connection_id):
    """The person has seen the bank's own name for the account and says it is the right one."""
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status != ConnectionStatus.PENDING:
            raise BankRejected("This account is not waiting to be confirmed.", "not_pending")
        connection.status = ConnectionStatus.CONNECTED
        connection.last_error_code = ""
        token = None
        connector = registry.get_connector(connection.provider)
        if connector is not None and connector.info.capabilities.supports_webhooks:
            token = identifiers.new_webhook_token()
            connection.webhook_token_hash = identifiers.hash_token(token)
        connection.save()
        audit.record(
            membership.school, "connected", actor=membership, connection=connection,
            provider=connection.provider, account=connection.account_mask,
        )
    return connection, token


def test(membership, connection_id) -> tuple[BankConnection, TestResult]:
    """Check the stored credential still works and still reaches the same account."""
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED:
        raise BankRejected("Only a connected account can be tested.", "not_live")
    try:
        connector, secret = open_for_provider(connection)
        identity = connector.test_connection(secret)
        same = identifiers.fingerprint(connection.school_id, connection.provider, identity.account_number)
        if same != connection.account_fingerprint:
            raise ConnectorError("account_changed", "The provider now reports a different account than the one that was connected.")
    except ConnectorError as error:
        record_failure(connection, error.code)
        result = TestResult(False, error.code, error.message)
    except VaultError:
        record_failure(connection, "vault_error")
        result = TestResult(False, "vault_error", "The stored credential could not be opened. Reconnect the account.")
    else:
        record_success(connection)
        result = TestResult(True)
    audit.record(
        membership.school, "test_passed" if result.ok else "test_failed", actor=membership,
        connection=connection, code=result.code,
    )
    return connection, result


def rename(membership, connection_id, *, purpose=None, label=None) -> BankConnection:
    connection = get_connection(membership, connection_id)
    if connection.status not in NOT_CLOSED + (ConnectionStatus.PENDING,):
        raise BankRejected("A disconnected account cannot be changed.", "closed")
    if purpose in (None, "") and label is None:
        raise BankRejected("Say what to change.", "nothing_to_change")
    before = {"purpose": connection.purpose, "label": connection.label}
    connection.purpose = _purpose(purpose, default=connection.purpose)
    if label is not None:
        connection.label = _label(label)
    connection.save(update_fields=["purpose", "label", "updated_at"])
    audit.record(
        membership.school, "renamed", actor=membership, connection=connection,
        before=before, after={"purpose": connection.purpose, "label": connection.label},
    )
    return connection


def disable(membership, connection_id) -> BankConnection:
    """Stop reading this account for now. Its history and credential stay; nothing new is pulled in."""
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in LIVE:
            raise BankRejected("Only a connected account can be disabled.", "not_live")
        was = connection.status
        connection.status = ConnectionStatus.DISABLED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "disabled", actor=membership, connection=connection, was=was)
    return connection


def enable(membership, connection_id) -> tuple[BankConnection, TestResult]:
    """Start reading it again - but only after proving the credential still works."""
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status != ConnectionStatus.DISABLED:
            raise BankRejected("This account is not disabled.", "not_disabled")
        connection.status = ConnectionStatus.CONNECTED
        connection.save(update_fields=["status", "updated_at"])
        audit.record(membership.school, "enabled", actor=membership, connection=connection)
    return test(membership, connection_id)


@sensitive_variables("credentials", "authorization_code", "grant", "secret")
def replace_credentials(membership, connection_id, *, kind, credentials=None, authorization_code=None, state=None):
    """Swap in a new credential for the same account (`rotated`), or restore one that stopped
    working (`reconnected`). A credential for a different account is refused."""
    allowed = NOT_CLOSED if kind == "credentials_rotated" else (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR)
    connection = get_connection(membership, connection_id)
    if connection.status not in allowed:
        raise BankRejected(
            "This account does not need reconnecting." if kind == "reconnected" else "This account cannot be changed.",
            "wrong_status",
        )
    connector = _implemented(connection.provider)
    vault = get_vault()
    grant = _obtain_grant(membership, connector, credentials=credentials, authorization_code=authorization_code, state=state)
    same = identifiers.fingerprint(connection.school_id, connection.provider, grant.identity.account_number)
    if same != connection.account_fingerprint:
        raise BankRejected(
            "These credentials are for a different account. To use another account, connect it as a new one.",
            "different_account",
        )
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status not in allowed:  # changed while the provider was being asked
            raise BankRejected("This account was changed by someone else. Check it and try again.", "wrong_status")
        connection.sealed_credentials = vault.seal(context_for(connection), grant.secret)
        connection.token_expires_at = grant.token_expires_at
        if connection.status in (ConnectionStatus.NEEDS_REAUTH, ConnectionStatus.ERROR):
            connection.status = ConnectionStatus.CONNECTED
        connection.last_error_code = ""
        connection.save()
        audit.record(membership.school, kind, actor=membership, connection=connection, provider=connection.provider)
    return connection


def disconnect(membership, connection_id) -> tuple[BankConnection, bool]:
    """Revoke at the provider where that is possible, then wipe the credential either way. The
    history stays. Returns whether the provider confirmed the revocation."""
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        if connection.status == ConnectionStatus.REVOKED:
            return connection, True
        revoked = False
        try:
            connector, secret = open_for_provider(connection)
            connector.disconnect(secret)
            revoked = True
        except (ConnectorError, VaultError):
            pass  # the school still gets its side closed: the credential is wiped regardless
        connection.sealed_credentials = b""
        connection.webhook_token_hash = ""
        connection.token_expires_at = None
        connection.last_error_code = ""
        connection.status = ConnectionStatus.REVOKED
        connection.disconnected_at = timezone.now()
        connection.save()
        audit.record(
            membership.school, "disconnected", actor=membership, connection=connection,
            provider=connection.provider, account=connection.account_mask, providerRevoked=revoked,
        )
    return connection, revoked


def issue_webhook_token(membership, connection_id) -> str:
    """A new address for the provider to call. The old one stops working; this one is shown once."""
    with transaction.atomic():
        connection = get_connection(membership, connection_id, lock=True)
        connector = registry.get_connector(connection.provider)
        if connection.status not in NOT_CLOSED or connector is None or not connector.info.capabilities.supports_webhooks:
            raise BankRejected("This account cannot receive callbacks from its provider.", "no_webhooks")
        token = identifiers.new_webhook_token()
        connection.webhook_token_hash = identifiers.hash_token(token)
        connection.save(update_fields=["webhook_token_hash", "updated_at"])
        audit.record(membership.school, "webhook_token_issued", actor=membership, connection=connection)
    return token

"""A stand-in provider that makes the whole feature testable without any real bank.

It reads its transactions from `SandboxFeedItem` rows (put there by tests or the
`bankconnect_sandbox_post` command), signs its own webhooks, and can be connected either with a
key or through a two-step authorisation, so every code path a real connector will use is exercised.
Its data is synthetic and is flagged so: it is never counted as the school's money.
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from django.utils.dateparse import parse_datetime

from ..constants import ConnectionType
from .base import (
    METHOD_AUTHORIZATION,
    METHOD_CREDENTIALS,
    STATUS_SANDBOX,
    AccountIdentity,
    AuthorizationStart,
    BadCredentials,
    BankConnector,
    Capabilities,
    ConnectionGrant,
    CredentialField,
    InvalidSignature,
    NormalizedTransaction,
    ProviderInfo,
    SyncPage,
)

ACCESS_LIFETIME = timedelta(hours=1)
SIGNATURE_HEADER = "x-sandbox-signature"


def _identity(account_number: str) -> AccountIdentity:
    return AccountIdentity(
        bank_name="Sandbox Bank",
        account_name="SANDBOX SCHOOL ACCOUNT",
        account_number=account_number,
        external_account_id=f"sandbox-{account_number}",
    )


def _parse(item: dict) -> NormalizedTransaction:
    when = parse_datetime(item.get("transaction_date", "")) or datetime.now(timezone.utc)
    return NormalizedTransaction(
        external_transaction_id=str(item["external_transaction_id"]),
        direction=item.get("direction", "credit"),
        amount_minor=int(item["amount_minor"]),
        transaction_date=when,
        currency=item.get("currency", "NGN"),
        transaction_reference=item.get("transaction_reference", ""),
        provider_session_id=item.get("provider_session_id", ""),
        transaction_type=item.get("transaction_type", "transfer"),
        sender_name=item.get("sender_name", ""),
        sender_account_number=item.get("sender_account_number", ""),
        sender_bank=item.get("sender_bank", ""),
        narration=item.get("narration", ""),
        raw_provider_reference=item.get("raw_provider_reference", ""),
    )


class SandboxConnector(BankConnector):
    info = ProviderInfo(
        code="sandbox",
        display_name="Sandbox (test data)",
        icon="science",
        connection_type=ConnectionType.SANDBOX,
        capabilities=Capabilities(
            supports_transaction_sync=True,
            supports_webhooks=True,
            supports_balance=True,
            supports_historical_transactions=True,
            supports_realtime_transactions=True,
            supports_account_verification=True,
            supports_token_refresh=True,
        ),
        credential_fields=(
            CredentialField("sandbox_key", "Sandbox key"),
            CredentialField("account_number", "Account number (10 digits)", secret=False),
        ),
        connect_methods=(METHOD_CREDENTIALS, METHOD_AUTHORIZATION),
        production_status=STATUS_SANDBOX,
        description="Synthetic transactions for testing. Never counted as the school's money.",
    )

    # -- connecting -----------------------------------------------------------------------------

    def begin_authorization(self, *, redirect_uri: str, state: str) -> AuthorizationStart:
        return AuthorizationStart(authorization_url=f"{redirect_uri}?code=sandbox-approved&state={state}", state=state)

    def connect(self, *, credentials=None, authorization_code=None) -> ConnectionGrant:
        if authorization_code is not None:
            if authorization_code != "sandbox-approved":
                raise BadCredentials()
            account_number = "0123456789"
            secret = {"access_token": secrets.token_urlsafe(24), "account_number": account_number}
            expires = datetime.now(timezone.utc) + ACCESS_LIFETIME
        else:
            credentials = credentials or {}
            key, account_number = credentials.get("sandbox_key", ""), credentials.get("account_number", "")
            if not key.startswith("sandbox-") or not (account_number.isdigit() and len(account_number) == 10):
                raise BadCredentials()
            secret = {"sandbox_key": key, "account_number": account_number}
            expires = None
        secret["webhook_secret"] = secrets.token_urlsafe(24)
        return ConnectionGrant(identity=_identity(account_number), secret=secret, token_expires_at=expires)

    def test_connection(self, secret: dict) -> AccountIdentity:
        self._require_live(secret)
        return _identity(secret["account_number"])

    def refresh_access_token(self, secret: dict):
        if "access_token" not in secret:
            return secret, None
        renewed = {**secret, "access_token": secrets.token_urlsafe(24)}
        return renewed, datetime.now(timezone.utc) + ACCESS_LIFETIME

    def get_account_details(self, secret: dict) -> AccountIdentity:
        return self.test_connection(secret)

    def get_balance(self, secret: dict):
        self._require_live(secret)
        return None

    # -- reading --------------------------------------------------------------------------------

    def fetch_transactions(self, secret: dict, cursor: dict, limit: int = 100) -> SyncPage:
        from ..models import SandboxFeedItem

        self._require_live(secret)
        connection_id = secret.get("connection_id")
        after = int(cursor.get("after", 0))
        rows = list(SandboxFeedItem.objects.filter(connection_id=connection_id, id__gt=after)[: limit + 1])
        page, more = rows[:limit], len(rows) > limit
        next_after = page[-1].id if page else after
        return SyncPage([_parse(r.payload) for r in page], {"after": next_after}, has_more=more)

    def fetch_transaction(self, secret: dict, external_transaction_id: str):
        from ..models import SandboxFeedItem

        for row in SandboxFeedItem.objects.filter(connection_id=secret.get("connection_id")):
            if str(row.payload.get("external_transaction_id")) == external_transaction_id:
                return _parse(row.payload)
        return None

    def verify_transaction(self, secret: dict, external_transaction_id: str) -> bool:
        return self.fetch_transaction(secret, external_transaction_id) is not None

    def handle_webhook(self, *, raw_body: bytes, headers: dict, secret: dict):
        given = headers.get(SIGNATURE_HEADER, "")
        expected = self.sign(raw_body, secret["webhook_secret"])
        if not (given and hmac.compare_digest(given, expected)):
            raise InvalidSignature()
        try:
            body = json.loads(raw_body)
            return [_parse(body["transaction"])]
        except (ValueError, KeyError, TypeError):
            return []

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def sign(raw_body: bytes, webhook_secret: str) -> str:
        return hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()

    @staticmethod
    def _require_live(secret: dict) -> None:
        if not (secret.get("sandbox_key") or secret.get("access_token")):
            raise BadCredentials()

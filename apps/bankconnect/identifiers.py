"""The small, non-reversible things derived from an account: how it is shown, how a duplicate is
recognised, and the token a provider's callback carries. A full account number never leaves the
call that received it."""

import hashlib
import hmac
import secrets

from django.conf import settings


def mask_account(account_number: str) -> str:
    """`0123456789` -> `****6789`. Anything shorter than four digits shows no digits at all."""
    digits = "".join(ch for ch in str(account_number or "") if ch.isalnum())
    return f"****{digits[-4:]}" if len(digits) >= 4 else "****"


def fingerprint(school_id, provider: str, account_number: str) -> str:
    """A keyed one-way fingerprint. The same account gives the same value for one school and
    provider (so it cannot be connected twice) and a different value for another school, so the
    column cannot be used to see that two schools share an account."""
    message = f"{school_id}|{provider}|{account_number}".encode()
    key = hashlib.sha256(f"bankconnect.fingerprint|{settings.SECRET_KEY}".encode()).digest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def new_webhook_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are long and random, so a plain hash is enough to look one up without storing it."""
    return hashlib.sha256(token.encode()).hexdigest()

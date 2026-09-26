"""The small, non-reversible things derived from a payer's bank account: how it is shown and how a duplicate is recognised. A full account
number never leaves the call that received it, except sealed (see vault.py) for the providers that need it again to debit."""

import hashlib
import hmac

from django.conf import settings

from apps.bankconnect.identifiers import hash_token, mask_account, new_webhook_token  # noqa: F401 - shared, provider-neutral

__all__ = ["account_fingerprint", "hash_token", "mask_account", "new_webhook_token", "clean_digits", "numeric_reference"]


def clean_digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def account_fingerprint(school_id, bank_code: str, account_number: str) -> str:
    """A keyed one-way fingerprint. The same account gives the same value for one school (so a duplicate mandate on it is recognised) and a
    different value for another school, so the column cannot show that two schools share a payer."""
    message = f"{school_id}|{bank_code}|{account_number}".encode()
    key = hashlib.sha256(f"mandates.account|{settings.SECRET_KEY}".encode()).digest()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def numeric_reference(*parts) -> str:
    """A 13-digit number derived from what identifies one operation (a mandate, a debit). The same operation always gives the same reference,
    so a retry quotes the reference the first attempt did; and it is numeric because that is what the providers' own examples use."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return str(10**12 + int(digest, 16) % (9 * 10**12))

import hashlib
import secrets


def new_token() -> str:
    """The secret in the link: 256 bits, safe to put in a URL."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """What is stored. The link itself never is."""
    return hashlib.sha256(token.encode()).hexdigest()


def mask_email(email: str) -> str:
    """m***@school.ng: enough to recognise, not enough to learn the address."""
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"

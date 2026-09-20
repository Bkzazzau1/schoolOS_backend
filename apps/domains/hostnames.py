"""What counts as a valid domain for a school, and whose it may be.

A school domain is used to route web requests and to build links that are
emailed to people, so a wrong or hijackable one is a real risk. These rules are
strict on purpose.
"""

import re

from django.conf import settings

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

#: Names a school may not take as its platform subdomain, because they are, or
#: could be mistaken for, the platform's own services.
RESERVED_LABELS = frozenset(
    {
        "www", "api", "admin", "app", "apps", "mail", "smtp", "imap", "pop", "ftp",
        "static", "media", "assets", "cdn", "status", "docs", "dashboard", "login",
        "auth", "account", "accounts", "support", "help", "blog", "web", "ns1", "ns2",
        "staging", "test", "dev", "root", "localhost",
    }
)


class DomainError(Exception):
    """A domain that cannot be used, with a message a person can act on."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def platform_domain() -> str:
    return (getattr(settings, "PLATFORM_DOMAIN", "") or "").strip().lower().strip(".")


def normalize_host(raw: str) -> str:
    """Lower-case, no trailing dot; rejects anything that is not a plain host name.

    Refuses schemes, paths, ports, spaces, underscores, IP addresses, names with
    no dot, and names longer than DNS allows, so what is stored is always safe to
    put in a URL and to compare against a request's Host header.
    """
    host = (raw or "").strip().lower().rstrip(".")
    if not host:
        raise DomainError("Enter a domain.")
    if "://" in host or "/" in host or ":" in host or " " in host or "@" in host:
        raise DomainError("Enter just the domain, like school.example.com, with no https:// or path.")
    if len(host) > 253:
        raise DomainError("That domain is too long.")
    labels = host.split(".")
    if len(labels) < 2:
        raise DomainError("A domain needs at least one dot, like school.example.com.")
    if not all(_LABEL.match(label) for label in labels):
        raise DomainError("That is not a valid domain name.")
    if labels[-1].isdigit():
        raise DomainError("An IP address is not a domain.")
    return host


def platform_host_for(slug: str) -> str:
    """The subdomain every school gets: <slug>.<platform domain>."""
    base = platform_domain()
    if not base:
        raise DomainError("The platform domain is not configured.")
    label = (slug or "").strip().lower()
    if not _LABEL.match(label):
        raise DomainError("The school's short name cannot be used as a web address.")
    if label in RESERVED_LABELS:
        raise DomainError(f"'{label}' is reserved. Choose another short name for the school.")
    return f"{label}.{base}"


def validate_for_kind(host: str, kind: str) -> str:
    """Normalize `host` and check it is allowed for that kind of domain."""
    host = normalize_host(host)
    base = platform_domain()
    under_platform = bool(base) and (host == base or host.endswith("." + base))
    if kind == "platform":
        if not base:
            raise DomainError("The platform domain is not configured.")
        label = host[: -(len(base) + 1)] if under_platform and host != base else ""
        if not under_platform or host == base or "." in label:
            raise DomainError(f"A platform domain must be one name under {base}.")
        if label in RESERVED_LABELS:
            raise DomainError(f"'{label}' is reserved.")
    elif under_platform:
        # A school must not be able to claim the platform's own names, or another
        # school's subdomain, as its "own" domain.
        raise DomainError(f"{host} belongs to the platform. Use a domain you own.")
    return host

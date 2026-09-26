"""Every collection provider SchoolOS supports, in one place: names, what each can do, what it needs to connect. UI code and
business rules ask here instead of naming providers themselves.

Smart Money Collection supports exactly two providers - Paystack and Monnify - each connected with the SCHOOL'S OWN
credentials. Remita is not one of them: it is reserved for a separate Mandates / Direct Debit domain, and never appears here. Conventional banks (GTBank, UBA, Access, ...) and open banking are not collection providers and are not listed. The
sandbox stands in for a provider in development and tests, and is never offered where it is switched off.

Adding a provider is one connector class and one entry below - nothing else in SchoolOS changes.
"""

from django.conf import settings

from ..constants import SMART_PROVIDERS
from .base import CollectionConnector, ProviderInfo
from .monnify import MonnifyConnector
from .paystack import PaystackConnector
from .sandbox import SandboxConnector

_REGISTRY: dict[str, CollectionConnector] = {c.info.code: c for c in (PaystackConnector(), MonnifyConnector())}
_REGISTRY["sandbox"] = SandboxConnector()

assert tuple(code for code in _REGISTRY if code != "sandbox") == tuple(SMART_PROVIDERS)


def sandbox_enabled() -> bool:
    return bool(getattr(settings, "BANKCONNECT_ENABLE_SANDBOX", False))


def get_connector(code: str) -> CollectionConnector | None:
    """The connector for a provider code, or None for anything that is not a supported provider (a legacy bank code, or the sandbox where it is off)."""
    connector = _REGISTRY.get(code)
    if connector is None or (connector.info.is_sandbox and not sandbox_enabled()):
        return None
    return connector


def all_providers() -> list[ProviderInfo]:
    """What a school may connect. The sandbox appears only where it is switched on."""
    return [c.info for c in _REGISTRY.values() if not c.info.is_sandbox or sandbox_enabled()]

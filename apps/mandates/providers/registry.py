"""Every direct-debit provider SchoolOS supports, in one place: names, what each can do, what it needs to connect. UI code and business rules ask
here instead of naming providers themselves.

Mandates & Direct Debit supports exactly two providers - Remita and Lendsqr - each connected with the SCHOOL'S OWN credentials, and a school may
connect both at once. They are NOT Smart Money Collection providers, and Paystack and Monnify are not listed here. The sandbox stands in for a
provider in development and tests, and is never offered where it is switched off.
"""

from django.conf import settings

from ..constants import MANDATE_PROVIDERS
from .base import MandateConnector, MandateProviderInfo
from .lendsqr import LendsqrMandateConnector
from .remita import RemitaMandateConnector
from .sandbox import SandboxMandateConnector

_REGISTRY: dict[str, MandateConnector] = {c.info.code: c for c in (RemitaMandateConnector(), LendsqrMandateConnector())}
_REGISTRY["sandbox"] = SandboxMandateConnector()

assert tuple(code for code in _REGISTRY if code != "sandbox") == tuple(MANDATE_PROVIDERS)


def sandbox_enabled() -> bool:
    return bool(getattr(settings, "MANDATES_ENABLE_SANDBOX", False))


def get_connector(code: str) -> MandateConnector | None:
    """The connector for a provider code, or None for anything that is not a mandate provider (Paystack, Monnify, or the sandbox where it is off)."""
    connector = _REGISTRY.get(code)
    if connector is None or (connector.info.is_sandbox and not sandbox_enabled()):
        return None
    return connector


def all_providers() -> list[MandateProviderInfo]:
    """What a school may connect. The sandbox appears only where it is switched on."""
    return [c.info for c in _REGISTRY.values() if not c.info.is_sandbox or sandbox_enabled()]

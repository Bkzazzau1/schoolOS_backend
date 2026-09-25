"""Every provider SchoolOS knows about, in one place: names, type, capabilities, what it needs to
connect. UI code and business rules ask here instead of naming banks themselves.

Adding a provider is one connector class and one entry below - nothing else in SchoolOS changes.

No real bank or provider API is implemented. The banks are listed so the screen can show them, but
every one is `pending_verified_documentation` with no capabilities and no endpoints: a connector is
written only against the provider's own published documentation, never guessed. The sandbox is the
only connector that does anything.
"""

from django.conf import settings

from ..constants import ConnectionType
from .base import (
    STATUS_PENDING_DOCS,
    Capabilities,
    BankConnector,
    ConnectorError,
    ProviderInfo,
)
from .sandbox import SandboxConnector


class PendingConnector(BankConnector):
    """A provider that is listed but not yet connectable: awaiting its verified documentation."""

    def __init__(self, info: ProviderInfo):
        self.info = info

    def connect(self, *, credentials=None, authorization_code=None):
        raise ConnectorError(
            "pending_documentation",
            f"{self.info.display_name} cannot be connected yet: SchoolOS has not been given its verified API documentation.",
        )

    def begin_authorization(self, *, redirect_uri, state):
        return self.connect()


def _pending(code: str, name: str, kind: str, icon: str, description: str) -> PendingConnector:
    return PendingConnector(
        ProviderInfo(
            code=code,
            display_name=name,
            icon=icon,
            connection_type=kind,
            capabilities=Capabilities(),
            production_status=STATUS_PENDING_DOCS,
            description=description,
        )
    )


_DIRECT = ConnectionType.DIRECT_BANK_API
_pending_connectors = [
    _pending("gtbank", "GTBank", _DIRECT, "bank", "Corporate API credentials issued by the bank."),
    _pending("uba", "UBA", _DIRECT, "bank", "Corporate API credentials issued by the bank."),
    _pending("zenith", "Zenith Bank", _DIRECT, "bank", "Corporate API credentials issued by the bank."),
    _pending("access", "Access Bank", _DIRECT, "bank", "Corporate API credentials issued by the bank."),
    _pending("firstbank", "FirstBank", _DIRECT, "bank", "Corporate API credentials issued by the bank."),
    _pending("moniepoint", "Moniepoint Business", _DIRECT, "bank", "Business account API access."),
    _pending("opay", "OPay Business", _DIRECT, "bank", "Business account API access."),
    _pending(
        "open_banking",
        "Other bank (open banking)",
        ConnectionType.OPEN_BANKING,
        "link",
        "Authorise SchoolOS through an approved open-banking provider. No internet-banking password is held.",
    ),
    _pending(
        "monnify",
        "Monnify",
        ConnectionType.COLLECTION_PROVIDER,
        "payments",
        "Collections processed through Monnify. This is not access to the school's underlying bank account.",
    ),
    _pending(
        "paystack",
        "Paystack",
        ConnectionType.COLLECTION_PROVIDER,
        "payments",
        "Collections processed through the school's own Paystack account. This is not access to its bank account.",
    ),
]

_REGISTRY: dict[str, BankConnector] = {c.info.code: c for c in _pending_connectors}
_REGISTRY["sandbox"] = SandboxConnector()


def sandbox_enabled() -> bool:
    return bool(getattr(settings, "BANKCONNECT_ENABLE_SANDBOX", False))


def get_connector(code: str) -> BankConnector | None:
    connector = _REGISTRY.get(code)
    if connector is None or (connector.info.is_sandbox and not sandbox_enabled()):
        return None
    return connector


def all_providers() -> list[ProviderInfo]:
    """What the connect screen lists. The sandbox appears only where it is switched on."""
    return [c.info for c in _REGISTRY.values() if not c.info.is_sandbox or sandbox_enabled()]

"""Sending a real Principal announcement over a real SMS or email provider - the transport counterpart to
apps/media/transcoding.py and apps/media/scanning.py's own pluggable-and-honestly-absent seams.

Unlike a video decoder or a malware scanner, there is no single standard interface a "real" SMS or email
provider implements: every vendor (Termii, Africa's Talking, Twilio, SendGrid, ...) has its own proprietary
REST contract, so - following the same rule the Smart Money Collection plan already set for a bank connector -
no vendor's API is invented here without real, verified documentation to build it against. What this module
gives is the seam itself: a stable interface a school's chosen provider will one day implement, an override
for tests, and an honest default that refuses rather than pretends to have sent something it never could.
`principal_outgoing_communication`'s own non-portal channels (SMS, email, WhatsApp) stay queued-only, exactly as
before, until a real provider is plugged in here.
"""

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass


class DeliveryError(Exception):
    """Something the provider itself refused or could not do. `code` is a stable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DeliveryUnavailable(DeliveryError):
    """No real provider is configured for this channel on this server."""

    def __init__(self, message: str = "No real delivery provider is configured for this channel."):
        super().__init__("delivery_unavailable", message)


@dataclass(frozen=True)
class DeliveryReceipt:
    """What a real provider hands back once it has really accepted a message for delivery - never a claim
    this server invents itself."""

    provider_reference: str
    accepted_at: str


class SmsProvider(ABC):
    @abstractmethod
    def send(self, *, to: str, body: str) -> DeliveryReceipt:
        """Hands a real message to a real SMS provider. Raises DeliveryUnavailable when no real provider is
        configured on this server; raises DeliveryError for a genuine provider-side failure (a bad number, an
        exhausted sender balance, ...)."""


class EmailProvider(ABC):
    @abstractmethod
    def send(self, *, to: str, subject: str, body: str) -> DeliveryReceipt:
        """Hands a real message to a real email provider. Raises DeliveryUnavailable when no real provider is
        configured on this server; raises DeliveryError for a genuine provider-side failure."""


class UnconfiguredSmsProvider(SmsProvider):
    """The honest default until a school's own real SMS provider (Termii, Africa's Talking, ...) is wired in
    here as its own class, the same way a real bank connector joins apps/bankconnect's own registry only once
    its real API is documented and verified."""

    def send(self, *, to: str, body: str) -> DeliveryReceipt:
        raise DeliveryUnavailable("No real SMS provider is configured on this server yet.")


class UnconfiguredEmailProvider(EmailProvider):
    """The honest default until a school's own real email provider (SendGrid, Postmark, ...) is wired in here
    as its own class."""

    def send(self, *, to: str, subject: str, body: str) -> DeliveryReceipt:
        raise DeliveryUnavailable("No real email provider is configured on this server yet.")


_sms_override: SmsProvider | None = None
_email_override: EmailProvider | None = None


@contextmanager
def use_sms_provider(provider: SmsProvider):
    """Swaps in a fake SMS provider for the duration of a `with` block, so a test can prove the delivery
    machinery end to end without a real provider account - the same shape `apps/media/transcoding.py:
    use_transcoder` already uses."""
    global _sms_override
    previous, _sms_override = _sms_override, provider
    try:
        yield provider
    finally:
        _sms_override = previous


@contextmanager
def use_email_provider(provider: EmailProvider):
    global _email_override
    previous, _email_override = _email_override, provider
    try:
        yield provider
    finally:
        _email_override = previous


def get_sms_provider() -> SmsProvider:
    if _sms_override is not None:
        return _sms_override
    return UnconfiguredSmsProvider()


def get_email_provider() -> EmailProvider:
    if _email_override is not None:
        return _email_override
    return UnconfiguredEmailProvider()

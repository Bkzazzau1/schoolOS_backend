"""Sending a real push notification to a real device - the push counterpart to
apps/administration/communication_delivery.py's own SMS/email seam.

Unlike SMS/email, where every vendor has its own proprietary contract (so
communication_delivery.py refuses to invent one), push has a single real
vendor already chosen for this product: Firebase Cloud Messaging, with one
stable, documented API. `FirebasePushProvider` is built here for real, against
the real `firebase-admin` SDK - it just stays inert, and `get_push_provider()`
keeps handing back the same honest `UnconfiguredPushProvider` every other
unconfigured seam defaults to, until FIREBASE_PROJECT_ID and
FIREBASE_CREDENTIALS_JSON are both set on this server.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass


class DeliveryError(Exception):
    """Something the provider itself refused or could not do. `code` is a stable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DeliveryUnavailable(DeliveryError):
    """No real push provider is configured on this server."""

    def __init__(self, message: str = "No real push provider is configured on this server yet."):
        super().__init__("delivery_unavailable", message)


@dataclass(frozen=True)
class PushReceipt:
    """What a real provider hands back for one token - never a claim this server invents itself."""

    token: str
    sent: bool
    detail: str = ""


class PushProvider(ABC):
    @abstractmethod
    def send(self, *, tokens: Iterable[str], title: str, body: str, data: dict) -> list[PushReceipt]:
        """Hands a real notification to a real push provider for each token. Raises
        DeliveryUnavailable when no real provider is configured on this server; raises
        DeliveryError for a genuine provider-side failure."""


class UnconfiguredPushProvider(PushProvider):
    """The honest default until FIREBASE_PROJECT_ID and FIREBASE_CREDENTIALS_JSON are set."""

    def send(self, *, tokens: Iterable[str], title: str, body: str, data: dict) -> list[PushReceipt]:
        raise DeliveryUnavailable()


class FirebasePushProvider(PushProvider):
    """Delivers through Firebase Cloud Messaging.

    `credentials_json` is the downloaded service-account key, as one line of
    JSON (see FIREBASE_CREDENTIALS_JSON in .env.example). The firebase_admin app
    is created lazily, on first real send, not at import time - so a server
    with these settings unset never even imports the SDK.
    """

    def __init__(self, *, project_id: str, credentials_json: str):
        self._project_id = project_id
        self._credentials_json = credentials_json
        self._app = None

    def _app_instance(self):
        if self._app is None:
            import json

            import firebase_admin
            from firebase_admin import credentials

            cert = credentials.Certificate(json.loads(self._credentials_json))
            self._app = firebase_admin.initialize_app(
                cert, {"projectId": self._project_id}, name="skulgen-push"
            )
        return self._app

    def send(self, *, tokens: Iterable[str], title: str, body: str, data: dict) -> list[PushReceipt]:
        from firebase_admin import messaging

        app = self._app_instance()
        receipts = []
        for token in tokens:
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data={str(k): str(v) for k, v in data.items()},
            )
            try:
                messaging.send(message, app=app)
                receipts.append(PushReceipt(token=token, sent=True))
            except Exception as exc:  # pragma: no cover - only exercised against a real Firebase project
                receipts.append(PushReceipt(token=token, sent=False, detail=str(exc)))
        return receipts


_override: PushProvider | None = None


@contextmanager
def use_push_provider(provider: PushProvider):
    """Swaps in a fake push provider for the duration of a `with` block, the same shape
    communication_delivery.py's own use_sms_provider/use_email_provider already use."""
    global _override
    previous, _override = _override, provider
    try:
        yield provider
    finally:
        _override = previous


def get_push_provider() -> PushProvider:
    if _override is not None:
        return _override
    from django.conf import settings

    project_id = getattr(settings, "FIREBASE_PROJECT_ID", "")
    credentials_json = getattr(settings, "FIREBASE_CREDENTIALS_JSON", "")
    if project_id and credentials_json:
        return FirebasePushProvider(project_id=project_id, credentials_json=credentials_json)
    return UnconfiguredPushProvider()

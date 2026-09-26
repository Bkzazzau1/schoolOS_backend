from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import connections, sync, webhooks
from .http import bank_errors as _bank_errors
from .http import body as _body
from .models import BankAuditEvent
from .permissions import acting_membership, can_manage_connections
from .providers import registry
from .serializers import serialize_audit_event, serialize_connection, serialize_provider
from .vault import VaultError, get_vault

#: Bodies that can carry a credential are kept out of Django's error reports and logs.
_SENSITIVE = method_decorator(
    sensitive_post_parameters("credentials", "authorizationCode", "state"), name="dispatch"
)


def _secure_storage_ready() -> bool:
    try:
        get_vault()
    except VaultError:
        return False
    return True


class ProvidersView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response(
            {
                "providers": [serialize_provider(info) for info in registry.all_providers()],
                "canManage": can_manage_connections(membership),
                "secureStorageReady": _secure_storage_ready(),
            }
        )


@_SENSITIVE
class ConnectionsView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response(
            {
                "connections": [serialize_connection(c) for c in connections.list_connections(membership)],
                "canManage": can_manage_connections(membership),
            }
        )

    @_bank_errors
    def post(self, request, school_id):
        """Verify an account with its provider. It stays `pending` until confirmed."""
        membership = acting_membership(request, school_id, manage=True)
        data = _body(request)
        connection = connections.connect(
            membership,
            provider=data.get("provider"), purpose=data.get("purpose"), label=data.get("label"),
            credentials=data.get("credentials"), authorization_code=data.get("authorizationCode"),
            state=data.get("state"),
        )
        return Response({"connection": serialize_connection(connection)}, status=status.HTTP_201_CREATED)


class AuthorizationStartView(APIView):
    """Where to send the person to approve access on the bank's own page."""

    @_bank_errors
    def post(self, request, school_id):
        membership = acting_membership(request, school_id, manage=True)
        data = _body(request)
        return Response(
            connections.begin_authorization(membership, provider=data.get("provider"), redirect_uri=data.get("redirectUri"))
        )


class ConnectionAuditView(APIView):
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id)
        connection = connections.get_connection(membership, connection_id)
        events = BankAuditEvent.objects.filter(school=membership.school, connection=connection)[:50]
        return Response({"events": [serialize_audit_event(e) for e in events]})


# -- actions on one connection: name -> (membership, connection id, request body) -> response body


def _connection_only(result):
    return {"connection": serialize_connection(result)}


def _with_test(pair):
    connection, result = pair
    return {
        "connection": serialize_connection(connection),
        "test": {"ok": result.ok, "code": result.code, "message": result.message},
    }


def _new_credentials(kind):
    def act(membership, connection_id, data):
        return _connection_only(
            connections.replace_credentials(
                membership, connection_id, kind=kind,
                credentials=data.get("credentials"), authorization_code=data.get("authorizationCode"),
                state=data.get("state"),
            )
        )

    return act


def _confirm(membership, connection_id, data):
    connection, token = connections.confirm(membership, connection_id)
    body = _connection_only(connection)
    if token:
        # Shown this once: only its hash is kept.
        body["webhook"] = {"path": f"bank-webhooks/{connection.provider}/{token}/"}
    return body


def _disconnect(membership, connection_id, data):
    connection, revoked = connections.disconnect(membership, connection_id)
    return {**_connection_only(connection), "providerRevoked": revoked}


def _webhook_token(membership, connection_id, data):
    token = connections.issue_webhook_token(membership, connection_id)
    connection = connections.get_connection(membership, connection_id)
    return {**_connection_only(connection), "webhook": {"path": f"bank-webhooks/{connection.provider}/{token}/"}}


ACTIONS = {
    "confirm": _confirm,
    "test": lambda m, c, d: _with_test(connections.test(m, c)),
    "rename": lambda m, c, d: _connection_only(
        connections.rename(m, c, purpose=d.get("purpose"), label=d.get("label"))
    ),
    "disable": lambda m, c, d: _connection_only(connections.disable(m, c)),
    "enable": lambda m, c, d: _with_test(connections.enable(m, c)),
    "rotate": _new_credentials("credentials_rotated"),
    "reconnect": _new_credentials("reconnected"),
    "disconnect": _disconnect,
    "webhook-token": _webhook_token,
}


@_SENSITIVE
class ConnectionActionView(APIView):
    action = None

    @_bank_errors
    def post(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, manage=True)
        return Response(ACTIONS[self.action](membership, connection_id, _body(request)))


class ConnectionSyncView(APIView):
    """Pull new transactions now. Reading is harmless, so the finance office may do it too."""

    @_bank_errors
    def post(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id)
        connection = connections.get_connection(membership, connection_id)
        outcome = sync.sync_connection(connection, actor=membership)
        connection.refresh_from_db()
        return Response(
            {
                "connection": serialize_connection(connection),
                "sync": {
                    "ok": outcome.ok, "fetched": outcome.fetched, "created": outcome.created,
                    "duplicates": outcome.duplicates, "invalid": outcome.invalid, "more": outcome.more,
                    "code": outcome.error_code, "message": outcome.error_message,
                },
            }
        )


class BankWebhookView(APIView):
    """A provider's callback. Public by necessity: see `webhooks` for how it defends itself."""

    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "bank_webhook"

    def post(self, request, provider, token):
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > webhooks.MAX_BODY_BYTES:
            return Response({"code": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        headers = {name.lower(): value for name, value in request.headers.items()}
        try:
            result = webhooks.receive(provider, token, request.body, headers)
        except webhooks.WebhookRefused as refused:
            return Response({"code": refused.code}, status=refused.status)
        return Response({"received": True, "outcome": result.outcome})

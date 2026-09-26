from functools import wraps

from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import connections
from .models import BankAuditEvent
from .permissions import acting_membership, can_manage_connections
from .providers import registry
from .serializers import serialize_audit_event, serialize_connection, serialize_provider
from .vault import VaultNotConfigured, VaultError, get_vault

#: Bodies that can carry a credential are kept out of Django's error reports and logs.
_SENSITIVE = method_decorator(
    sensitive_post_parameters("credentials", "authorizationCode", "state"), name="dispatch"
)


def _bank_errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except connections.BankRejected as error:
            return Response({"code": error.code, "message": error.message}, status=status.HTTP_400_BAD_REQUEST)
        except VaultNotConfigured:
            return Response(
                {"code": "secure_storage_unavailable",
                 "message": "Secure storage for bank credentials is not set up on this server, so no account can be connected yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except VaultError:
            return Response(
                {"code": "credential_unreadable",
                 "message": "The stored credential could not be opened. Reconnect the account."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    return wrapped


def _body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


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

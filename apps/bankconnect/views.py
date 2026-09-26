from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import provider_connections as connections
from . import webhooks
from .constants import COLLECTION_PROVIDER_CODES
from .http import bank_errors as _bank_errors
from .http import body as _body
from .models import BankAuditEvent
from .permissions import NEED_PROVIDER, acting_membership, permissions_of
from .providers import registry
from .serializers import serialize_audit_event, serialize_connection, serialize_provider
from .vault import VaultError, get_vault

#: Bodies that can carry a credential are kept out of Django's error reports and logs.
_SENSITIVE = method_decorator(sensitive_post_parameters("credentials", "secret", "settings"), name="dispatch")


def _secure_storage_ready() -> bool:
    try:
        get_vault()
    except VaultError:
        return False
    return True


def _listed(membership):
    """This school's provider connections. (Rows made by the earlier bank-account model are history, not part of Smart Money Collection.)"""
    return connections.list_connections(membership).filter(provider__in=COLLECTION_PROVIDER_CODES)


class ProvidersView(APIView):
    """GET the providers a school can connect (Paystack, Monnify), what each needs and can do, and what this person may do."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        active = connections.active_provider(membership.school)
        return Response(
            {
                "providers": [serialize_provider(info) for info in registry.all_providers()],
                "permissions": permissions_of(membership),
                "canManage": permissions_of(membership)["canManageProviders"],
                "secureStorageReady": _secure_storage_ready(),
                "activeConnectionId": str(active.id) if active else None,
            }
        )


@_SENSITIVE
class ConnectionsView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response(
            {
                "connections": [serialize_connection(c) for c in _listed(membership)],
                "canManage": permissions_of(membership)["canManageProviders"],
                "permissions": permissions_of(membership),
            }
        )

    @_bank_errors
    def post(self, request, school_id):
        """Verify the school's own credentials with the provider and connect it. It is not yet the active provider."""
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        data = _body(request)
        connection = connections.connect(
            membership, provider=data.get("provider"), environment=data.get("environment"), label=data.get("label"),
            credentials=data.get("credentials"), settings=data.get("settings"),
        )
        return Response({"connection": serialize_connection(connection)}, status=status.HTTP_201_CREATED)


class ConnectionAuditView(APIView):
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        connection = connections.get_connection(membership, connection_id)
        events = BankAuditEvent.objects.filter(school=membership.school, connection=connection)[:50]
        return Response({"events": [serialize_audit_event(e) for e in events]})


class ConnectionWebhookView(APIView):
    """GET what to do at the provider and the address to give it. For whoever manages providers; never carries a credential."""

    @_bank_errors
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        return Response({"webhook": connections.webhook_setup(membership, connection_id)})


# -- actions on one connection: name -> (membership, connection id, request body) -> response body


def _connection_only(result):
    return {"connection": serialize_connection(result)}


def _with_test(pair):
    connection, result = pair
    return {"connection": serialize_connection(connection), "test": {"ok": result.ok, "code": result.code, "message": result.message}}


def _replace(membership, connection_id, data):
    return _connection_only(
        connections.replace_credentials(membership, connection_id, credentials=data.get("credentials"), settings=data.get("settings"))
    )


def _webhook_token(membership, connection_id, data):
    connections.issue_webhook_token(membership, connection_id)
    return {"webhook": connections.webhook_setup(membership, connection_id)}


ACTIONS = {
    "test": lambda m, c, d: _with_test(connections.test(m, c)),
    "rename": lambda m, c, d: _connection_only(connections.rename(m, c, label=d.get("label"))),
    "disable": lambda m, c, d: _connection_only(connections.disable(m, c)),
    "enable": lambda m, c, d: _with_test(connections.enable(m, c)),
    "replace-credentials": _replace,
    "disconnect": lambda m, c, d: _connection_only(connections.disconnect(m, c)),
    "webhook-token": _webhook_token,
    "activate": lambda m, c, d: _connection_only(connections.activate(m, c)),
}


@_SENSITIVE
class ConnectionActionView(APIView):
    action = None

    @_bank_errors
    def post(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        return Response(ACTIONS[self.action](membership, connection_id, _body(request)))


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

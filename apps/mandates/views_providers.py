"""The school's mandate providers: which exist, connecting them with the school's own credentials, and their public callback."""

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import connections, serializers, webhooks
from .constants import LIVE_MANDATE
from .http import body, mandate_errors
from .models import DirectDebitMandate, MandateAuditEvent
from .permissions import NEED_PROVIDER, NEED_VIEW, acting_membership, permissions_of
from .providers import registry
from .providers.base import ConnectorError
from .vault import VaultError, get_vault

#: Bodies that can carry a credential are kept out of Django's error reports and logs.
_SENSITIVE = method_decorator(sensitive_post_parameters("credentials", "secret", "answers"), name="dispatch")


def _secure_storage_ready() -> bool:
    try:
        get_vault()
    except VaultError:
        return False
    return True


def _counts(connection) -> dict:
    rows = DirectDebitMandate.objects.filter(provider_connection=connection)
    return {
        "active": rows.filter(status="active").count(), "pending": rows.filter(status__in=["draft", "pending_consent", "pending_activation", "activating", "pending_provider_setup"]).count(),
        "live": rows.filter(status__in=LIVE_MANDATE).count(), "total": rows.count(),
    }


class ProvidersView(APIView):
    """GET the providers a school can connect (Remita, Lendsqr), what each needs and can do, and what this person may do. There is no active one."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        return Response({
            "providers": [serializers.provider(info) for info in registry.all_providers()],
            "permissions": permissions_of(membership), "secureStorageReady": _secure_storage_ready(),
        })


@_SENSITIVE
class ConnectionsView(APIView):
    def get(self, request, school_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        return Response({
            "connections": [serializers.connection(c, counts=_counts(c)) for c in connections.list_connections(membership)],
            "permissions": permissions_of(membership),
        })

    @mandate_errors
    def post(self, request, school_id):
        """Verify the school's own credentials with the provider and connect it. Connecting one provider never touches another."""
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        data = body(request)
        connection = connections.connect(
            membership, provider=data.get("provider"), environment=data.get("environment"), label=data.get("label"), credentials=data.get("credentials"),
        )
        return Response({"connection": serializers.connection(connection, counts=_counts(connection))}, status=status.HTTP_201_CREATED)


class ConnectionAuditView(APIView):
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        connection = connections.get_connection(membership, connection_id)
        events = MandateAuditEvent.objects.select_related("actor__user").filter(school=membership.school, object_type="MandateProviderConnection", object_id=str(connection.id))[:50]
        return Response({"events": [serializers.audit_event(e) for e in events]})


class ConnectionWebhookView(APIView):
    """GET what to do at the provider and the address to give it. For whoever manages providers; never carries a credential."""

    @mandate_errors
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        return Response({"webhook": connections.webhook_setup(membership, connection_id)})


class ConnectionBanksView(APIView):
    """GET the banks the provider can make a mandate on (for the form that starts one)."""

    @mandate_errors
    def get(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_VIEW)
        connection = connections.get_connection(membership, connection_id)
        try:
            connector, secret = connections.open_for_provider(connection)
            banks = connector.list_supported_banks(secret, **connections.provider_call_args(connection))
        except ConnectorError as error:
            return Response({"code": error.code, "message": error.message}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"banks": [{"code": b.code, "name": b.name, "selfActivation": b.self_activation, "activationAmountMinor": b.activation_amount_minor} for b in banks]})


def _with_test(pair):
    connection, result = pair
    return {"connection": serializers.connection(connection, counts=_counts(connection)), "test": {"ok": result.ok, "code": result.code, "message": result.message}}


def _connection_only(result):
    return {"connection": serializers.connection(result, counts=_counts(result))}


def _webhook_token(membership, connection_id, data):
    connections.issue_webhook_token(membership, connection_id)
    return {"webhook": connections.webhook_setup(membership, connection_id)}


ACTIONS = {
    "test": lambda m, c, d: _with_test(connections.test(m, c)),
    "rename": lambda m, c, d: _connection_only(connections.rename(m, c, label=d.get("label"))),
    "disable": lambda m, c, d: _connection_only(connections.disable(m, c)),
    "enable": lambda m, c, d: _with_test(connections.enable(m, c)),
    "replace-credentials": lambda m, c, d: _connection_only(connections.replace_credentials(m, c, credentials=d.get("credentials"))),
    "disconnect": lambda m, c, d: _connection_only(connections.disconnect(m, c)),
    "webhook-token": _webhook_token,
}


@_SENSITIVE
class ConnectionActionView(APIView):
    action = None

    @mandate_errors
    def post(self, request, school_id, connection_id):
        membership = acting_membership(request, school_id, need=NEED_PROVIDER)
        return Response(ACTIONS[self.action](membership, connection_id, body(request)))


class MandateWebhookView(APIView):
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
        # Remita's own examples answer a notification with a plain OK.
        return HttpResponse("OK", content_type="text/plain") if provider == "remita" else Response({"received": True, "outcome": result.outcome})

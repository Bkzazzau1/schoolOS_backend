"""What every bank-connection view shares: turning a refusal into a plain answer, and reading a
request body or filter safely."""

from functools import wraps
from uuid import UUID

from rest_framework import status
from rest_framework.response import Response

from .provider_connections import BankRejected
from .vault import VaultError, VaultNotConfigured


def bank_errors(view):
    """A refusal is a normal outcome (400 with a stable `code`). Secure storage being unavailable is
    a 503, so the app can tell "you did something wrong" from "the server is not set up"."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except BankRejected as error:
            return Response({"code": error.code, "message": error.message}, status=status.HTTP_400_BAD_REQUEST)
        except VaultNotConfigured:
            return Response(
                {"code": "secure_storage_unavailable",
                 "message": "Secure storage for provider credentials is not set up on this server, so no provider can be connected yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except VaultError:
            return Response(
                {"code": "credential_unreadable",
                 "message": "The stored credential could not be opened. Replace the provider's credentials."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    return wrapped


def body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def is_uuid(value) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def bad_filter(name: str) -> Response:
    return Response({"code": "invalid_filter", "message": f"'{name}' is not valid."}, status=status.HTTP_400_BAD_REQUEST)

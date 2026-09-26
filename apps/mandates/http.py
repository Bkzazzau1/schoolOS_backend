"""What every mandate view shares: turning a refusal into a plain answer, and reading a request body safely."""

from functools import wraps
from uuid import UUID

from rest_framework import status
from rest_framework.response import Response

from .errors import MandateRefused
from .vault import VaultError, VaultNotConfigured

#: A person who is not allowed to do this (as opposed to a request that is wrong).
FORBIDDEN_CODES = {"not_mandate_manager", "not_preparer", "not_approver", "maker_cannot_approve", "not_operator", "not_the_payer"}
#: What the person was looking at is out of date: reload it.
CONFLICT_CODES = {"stale_preview", "stale_approval", "batch_changed", "approval_mismatch", "stale_consent"}


def mandate_errors(view):
    """A refusal is a normal outcome (400, 403 or 409 with a stable `code`). Secure storage being unavailable is a 503, so the app can tell
    "you did something wrong" from "the server is not set up"."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except MandateRefused as error:
            if error.code in FORBIDDEN_CODES:
                code = status.HTTP_403_FORBIDDEN
            elif error.code in CONFLICT_CODES:
                code = status.HTTP_409_CONFLICT
            else:
                code = status.HTTP_400_BAD_REQUEST
            return Response({"code": error.code, "message": error.message, **error.extra}, status=code)
        except VaultNotConfigured:
            return Response(
                {"code": "secure_storage_unavailable", "message": "Secure storage is not set up on this server, so no provider or bank details can be stored yet."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except VaultError:
            return Response(
                {"code": "credential_unreadable", "message": "The stored details could not be opened. Replace the provider's credentials."},
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

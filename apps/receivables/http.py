"""What every receivables view shares: a refusal is a normal outcome, and requests are read safely."""

from uuid import UUID

from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import Refused

MAX_PAGE = 200


class ReceivablesView(APIView):
    """Base of every receivables view. A refusal - by any method, on the way in or during the work - is a normal
    outcome: a 400 with a stable `code` and words a person can act on, never a 500."""

    def handle_exception(self, exc):
        if isinstance(exc, Refused):
            return Response({"code": exc.code, "message": exc.message}, status=status.HTTP_400_BAD_REQUEST)
        return super().handle_exception(exc)


def body(request) -> dict:
    return request.data if isinstance(request.data, dict) else {}


def uuid_arg(value, what: str = "reference"):
    try:
        return UUID(str(value))
    except ValueError:
        raise Refused(f"That {what} is not valid.", "invalid_reference")


def paging(request, default: int = 50) -> tuple[int, int]:
    try:
        limit = min(max(int(request.query_params.get("limit", default)), 1), MAX_PAGE)
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except ValueError:
        raise Refused("limit and offset must be whole numbers.", "invalid_paging")
    return limit, offset


def found(queryset, what: str):
    """The first match, or a 404 - which is also what another school's object looks like."""
    obj = queryset.first()
    if obj is None:
        raise NotFound(f"That {what} was not found.")
    return obj

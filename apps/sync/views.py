from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership

from . import pull as pulling
from .serializers import MutationSerializer, PullQuery
from .services import ACCEPTED, CONFLICT, apply_mutation


class PushView(APIView):
    """POST /api/v1/sync/push/ with one mutation.

    200 accepted, 409 conflict, 422 rejected, all with
    {"disposition", "serverVersion", "message"}. 403 means the signed-in
    person is not a member of that school. The app maps these to its
    SyncPushDisposition values.
    """

    def post(self, request):
        serializer = MutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outcome = apply_mutation(request.user, serializer.validated_data)
        http_status = {
            ACCEPTED: status.HTTP_200_OK,
            CONFLICT: status.HTTP_409_CONFLICT,
        }.get(outcome.disposition, status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(
            {
                "disposition": outcome.disposition,
                "serverVersion": outcome.server_version,
                "message": outcome.message,
            },
            status=http_status,
        )


class PullView(APIView):
    """GET /api/v1/sync/pull/?school=<id>&since=<cursor>&limit=<n>[&membership=<id>]

    Returns {"records": [...], "cursor": n, "hasMore": bool}. Start with since=0,
    apply the records, remember `cursor`, and ask again while `hasMore` is true;
    afterwards ask with the last cursor to get only what changed. A deleted record
    comes back with "deleted": true. Records the person may not see are left out.
    """

    def get(self, request):
        query = PullQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        q = query.validated_data
        membership = require_membership(request.user, q["school"], membership_id=q.get("membership"))
        page = pulling.pull(membership, q["since"], q["limit"])
        return Response({"records": page.records, "cursor": page.cursor, "hasMore": page.has_more})

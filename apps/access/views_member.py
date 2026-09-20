from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import membership_from_request

from .serializers import AcknowledgeSerializer
from .services import acknowledge, effective_activities, pending_blocks


def _access_json(membership):
    now = timezone.now()
    return {
        "membershipId": str(membership.id),
        "role": membership.role,
        "activities": sorted(effective_activities(membership, now)),
        # Blocks the owner has made that are not in force yet. The person still has
        # these activities. The app should fetch the latest, submit any pending work
        # for them, then call access/acknowledge/ so the block takes effect.
        "blocking": [
            {"activity": o.activity, "finalizeAt": o.finalize_at.isoformat()}
            for o in sorted(pending_blocks(membership, now), key=lambda o: o.activity)
        ],
    }


class MyAccessView(APIView):
    """GET schools/<school>/access/me/

    Which activities the signed-in person may see in this school. If they hold
    more than one role here, add ?membership=<id> to say which. The app shows
    only these in its menus, and re-reads this whenever it syncs, so a change the
    owner makes takes effect on the person's next sync.
    """

    def get(self, request, school_id):
        return Response(_access_json(membership_from_request(request, school_id)))


class AcknowledgeView(APIView):
    """POST schools/<school>/access/acknowledge/  {"activities": [...]}

    The app tells us it has fetched the latest and submitted its pending work for
    these activities, so the owner's pending blocks on them take effect now. It
    can only ever affect the caller's own blocks, and repeating it is harmless.
    """

    def post(self, request, school_id):
        membership = membership_from_request(request, school_id)
        body = AcknowledgeSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        acknowledge(membership, body.validated_data["activities"])
        return Response(_access_json(membership))

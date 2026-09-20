from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import membership_from_request

from . import services
from .models import Notification


def _json(n):
    return {
        "id": n.id, "kind": n.kind, "title": n.title, "message": n.message, "data": n.data,
        "createdAt": n.created_at.isoformat(), "read": n.read_at is not None,
    }


class InboxView(APIView):
    """GET schools/<school>/notifications/?unread=1&limit=50

    The signed-in person's own messages for this school, newest first. Nobody can
    read anyone else's.
    """

    def get(self, request, school_id):
        me = membership_from_request(request, school_id)
        mine = Notification.objects.filter(recipient=me)
        unread = mine.filter(read_at__isnull=True)
        try:
            limit = min(max(int(request.query_params.get("limit", 50)), 1), 200)
        except ValueError:
            limit = 50
        shown = unread if request.query_params.get("unread") in ("1", "true") else mine
        return Response({"unread": unread.count(), "notifications": [_json(n) for n in shown[:limit]]})


class ReadOneView(APIView):
    """POST schools/<school>/notifications/<id>/read/"""

    def post(self, request, school_id, notification_id):
        me = membership_from_request(request, school_id)
        if not services.mark_read(me, notification_id):
            return Response({"code": "not_found", "message": "That message was not found."}, status=404)
        return Response({"ok": True})


class ReadAllView(APIView):
    """POST schools/<school>/notifications/read-all/"""

    def post(self, request, school_id):
        me = membership_from_request(request, school_id)
        return Response({"marked": services.mark_all_read(me)})

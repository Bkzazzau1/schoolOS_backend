from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import membership_from_request

from .services import active_announcements_for_organization


def _json(announcement):
    return {
        "id": str(announcement.id),
        "title": announcement.title,
        "body": announcement.body,
        "startsAt": announcement.effective_from.isoformat(),
    }


class ActiveAnnouncementsView(APIView):
    """GET schools/<school>/announcements/active/

    Every announcement currently live for this school's organization - platform-wide
    ones and ones targeted at this organization specifically. Read-only; there is no
    path for a school's own staff to create or target one of these.
    """

    def get(self, request, school_id):
        me = membership_from_request(request, school_id, allow_when_restricted=True)
        announcements = active_announcements_for_organization(me.school.organization)
        return Response({"announcements": [_json(a) for a in announcements]})

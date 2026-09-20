from django.http import Http404
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import require_membership
from apps.schools.models import Role
from apps.sync.models import SyncRecord

from .handlers import OWNER_ENTITY_TYPES


class RecordsView(APIView):
    """GET owner/schools/<school>/records/<entity_type>/

    The owner's records of one kind, so a new or reinstalled device can load
    them. Only the school's owner may read them: salaries and payroll authority
    are not for anyone else. Deleted records are included and flagged, so a
    device can drop them.
    """

    def get(self, request, school_id, entity_type):
        if entity_type not in OWNER_ENTITY_TYPES:
            raise Http404
        require_membership(request.user, school_id, roles=[Role.PROPRIETOR])
        records = SyncRecord.objects.filter(
            school_id=school_id, entity_type=entity_type
        ).order_by("entity_id")
        return Response(
            {
                "records": [
                    {
                        "entityId": r.entity_id,
                        "version": r.version,
                        "deleted": r.deleted,
                        "updatedAt": r.updated_at.isoformat(),
                        "payload": r.payload,
                    }
                    for r in records
                ]
            }
        )

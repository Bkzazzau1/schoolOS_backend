from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import require_activity
from apps.schools.models import Role

from .models import AlumniProfile
from .serializers import AlumniProfileSerializer


class MyAlumniProfileView(APIView):
    def get(self, request, school_id):
        membership = require_activity(
            request.user,
            school_id,
            "alumni.profile",
            membership_id=request.query_params.get("membership"),
        )
        if membership.role != Role.ALUMNI:
            raise PermissionDenied("This endpoint requires an Alumni membership.")

        profile = AlumniProfile.objects.filter(
            school_id=school_id,
            membership=membership,
        ).select_related("membership__user").first()
        if profile is None:
            return Response(
                {
                    "membershipId": str(membership.id),
                    "schoolId": str(membership.school_id),
                    "role": membership.role,
                    "profile": None,
                    "message": "Your alumni identity has not been completed yet.",
                }
            )
        return Response({"profile": AlumniProfileSerializer(profile).data})

from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Membership


class MeView(APIView):
    """Who is signed in and which schools they can act in. The membership
    objects use the same shape the app stores locally (id, schoolId,
    schoolName, role)."""

    def get(self, request):
        memberships = Membership.objects.filter(
            user=request.user, is_active=True, school__is_active=True
        ).select_related("school")
        return Response(
            {
                "id": str(request.user.id),
                "email": request.user.email,
                "name": request.user.get_full_name(),
                "memberships": [
                    {
                        "id": str(m.id),
                        "schoolId": str(m.school_id),
                        "schoolName": m.school.name,
                        "role": m.role,
                    }
                    for m in memberships
                ],
            }
        )
